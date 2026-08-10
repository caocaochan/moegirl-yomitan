from __future__ import annotations

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable, Iterable
from uuid import uuid4

import requests

from .config import MAX_EXTRACT_BATCH_SIZE, Settings
from .models import SUMMARY_RECORD_SCHEMA_VERSION, ListedLink, ManifestPage, SummaryRecord
from .sitemaps import (
    canonical_article_url,
    merge_manifest_pages,
    parse_namespace_zero_sitemaps,
    parse_sitemap_entries,
    xml_has_closing_root,
)
from .text import normalize_whitespace, trim_summary


CHECKPOINT_INTERVAL_SECONDS = 30.0
CHECKPOINT_BATCH_INTERVAL = 100
SLOW_CHECKPOINT_SECONDS = 1.0
RECORD_CACHE_INDEX_PROGRESS_INTERVAL = 10_000
ATOMIC_WRITE_REPLACE_ATTEMPTS = 5
ATOMIC_WRITE_RETRY_SECONDS = 0.05
SESSION_POOL_BATCH = "batch"
SESSION_POOL_SITEMAP = "sitemap"
RECORD_CACHE_INDEX_SCHEMA_VERSION = 2
NEGATIVE_CACHE_SCHEMA_VERSION = 1

_THREAD_LOCAL = threading.local()
_ACTIVE_SESSION_REGISTRIES: dict[str, list["SessionRegistry"]] = {
    SESSION_POOL_BATCH: [],
    SESSION_POOL_SITEMAP: [],
}
_ACTIVE_SESSION_REGISTRIES_LOCK = threading.Lock()
_REQUEST_METRICS_LOCK = threading.Lock()
_REQUEST_RETRY_COUNT = 0


def build_session(settings: Settings) -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = settings.user_agent
    return session


@dataclass
class BatchTask:
    pages: list[ManifestPage]
    attempt: int = 0
    ready_at: float = 0.0


@dataclass
class AdaptiveState:
    current_concurrency: int
    consecutive_successes: int = 0
    cooldown_seconds: float = 0.0


@dataclass(frozen=True)
class CachedRecord:
    record_schema_version: int
    pageid: int
    canonical_title: str
    article_url: str
    source_url: str
    lastmod: str
    record_path: str
    file_size: int
    file_mtime_ns: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CachedRecord" | None:
        try:
            record_schema_version = data["record_schema_version"]
            pageid = data["pageid"]
            canonical_title = data["canonical_title"]
            article_url = data["article_url"]
            source_url = data["source_url"]
            lastmod = data["lastmod"]
            record_path = data["record_path"]
            file_size = data["file_size"]
            file_mtime_ns = data["file_mtime_ns"]
        except KeyError:
            return None

        if not isinstance(record_schema_version, int) or not isinstance(pageid, int):
            return None
        string_values = (canonical_title, article_url, source_url, lastmod, record_path)
        if not all(isinstance(value, str) for value in string_values):
            return None
        if not isinstance(file_size, int) or not isinstance(file_mtime_ns, int):
            return None
        return cls(
            record_schema_version=record_schema_version,
            pageid=pageid,
            canonical_title=canonical_title,
            article_url=article_url,
            source_url=source_url,
            lastmod=lastmod,
            record_path=record_path,
            file_size=file_size,
            file_mtime_ns=file_mtime_ns,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_schema_version": self.record_schema_version,
            "pageid": self.pageid,
            "canonical_title": self.canonical_title,
            "article_url": self.article_url,
            "source_url": self.source_url,
            "lastmod": self.lastmod,
            "record_path": self.record_path,
            "file_size": self.file_size,
            "file_mtime_ns": self.file_mtime_ns,
        }


@dataclass(frozen=True)
class NegativeCacheEntry:
    lastmod: str
    record_schema_version: int
    reason: str = "missing-or-empty"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NegativeCacheEntry" | None:
        lastmod = data.get("lastmod")
        record_schema_version = data.get("record_schema_version")
        reason = data.get("reason", "missing-or-empty")
        if not isinstance(lastmod, str) or not isinstance(record_schema_version, int) or not isinstance(reason, str):
            return None
        return cls(lastmod=lastmod, record_schema_version=record_schema_version, reason=reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lastmod": self.lastmod,
            "record_schema_version": self.record_schema_version,
            "reason": self.reason,
        }


@dataclass
class NegativeCacheIndex:
    by_source_url: dict[str, NegativeCacheEntry] = field(default_factory=dict)
    dirty: bool = False


@dataclass
class RecordCacheIndex:
    by_source_url: dict[str, CachedRecord]
    by_pageid: dict[int, CachedRecord]
    by_canonical_title: dict[str, CachedRecord]
    count: int = 0
    by_record_path: dict[str, CachedRecord] = field(default_factory=dict)


@dataclass
class FetchProgressState:
    cached_records_seen: int
    pages_hydrated_from_records: int
    pending_pages_remaining: int
    fetch_started_at: float
    last_checkpoint_at: float
    successful_batches: int = 0
    batches_since_checkpoint: int = 0
    records_fetched: int = 0
    new_pageids_seen: set[int] = field(default_factory=set)
    initial_pending_pages: int = 0
    current_concurrency: int = 1
    failed_batch_attempts: int = 0
    request_retry_baseline: int = 0
    recent_batch_seconds: deque[float] = field(default_factory=lambda: deque(maxlen=100))


@dataclass
class SessionRegistry:
    sessions: list[requests.Session] = field(default_factory=list)
    session_ids: set[int] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, session: requests.Session) -> None:
        with self.lock:
            session_id = id(session)
            if session_id in self.session_ids:
                return
            self.session_ids.add(session_id)
            self.sessions.append(session)

    def close_all(self) -> None:
        with self.lock:
            sessions = list(self.sessions)
            self.sessions.clear()
            self.session_ids.clear()

        for session in sessions:
            session.close()


def log_status(message: str) -> None:
    print(message, flush=True)


def request_retry_count() -> int:
    with _REQUEST_METRICS_LOCK:
        return _REQUEST_RETRY_COUNT


def record_request_retry() -> None:
    global _REQUEST_RETRY_COUNT
    with _REQUEST_METRICS_LOCK:
        _REQUEST_RETRY_COUNT += 1


class FirstListItemTitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.titles: list[str] = []
        self._ul_depth = 0
        self._done = False
        self._in_li = False
        self._li_has_title = False
        self._capture_bold = False
        self._bold_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._done:
            return
        if tag == "ul":
            self._ul_depth += 1
            return
        if self._ul_depth != 1:
            return
        if tag == "li":
            self._in_li = True
            self._li_has_title = False
            return
        if tag == "b" and self._in_li and not self._li_has_title:
            self._capture_bold = True
            self._bold_parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._done:
            return
        if tag == "b" and self._capture_bold:
            title = normalize_whitespace("".join(self._bold_parts))
            if title:
                self.titles.append(title)
                self._li_has_title = True
            self._capture_bold = False
            self._bold_parts = []
            return
        if tag == "li" and self._ul_depth == 1:
            self._in_li = False
            self._li_has_title = False
            self._capture_bold = False
            self._bold_parts = []
            return
        if tag == "ul" and self._ul_depth:
            self._ul_depth -= 1
            if self._ul_depth == 0:
                self._done = True

    def handle_data(self, data: str) -> None:
        if self._capture_bold:
            self._bold_parts.append(data)


def extract_listed_item_titles(extract_html: str) -> list[str]:
    parser = FirstListItemTitleParser()
    parser.feed(extract_html)
    return parser.titles


def summary_may_list_links(summary: str) -> bool:
    return summary.rstrip().endswith(("可以指：", "可以指:"))


def sitemap_progress_name(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or url


def log_sitemap_download_progress(
    done: int,
    total: int,
    workers: int,
    started_at: float,
    url: str,
) -> None:
    elapsed = time.monotonic() - started_at
    log_status(
        "Sitemap download progress: "
        f"{done}/{total} files, "
        f"workers={workers}, "
        f"elapsed={elapsed:.1f}s, "
        f"last={sitemap_progress_name(url)}"
    )


def log_record_cache_index_progress(
    checked: int,
    total: int,
    usable: int,
    reused: int,
    started_at: float,
) -> None:
    elapsed = time.monotonic() - started_at
    log_status(
        "Record cache index progress: "
        f"checked={checked}/{total}, "
        f"usable={usable}, "
        f"reused_index_entries={reused}, "
        f"elapsed={elapsed:.1f}s"
    )


def get_thread_session(settings: Settings, pool_name: str) -> requests.Session:
    sessions = getattr(_THREAD_LOCAL, "sessions", None)
    if sessions is None:
        sessions = {}
        _THREAD_LOCAL.sessions = sessions

    session = sessions.get(pool_name)
    if session is not None:
        return session

    session = build_session(settings)
    sessions[pool_name] = session
    register_active_session(pool_name, session)
    return session


def close_thread_session(pool_name: str) -> None:
    sessions = getattr(_THREAD_LOCAL, "sessions", None)
    if not sessions:
        return

    session = sessions.pop(pool_name, None)
    if session is not None:
        session.close()

    if not sessions:
        delattr(_THREAD_LOCAL, "sessions")


def register_active_session(pool_name: str, session: requests.Session) -> None:
    with _ACTIVE_SESSION_REGISTRIES_LOCK:
        registries = _ACTIVE_SESSION_REGISTRIES.get(pool_name, [])
        registry = registries[-1] if registries else None

    if registry is not None:
        registry.add(session)


@contextmanager
def session_registry_scope(pool_name: str) -> Iterable[SessionRegistry]:
    registry = SessionRegistry()
    with _ACTIVE_SESSION_REGISTRIES_LOCK:
        _ACTIVE_SESSION_REGISTRIES.setdefault(pool_name, []).append(registry)

    try:
        yield registry
    finally:
        registry.close_all()
        with _ACTIVE_SESSION_REGISTRIES_LOCK:
            registries = _ACTIVE_SESSION_REGISTRIES.get(pool_name, [])
            if registry in registries:
                registries.remove(registry)


def discover_pages(settings: Settings, session: requests.Session, limit: int | None = None) -> list[ManifestPage]:
    log_status("Downloading sitemap index...")
    sitemap_index_xml = fetch_text_from_candidates(
        session,
        sitemap_url_candidates(settings.sitemap_index_url),
        settings,
        validator=lambda text: xml_has_closing_root(text, "sitemapindex"),
    )
    sitemap_urls = parse_namespace_zero_sitemaps(sitemap_index_xml)
    log_status(f"Found {len(sitemap_urls)} namespace-zero sitemap files.")

    discovered: list[ManifestPage] = []
    if limit is None:
        sitemap_texts = fetch_sitemaps_in_parallel(settings, sitemap_urls)
        log_status("Parsing sitemap page entries...")
        for sitemap_url in sitemap_urls:
            sitemap_xml = sitemap_texts[sitemap_url]
            discovered.extend(parse_sitemap_entries(sitemap_xml, sitemap_url))
    else:
        started_at = time.monotonic()
        total = len(sitemap_urls)
        for sitemap_index, sitemap_url in enumerate(sitemap_urls, start=1):
            log_status(
                "Downloading sitemap file "
                f"{sitemap_index}/{total} for limited fetch: {sitemap_progress_name(sitemap_url)}"
            )
            sitemap_xml = fetch_sitemap_text_with_fallback(session, sitemap_url, settings)
            log_sitemap_download_progress(sitemap_index, total, 1, started_at, sitemap_url)
            discovered.extend(parse_sitemap_entries(sitemap_xml, sitemap_url))
            log_status(f"Discovered {len(discovered)} pages so far (limit={limit}).")
            if len(discovered) >= limit:
                discovered = discovered[:limit]
                log_status(f"Reached discovery limit of {limit} pages after {sitemap_index}/{total} sitemap files.")
                break

    log_status("Merging discovered pages with existing manifest...")
    previous_pages = {page.source_url: page for page in load_manifest(settings)}
    return merge_manifest_pages(discovered, previous_pages, retain_previous=limit is None)


def load_manifest_payload(settings: Settings) -> dict:
    if not settings.manifest_path.exists():
        return {}
    return json.loads(settings.manifest_path.read_text(encoding="utf-8"))


def load_manifest(settings: Settings) -> list[ManifestPage]:
    data = load_manifest_payload(settings)
    return [ManifestPage.from_dict(item) for item in data.get("pages", [])]


def save_manifest(settings: Settings, pages: Iterable[ManifestPage], progress: dict[str, int] | None = None) -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": utc_now_iso(),
        "sitemap_index_url": settings.sitemap_index_url,
        "pages": [page.to_dict() for page in pages],
    }
    if progress is not None:
        payload["progress"] = progress
    atomic_write_text(
        settings.manifest_path,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


def load_record(record_path: Path) -> SummaryRecord | None:
    if not record_path.exists():
        return None
    data = json.loads(record_path.read_text(encoding="utf-8"))
    return SummaryRecord.from_dict(data)


def record_path_for_page(settings: Settings, pageid: int) -> Path:
    return settings.records_dir / f"{pageid}.json"


def record_relative_path(settings: Settings, record_path: Path) -> str:
    return record_path.relative_to(settings.cache_dir).as_posix()


def cached_record_from_record(settings: Settings, record: SummaryRecord, record_path: Path) -> CachedRecord:
    record_stat = record_path.stat()
    return CachedRecord(
        record_schema_version=record.schema_version,
        pageid=record.pageid,
        canonical_title=record.canonical_title,
        article_url=record.article_url,
        source_url=record.source_url,
        lastmod=record.lastmod,
        record_path=record_relative_path(settings, record_path),
        file_size=record_stat.st_size,
        file_mtime_ns=record_stat.st_mtime_ns,
    )


def make_record_cache_index(records: Iterable[CachedRecord]) -> RecordCacheIndex:
    by_source_url: dict[str, CachedRecord] = {}
    by_pageid: dict[int, CachedRecord] = {}
    by_canonical_title: dict[str, CachedRecord] = {}
    by_record_path: dict[str, CachedRecord] = {}
    count = 0

    for record in records:
        count += 1
        by_source_url.setdefault(record.source_url, record)
        by_pageid.setdefault(record.pageid, record)
        by_canonical_title.setdefault(record.canonical_title, record)
        by_record_path[record.record_path] = record

    return RecordCacheIndex(
        by_source_url=by_source_url,
        by_pageid=by_pageid,
        by_canonical_title=by_canonical_title,
        count=count,
        by_record_path=by_record_path,
    )


def load_persisted_record_cache_entries(settings: Settings) -> dict[str, CachedRecord]:
    if not settings.record_cache_index_path.exists():
        return {}
    try:
        data = json.loads(settings.record_cache_index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("schema_version") != RECORD_CACHE_INDEX_SCHEMA_VERSION:
        return {}

    raw_records = data.get("records")
    if not isinstance(raw_records, dict):
        return {}

    records: dict[str, CachedRecord] = {}
    for key, value in raw_records.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        record = CachedRecord.from_dict(value)
        if record is not None and record.record_path == key:
            records[key] = record
    return records


def cached_record_matches_path(cached: CachedRecord, relative_record_path: str, record_stat) -> bool:
    return (
        cached.record_path == relative_record_path
        and cached.file_size == record_stat.st_size
        and cached.file_mtime_ns == record_stat.st_mtime_ns
    )


def save_record_cache_index(settings: Settings, record_index: RecordCacheIndex) -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        settings.record_cache_index_path,
        json.dumps(
            {
                "schema_version": RECORD_CACHE_INDEX_SCHEMA_VERSION,
                "records": {
                    path: record.to_dict()
                    for path, record in sorted(record_index.by_record_path.items())
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
    )


def load_negative_cache(settings: Settings) -> NegativeCacheIndex:
    if not settings.negative_cache_path.exists():
        return NegativeCacheIndex()
    try:
        data = json.loads(settings.negative_cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return NegativeCacheIndex()
    if not isinstance(data, dict) or data.get("schema_version") != NEGATIVE_CACHE_SCHEMA_VERSION:
        return NegativeCacheIndex()

    raw_entries = data.get("entries")
    if not isinstance(raw_entries, dict):
        return NegativeCacheIndex()
    entries: dict[str, NegativeCacheEntry] = {}
    for source_url, raw_entry in raw_entries.items():
        if not isinstance(source_url, str) or not isinstance(raw_entry, dict):
            continue
        entry = NegativeCacheEntry.from_dict(raw_entry)
        if entry is not None:
            entries[source_url] = entry
    return NegativeCacheIndex(entries)


def save_negative_cache(settings: Settings, negative_index: NegativeCacheIndex, *, force: bool = False) -> None:
    if not force and not negative_index.dirty:
        return
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        settings.negative_cache_path,
        json.dumps(
            {
                "schema_version": NEGATIVE_CACHE_SCHEMA_VERSION,
                "entries": {
                    source_url: entry.to_dict()
                    for source_url, entry in sorted(negative_index.by_source_url.items())
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
    negative_index.dirty = False


def mark_negative_cache_entry(negative_index: NegativeCacheIndex, page: ManifestPage) -> None:
    entry = NegativeCacheEntry(
        lastmod=page.lastmod,
        record_schema_version=SUMMARY_RECORD_SCHEMA_VERSION,
    )
    if negative_index.by_source_url.get(page.source_url) == entry:
        return
    negative_index.by_source_url[page.source_url] = entry
    negative_index.dirty = True


def clear_negative_cache_entry(negative_index: NegativeCacheIndex, source_url: str) -> None:
    if negative_index.by_source_url.pop(source_url, None) is not None:
        negative_index.dirty = True


def add_record_to_cache_index(settings: Settings, record_index: RecordCacheIndex, record: SummaryRecord) -> bool:
    record_path = record_path_for_page(settings, record.pageid)
    try:
        cached = cached_record_from_record(settings, record, record_path)
    except OSError:
        return False

    existing = record_index.by_record_path.get(cached.record_path)
    if existing is None:
        record_index.count += 1
    else:
        if record_index.by_source_url.get(existing.source_url) == existing:
            del record_index.by_source_url[existing.source_url]
        if record_index.by_pageid.get(existing.pageid) == existing:
            del record_index.by_pageid[existing.pageid]
        if record_index.by_canonical_title.get(existing.canonical_title) == existing:
            del record_index.by_canonical_title[existing.canonical_title]

    record_index.by_record_path[cached.record_path] = cached
    record_index.by_source_url.setdefault(cached.source_url, cached)
    record_index.by_pageid.setdefault(cached.pageid, cached)
    record_index.by_canonical_title.setdefault(cached.canonical_title, cached)
    return existing is None


def build_record_cache_index(settings: Settings) -> RecordCacheIndex:
    if not settings.records_dir.exists():
        log_status("Record cache directory does not exist; starting with an empty index.")
        return RecordCacheIndex({}, {}, {})

    persisted_records = load_persisted_record_cache_entries(settings)
    record_paths = sorted(settings.records_dir.glob("*.json"))
    total = len(record_paths)
    log_status(f"Scanning {total} record cache files for index reuse...")
    started_at = time.monotonic()
    records: list[CachedRecord] = []
    reused_records = 0

    for checked, record_path in enumerate(record_paths, start=1):
        try:
            record_stat = record_path.stat()
        except OSError:
            continue
        relative_record_path = record_relative_path(settings, record_path)
        cached = persisted_records.get(relative_record_path)
        if cached is not None and cached_record_matches_path(cached, relative_record_path, record_stat):
            records.append(cached)
            reused_records += 1
            if checked % RECORD_CACHE_INDEX_PROGRESS_INTERVAL == 0:
                log_record_cache_index_progress(checked, total, len(records), reused_records, started_at)
            continue

        record = load_record(record_path)
        if record is None:
            if checked % RECORD_CACHE_INDEX_PROGRESS_INTERVAL == 0:
                log_record_cache_index_progress(checked, total, len(records), reused_records, started_at)
            continue
        records.append(cached_record_from_record(settings, record, record_path))
        if checked % RECORD_CACHE_INDEX_PROGRESS_INTERVAL == 0:
            log_record_cache_index_progress(checked, total, len(records), reused_records, started_at)

    if total and total % RECORD_CACHE_INDEX_PROGRESS_INTERVAL != 0:
        log_record_cache_index_progress(total, total, len(records), reused_records, started_at)

    index = make_record_cache_index(records)
    log_status(f"Saving record cache index with {index.count} cached records...")
    save_record_cache_index(settings, index)
    return index


def build_record_cache_index_for_pages(settings: Settings, pages: Iterable[ManifestPage]) -> RecordCacheIndex:
    if not settings.records_dir.exists():
        log_status("Record cache directory does not exist; starting with an empty index.")
        return RecordCacheIndex({}, {}, {})

    record_paths: set[Path] = set()
    for page in pages:
        if page.record_path:
            record_paths.add(settings.cache_dir / page.record_path)
        if page.pageid is not None:
            record_paths.add(record_path_for_page(settings, page.pageid))

    if not record_paths:
        return build_record_cache_index(settings)

    persisted_records = load_persisted_record_cache_entries(settings)
    sorted_record_paths = sorted(record_paths)
    total = len(sorted_record_paths)
    log_status(f"Scanning {total} page-specific record cache files for index reuse...")
    started_at = time.monotonic()
    records: list[CachedRecord] = []
    reused_records = 0
    for checked, path in enumerate(sorted_record_paths, start=1):
        try:
            record_stat = path.stat()
        except OSError:
            continue
        relative_record_path = record_relative_path(settings, path)
        cached = persisted_records.get(relative_record_path)
        if cached is not None and cached_record_matches_path(cached, relative_record_path, record_stat):
            records.append(cached)
            reused_records += 1
            if checked % RECORD_CACHE_INDEX_PROGRESS_INTERVAL == 0:
                log_record_cache_index_progress(checked, total, len(records), reused_records, started_at)
            continue

        record = load_record(path)
        if record is not None:
            records.append(cached_record_from_record(settings, record, path))
        if checked % RECORD_CACHE_INDEX_PROGRESS_INTERVAL == 0:
            log_record_cache_index_progress(checked, total, len(records), reused_records, started_at)

    if total and total % RECORD_CACHE_INDEX_PROGRESS_INTERVAL != 0:
        log_record_cache_index_progress(total, total, len(records), reused_records, started_at)

    return make_record_cache_index(records)


def record_for_page(page: ManifestPage, record_index: RecordCacheIndex) -> CachedRecord | None:
    record = record_index.by_source_url.get(page.source_url)
    if record is not None:
        return record
    if page.pageid is not None:
        record = record_index.by_pageid.get(page.pageid)
        if record is not None:
            return record
    if page.canonical_title:
        record = record_index.by_canonical_title.get(page.canonical_title)
        if record is not None:
            return record
    return record_index.by_canonical_title.get(page.title_from_url)


def hydrate_page_from_record(settings: Settings, page: ManifestPage, record: CachedRecord) -> bool:
    changed = False

    if page.pageid != record.pageid:
        page.pageid = record.pageid
        changed = True
    if page.canonical_title != record.canonical_title:
        page.canonical_title = record.canonical_title
        changed = True
    if page.article_url != record.article_url:
        page.article_url = record.article_url
        changed = True
    if page.record_path != record.record_path:
        page.record_path = record.record_path
        changed = True

    return changed


def hydrate_pages_from_record_cache(
    settings: Settings,
    pages: Iterable[ManifestPage],
    record_index: RecordCacheIndex,
) -> int:
    hydrated = 0
    for page in pages:
        record = record_for_page(page, record_index)
        if record is None:
            continue
        if hydrate_page_from_record(settings, page, record):
            hydrated += 1
    return hydrated


def page_needs_fetch(
    page: ManifestPage,
    record: CachedRecord | None,
    negative_entry: NegativeCacheEntry | None = None,
) -> bool:
    if record is None:
        return not (
            negative_entry is not None
            and negative_entry.record_schema_version == SUMMARY_RECORD_SCHEMA_VERSION
            and negative_entry.lastmod == page.lastmod
        )
    if record.record_schema_version != SUMMARY_RECORD_SCHEMA_VERSION:
        return True
    if record.lastmod != page.lastmod:
        return True
    if page.canonical_title and record.canonical_title != page.canonical_title:
        return True
    return False


def build_progress(
    record_count: int,
    hydrated_from_records: int,
    pages_pending_fetch: int,
    batches_completed: int = 0,
    records_fetched: int = 0,
) -> dict[str, int]:
    return {
        "cached_records_seen": record_count,
        "pages_hydrated_from_records": hydrated_from_records,
        "pages_pending_fetch": pages_pending_fetch,
        "batches_completed": batches_completed,
        "records_fetched": records_fetched,
    }


def fetch_pages(settings: Settings, limit: int | None = None) -> list[ManifestPage]:
    if not 1 <= settings.batch_size <= MAX_EXTRACT_BATCH_SIZE:
        raise ValueError(
            f"batch_size must be between 1 and {MAX_EXTRACT_BATCH_SIZE}; the extracts API returns no more than "
            f"{MAX_EXTRACT_BATCH_SIZE} extracts per request"
        )

    session = build_session(settings)
    try:
        log_status("Discovering sitemap pages...")
        discover_started_at = time.monotonic()
        pages = discover_pages(settings, session, limit=limit)
        log_status(f"Discovered {len(pages)} pages in {time.monotonic() - discover_started_at:.1f}s.")
    finally:
        session.close()

    log_status("Loading record cache index...")
    index_started_at = time.monotonic()
    if limit is not None:
        record_index = build_record_cache_index_for_pages(settings, pages)
    else:
        record_index = build_record_cache_index(settings)
    log_status(f"Loaded {record_index.count} cached records in {time.monotonic() - index_started_at:.1f}s.")

    negative_index = load_negative_cache(settings)
    log_status(f"Loaded {len(negative_index.by_source_url)} cached missing/empty outcomes.")
    hydrated_from_records = hydrate_pages_from_record_cache(settings, pages, record_index)
    pending = [
        page
        for page in pages
        if page_needs_fetch(
            page,
            record_for_page(page, record_index),
            negative_index.by_source_url.get(page.source_url),
        )
    ]

    if not pending:
        save_manifest(settings, pages, progress=build_progress(record_index.count, hydrated_from_records, 0))
        save_negative_cache(settings, negative_index)
        log_status("No pending pages to fetch.")
        return pages

    settings.records_dir.mkdir(parents=True, exist_ok=True)
    batches = [pending[index : index + settings.batch_size] for index in range(0, len(pending), settings.batch_size)]
    fetch_started_at = time.monotonic()
    progress_state = FetchProgressState(
        cached_records_seen=record_index.count,
        pages_hydrated_from_records=hydrated_from_records,
        pending_pages_remaining=len(pending),
        fetch_started_at=fetch_started_at,
        last_checkpoint_at=fetch_started_at,
        initial_pending_pages=len(pending),
        current_concurrency=max(1, settings.concurrency),
        request_retry_baseline=request_retry_count(),
    )

    log_status(
        "Fetching pending pages: "
        f"{len(pending)} pages, "
        f"{len(batches)} batches, "
        f"concurrency={settings.concurrency}, "
        f"batch_retries={settings.batch_retry_attempts}, "
        f"progress_every={CHECKPOINT_BATCH_INTERVAL} batches or {CHECKPOINT_INTERVAL_SECONDS:.0f}s; "
        "full_manifest=start/end."
    )
    save_manifest_checkpoint(settings, pages, progress_state, negative_index=negative_index, force=True)
    run_adaptive_fetch_loop(settings, pages, batches, progress_state, record_index, negative_index)
    if limit is None:
        save_record_cache_index(settings, record_index)
    save_manifest_checkpoint(settings, pages, progress_state, negative_index=negative_index, force=True)
    return pages


def run_adaptive_fetch_loop(
    settings: Settings,
    all_pages: list[ManifestPage],
    batches: list[list[ManifestPage]],
    progress_state: FetchProgressState,
    record_index: RecordCacheIndex,
    negative_index: NegativeCacheIndex | None = None,
) -> None:
    if negative_index is None:
        negative_index = NegativeCacheIndex()
    queue: deque[BatchTask] = deque(BatchTask(batch) for batch in batches)
    max_workers = max(1, settings.concurrency)
    state = AdaptiveState(current_concurrency=max_workers)
    progress_state.current_concurrency = state.current_concurrency
    in_flight: dict[object, tuple[BatchTask, float]] = {}

    with session_registry_scope(SESSION_POOL_BATCH):
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            while queue or in_flight:
                while queue and len(in_flight) < state.current_concurrency:
                    task = pop_ready_batch(queue, time.monotonic())
                    if task is None:
                        break
                    submitted_at = time.monotonic()
                    future = executor.submit(fetch_batch, settings, task.pages)
                    in_flight[future] = (task, submitted_at)

                if not in_flight:
                    if queue:
                        time.sleep(max(0.0, min(task.ready_at for task in queue) - time.monotonic()))
                    continue

                wait_timeout = None
                if queue and len(in_flight) < state.current_concurrency:
                    wait_timeout = max(0.0, min(task.ready_at for task in queue) - time.monotonic())
                completed, _ = wait(in_flight, timeout=wait_timeout, return_when=FIRST_COMPLETED)
                if not completed:
                    continue
                future = next(iter(completed))
                task, submitted_at = in_flight.pop(future)
                batch_elapsed = time.monotonic() - submitted_at
                try:
                    records = future.result()
                except requests.RequestException as exc:
                    state = adaptive_state_after_failure(settings, state)
                    progress_state.current_concurrency = state.current_concurrency
                    progress_state.failed_batch_attempts += 1
                    if task.attempt + 1 >= settings.batch_retry_attempts:
                        raise
                    ready_at = time.monotonic() + state.cooldown_seconds
                    queue.append(BatchTask(task.pages, task.attempt + 1, ready_at))
                    log_status(
                        "Batch request failed; scheduled retry: "
                        f"attempt={task.attempt + 2}/{settings.batch_retry_attempts}, "
                        f"concurrency={state.current_concurrency}, "
                        f"cooldown={state.cooldown_seconds:.1f}s, "
                        f"error={format_request_error(exc)}"
                    )
                    continue

                for page, record in zip(task.pages, records):
                    if record is None:
                        mark_negative_cache_entry(negative_index, page)
                        continue
                    clear_negative_cache_entry(negative_index, page.source_url)
                    is_new_pageid = record.pageid not in record_index.by_pageid
                    write_record(settings, record)
                    add_record_to_cache_index(settings, record_index, record)
                    page.pageid = record.pageid
                    page.canonical_title = record.canonical_title
                    page.article_url = record.article_url
                    page.record_path = record_path_for_page(settings, record.pageid).relative_to(settings.cache_dir).as_posix()
                    progress_state.records_fetched += 1
                    if is_new_pageid:
                        progress_state.new_pageids_seen.add(record.pageid)

                progress_state.successful_batches += 1
                progress_state.batches_since_checkpoint += 1
                progress_state.recent_batch_seconds.append(batch_elapsed)
                progress_state.pending_pages_remaining = max(0, progress_state.pending_pages_remaining - len(task.pages))
                state = adaptive_state_after_success(settings, state)
                progress_state.current_concurrency = state.current_concurrency
                save_manifest_checkpoint(
                    settings,
                    all_pages,
                    progress_state,
                    negative_index=negative_index,
                )


def pop_ready_batch(queue: deque[BatchTask], now: float) -> BatchTask | None:
    for _ in range(len(queue)):
        task = queue.popleft()
        if task.ready_at <= now:
            return task
        queue.append(task)
    return None


def save_manifest_checkpoint(
    settings: Settings,
    pages: list[ManifestPage],
    progress_state: FetchProgressState,
    *,
    negative_index: NegativeCacheIndex | None = None,
    force: bool = False,
) -> None:
    now = time.monotonic()
    if not force and not should_checkpoint(progress_state, now):
        return

    progress = build_progress(
        progress_state.cached_records_seen + len(progress_state.new_pageids_seen),
        progress_state.pages_hydrated_from_records,
        progress_state.pending_pages_remaining,
        batches_completed=progress_state.successful_batches,
        records_fetched=progress_state.records_fetched,
    )
    elapsed = max(0.0, now - progress_state.fetch_started_at)
    processed_pages = max(0, progress_state.initial_pending_pages - progress_state.pending_pages_remaining)
    pages_per_second = processed_pages / elapsed if elapsed > 0 else 0.0
    eta_seconds = progress_state.pending_pages_remaining / pages_per_second if pages_per_second > 0 else None
    sorted_batch_seconds = sorted(progress_state.recent_batch_seconds)
    batch_p50 = sorted_batch_seconds[len(sorted_batch_seconds) // 2] if sorted_batch_seconds else None
    retry_count = max(0, request_retry_count() - progress_state.request_retry_baseline)
    batch_p50_text = f"{batch_p50:.2f}s" if batch_p50 is not None else "n/a"
    log_status(
        "Fetch progress: "
        f"batches={progress_state.successful_batches}, "
        f"records={progress_state.records_fetched}, "
        f"pending={progress_state.pending_pages_remaining}, "
        f"concurrency={progress_state.current_concurrency}, "
        f"rate={pages_per_second:.1f} pages/s, "
        f"batch_p50={batch_p50_text}, "
        f"retries={retry_count}, "
        f"batch_failures={progress_state.failed_batch_attempts}, "
        f"eta={format_duration(eta_seconds)}, "
        f"elapsed={elapsed:.1f}s"
    )
    save_fetch_progress(
        settings,
        progress,
        current_concurrency=progress_state.current_concurrency,
        request_retries=retry_count,
        failed_batch_attempts=progress_state.failed_batch_attempts,
    )
    if negative_index is not None:
        save_negative_cache(settings, negative_index)
    if force:
        log_status("Saving manifest checkpoint...")
        save_started_at = time.monotonic()
        save_manifest(settings, pages, progress=progress)
        save_elapsed = time.monotonic() - save_started_at
        if save_elapsed > SLOW_CHECKPOINT_SECONDS:
            log_status(f"Saved manifest checkpoint in {save_elapsed:.1f}s.")
    progress_state.last_checkpoint_at = time.monotonic()
    progress_state.batches_since_checkpoint = 0


def save_fetch_progress(
    settings: Settings,
    progress: dict[str, int],
    *,
    current_concurrency: int,
    request_retries: int,
    failed_batch_attempts: int,
) -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        settings.fetch_progress_path,
        json.dumps(
            {
                "generated_at": utc_now_iso(),
                "progress": progress,
                "current_concurrency": current_concurrency,
                "request_retries": request_retries,
                "failed_batch_attempts": failed_batch_attempts,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    rounded = max(0, int(seconds + 0.5))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def should_checkpoint(progress_state: FetchProgressState, now: float) -> bool:
    return (
        progress_state.batches_since_checkpoint >= CHECKPOINT_BATCH_INTERVAL
        or now - progress_state.last_checkpoint_at >= CHECKPOINT_INTERVAL_SECONDS
    )


def fetch_batch(settings: Settings, batch: list[ManifestPage]) -> list[SummaryRecord | None]:
    session = get_thread_session(settings, SESSION_POOL_BATCH)
    return fetch_batch_with_session(session, settings, batch)


def fetch_batch_with_session(
    session: requests.Session,
    settings: Settings,
    batch: list[ManifestPage],
) -> list[SummaryRecord | None]:
    titles = [page.title_from_url for page in batch]
    pageids = [page.pageid for page in batch if page.pageid is not None]
    try:
        if batch and len(pageids) == len(batch):
            payload = fetch_extract_payload(session, settings, [], pageids=pageids)
        else:
            payload = fetch_extract_payload(session, settings, titles)
    except requests.HTTPError as exc:
        if not is_request_too_large_error(exc) or len(batch) <= 1:
            raise

        midpoint = len(batch) // 2
        left_records = fetch_batch_with_session(session, settings, batch[:midpoint])
        right_records = fetch_batch_with_session(session, settings, batch[midpoint:])
        return left_records + right_records

    query = payload.get("query", {})
    normalized_map = {item["from"]: item["to"] for item in query.get("normalized", [])}
    redirect_map = {item["from"]: item["to"] for item in query.get("redirects", [])}
    pages_by_title = {}
    pages_by_pageid: dict[int, dict[str, Any]] = {}
    for value in query.get("pages", {}).values():
        if "missing" in value:
            continue
        pages_by_title[value["title"]] = value
        if "pageid" in value:
            pages_by_pageid[int(value["pageid"])] = value

    records: list[SummaryRecord | None] = []
    for requested_page in batch:
        payload_page = pages_by_pageid.get(requested_page.pageid) if requested_page.pageid is not None else None
        if payload_page is None:
            resolved_title = normalized_map.get(requested_page.title_from_url, requested_page.title_from_url)
            resolved_title = redirect_map.get(resolved_title, resolved_title)
            payload_page = pages_by_title.get(resolved_title)
            if payload_page is None:
                payload_page = pages_by_title.get(requested_page.canonical_title or requested_page.title_from_url)
        if payload_page is None:
            records.append(None)
            continue

        extract = payload_page.get("extract", "")
        summary = trim_summary(extract, settings.summary_char_limit)
        summary = normalize_whitespace(summary)
        if not summary:
            records.append(None)
            continue

        pageid = int(payload_page["pageid"])
        title = payload_page["title"]
        article_url = requested_page.source_url
        listed_links = []
        if summary_may_list_links(summary):
            listed_links = fetch_listed_links(session, settings, title)
        records.append(
            SummaryRecord(
                pageid=pageid,
                canonical_title=title,
                article_url=article_url,
                source_url=requested_page.source_url,
                lastmod=requested_page.lastmod,
                summary=summary,
                retrieved_at=utc_now_iso(),
                listed_links=listed_links,
            )
        )
    return records


def fetch_sitemaps_in_parallel(settings: Settings, sitemap_urls: list[str]) -> dict[str, str]:
    if not sitemap_urls:
        return {}

    workers = max(1, min(settings.sitemap_concurrency, len(sitemap_urls)))
    started_at = time.monotonic()
    log_status(f"Downloading {len(sitemap_urls)} sitemap files with {workers} workers...")
    if workers == 1:
        with session_registry_scope(SESSION_POOL_SITEMAP):
            results = {}
            for done, url in enumerate(sitemap_urls, start=1):
                results[url] = fetch_sitemap_worker(settings, url)
                log_sitemap_download_progress(done, len(sitemap_urls), workers, started_at, url)
            return results

    results: dict[str, str] = {}
    with session_registry_scope(SESSION_POOL_SITEMAP):
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch_sitemap_worker, settings, url): url for url in sitemap_urls}
            for done, future in enumerate(as_completed(futures), start=1):
                url = futures[future]
                results[url] = future.result()
                log_sitemap_download_progress(done, len(sitemap_urls), workers, started_at, url)
    return results


def fetch_sitemap_worker(settings: Settings, url: str) -> str:
    session = get_thread_session(settings, SESSION_POOL_SITEMAP)
    return fetch_sitemap_text_with_fallback(session, url, settings)


def fetch_extract_payload(
    session: requests.Session,
    settings: Settings,
    titles: list[str],
    *,
    pageids: list[int] | None = None,
) -> dict:
    if pageids is not None and titles:
        raise ValueError("Specify titles or pageids, not both")
    requested_count = len(pageids) if pageids is not None else len(titles)
    if not 1 <= requested_count <= MAX_EXTRACT_BATCH_SIZE:
        raise ValueError(f"Extract requests must contain between 1 and {MAX_EXTRACT_BATCH_SIZE} pages")
    payload = {
        "action": "query",
        "prop": "extracts",
        "exintro": "1",
        "explaintext": "1",
        "redirects": "1",
        "format": "json",
    }
    if pageids is not None:
        payload["pageids"] = "|".join(str(pageid) for pageid in pageids)
    else:
        payload["titles"] = "|".join(titles)
    last_error: requests.RequestException | None = None
    for url in host_fallback_candidates(settings.extracts_api_url):
        try:
            response = request_with_retry(session, url, settings, method="POST", data=payload)
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise requests.HTTPError(f"Unable to fetch extracts from any candidate for {settings.extracts_api_url}")


def fetch_listed_links(session: requests.Session, settings: Settings, title: str) -> list[ListedLink]:
    extract_html = ""
    linked_titles: set[str] = set()
    continuation: dict[str, Any] | None = None

    while True:
        payload = fetch_listed_links_payload(session, settings, title, continuation=continuation)
        query = payload.get("query", {})
        pages = [value for value in query.get("pages", {}).values() if "missing" not in value]
        if pages:
            page = pages[0]
            if not extract_html:
                extract_html = page.get("extract", "")
            for link in page.get("links", []):
                if link.get("ns") != 0:
                    continue
                linked_title = link.get("title")
                if isinstance(linked_title, str) and linked_title:
                    linked_titles.add(linked_title)

        raw_continuation = payload.get("continue")
        if not isinstance(raw_continuation, dict) or "plcontinue" not in raw_continuation:
            break
        continuation = raw_continuation

    if not extract_html or not linked_titles:
        return []

    links: list[ListedLink] = []
    seen_titles: set[str] = set()
    for listed_title in extract_listed_item_titles(extract_html):
        if listed_title in seen_titles or listed_title not in linked_titles:
            continue
        seen_titles.add(listed_title)
        links.append(ListedLink(title=listed_title, url=canonical_article_url(listed_title)))
    return links


def fetch_listed_links_payload(
    session: requests.Session,
    settings: Settings,
    title: str,
    continuation: dict[str, Any] | None = None,
) -> dict:
    payload: dict[str, Any] = {
        "action": "query",
        "prop": "extracts|links",
        "redirects": "1",
        "format": "json",
        "titles": title,
        "plnamespace": "0",
        "pllimit": "max",
    }
    if continuation:
        payload.update(continuation)

    last_error: requests.RequestException | None = None
    for url in host_fallback_candidates(settings.extracts_api_url):
        try:
            response = request_with_retry(session, url, settings, method="POST", data=payload)
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise requests.HTTPError(f"Unable to fetch listed links from any candidate for {settings.extracts_api_url}")


def fetch_text_with_retry(
    session: requests.Session,
    url: str,
    settings: Settings,
    validator: Callable[[str], bool] | None = None,
) -> str:
    for attempt in range(settings.retry_attempts):
        response = request_with_retry(session, url, settings)
        text = response.text
        if validator is None or validator(text):
            return text
        if attempt == settings.retry_attempts - 1:
            raise requests.HTTPError(f"Incomplete response while fetching {url}")
        sleep_seconds = settings.backoff_base_seconds * (2**attempt)
        time.sleep(sleep_seconds)
    raise RuntimeError(f"Failed to fetch complete text from {url}")


def fetch_text_with_curl(
    url: str,
    settings: Settings,
    validator: Callable[[str], bool] | None = None,
) -> str:
    connect_timeout, read_timeout = request_timeout_parts(settings)
    result = subprocess.run(
        [
            "curl",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--connect-timeout",
            str(connect_timeout),
            "--max-time",
            str(read_timeout),
            "--user-agent",
            settings.user_agent,
            "--header",
            "Accept: application/xml,text/xml,*/*;q=0.8",
            url,
        ],
        capture_output=True,
        encoding="utf-8",
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or "<no stderr>"
        raise requests.HTTPError(f"curl failed while fetching {url}: {stderr}")

    text = result.stdout
    if validator is not None and not validator(text):
        raise requests.HTTPError(f"Incomplete response while fetching {url} via curl")
    return text


def request_timeout_parts(settings: Settings) -> tuple[float, float]:
    timeout = settings.request_timeout
    if isinstance(timeout, tuple):
        return float(timeout[0]), float(timeout[1])
    value = float(timeout)
    return value, value


def fetch_text_with_transport_fallback(
    session: requests.Session,
    url: str,
    settings: Settings,
    validator: Callable[[str], bool] | None = None,
) -> str:
    try:
        return fetch_text_with_retry(session, url, settings, validator=validator)
    except requests.RequestException as requests_error:
        try:
            return fetch_text_with_curl(url, settings, validator=validator)
        except requests.RequestException as curl_error:
            raise requests.HTTPError(
                "Unable to fetch text with requests or curl for "
                f"{url}. requests=({format_request_error(requests_error)}); "
                f"curl=({format_request_error(curl_error)})"
            ) from curl_error


def fetch_sitemap_text_with_fallback(session: requests.Session, url: str, settings: Settings) -> str:
    candidates = sitemap_url_candidates(url)
    best_partial: tuple[int, str] | None = None
    for candidate in candidates:
        try:
            return fetch_text_with_transport_fallback(
                session,
                candidate,
                settings,
                validator=lambda text: xml_has_closing_root(text, "urlset"),
            )
        except requests.RequestException:
            try:
                partial_text = fetch_text_with_transport_fallback(session, candidate, settings, validator=None)
            except requests.RequestException:
                continue
            if best_partial is None or len(partial_text) > best_partial[0]:
                best_partial = (len(partial_text), partial_text)

    if best_partial is not None:
        return best_partial[1]
    raise requests.HTTPError(f"Unable to fetch sitemap text from any candidate for {url}")


def fetch_text_from_candidates(
    session: requests.Session,
    candidates: list[str],
    settings: Settings,
    validator: Callable[[str], bool] | None = None,
) -> str:
    errors: list[tuple[str, requests.RequestException]] = []
    for candidate in candidates:
        try:
            return fetch_text_with_transport_fallback(session, candidate, settings, validator=validator)
        except requests.RequestException as exc:
            errors.append((candidate, exc))
            continue

    raise requests.HTTPError(
        build_candidate_failure_message("Unable to fetch text from any candidate", errors, candidates)
    )


def build_candidate_failure_message(
    prefix: str,
    errors: list[tuple[str, requests.RequestException]],
    candidates: list[str],
) -> str:
    attempted = errors or [(candidate, requests.HTTPError("not attempted")) for candidate in candidates]
    details = "; ".join(f"{url} ({format_request_error(error)})" for url, error in attempted)
    first_candidate = candidates[0] if candidates else "<no candidates>"
    return f"{prefix} for {first_candidate}. Attempts: {details}"


def format_request_error(error: requests.RequestException) -> str:
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    status = f" status={status_code}" if status_code is not None else ""
    message = str(error).replace("\n", " ").strip()
    if not message:
        message = "<no message>"
    return f"{type(error).__name__}{status}: {message}"


def adaptive_state_after_failure(settings: Settings, state: AdaptiveState) -> AdaptiveState:
    reduced_concurrency = max(settings.min_concurrency, state.current_concurrency - 1)
    next_cooldown = settings.backoff_base_seconds if state.cooldown_seconds <= 0 else min(
        settings.adaptive_backoff_cap_seconds,
        state.cooldown_seconds * 2,
    )
    return AdaptiveState(
        current_concurrency=reduced_concurrency,
        consecutive_successes=0,
        cooldown_seconds=next_cooldown,
    )


def adaptive_state_after_success(settings: Settings, state: AdaptiveState) -> AdaptiveState:
    consecutive_successes = state.consecutive_successes + 1
    current_concurrency = state.current_concurrency
    if consecutive_successes >= 2 and current_concurrency < settings.concurrency:
        current_concurrency += 1
        consecutive_successes = 0

    cooldown_seconds = 0.0 if state.cooldown_seconds <= settings.backoff_base_seconds else state.cooldown_seconds / 2
    return AdaptiveState(
        current_concurrency=current_concurrency,
        consecutive_successes=consecutive_successes,
        cooldown_seconds=cooldown_seconds,
    )


def sitemap_url_candidates(url: str) -> list[str]:
    candidates = host_fallback_candidates(url)

    for base in list(candidates):
        separator = "&" if "?" in base else "?"
        candidates.append(f"{base}{separator}output=1")

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


def host_fallback_candidates(url: str) -> list[str]:
    candidates = [url]
    if "mzh.moegirl.org.cn" in url:
        candidates.append(url.replace("mzh.moegirl.org.cn", "zh.moegirl.org.cn", 1))
    elif "zh.moegirl.org.cn" in url:
        candidates.append(url.replace("zh.moegirl.org.cn", "mzh.moegirl.org.cn", 1))

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


def is_request_too_large_error(error: requests.HTTPError) -> bool:
    response = error.response
    return response is not None and response.status_code in {413, 414, 431}


def request_with_retry(
    session: requests.Session,
    url: str,
    settings: Settings,
    method: str = "GET",
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
) -> requests.Response:
    for attempt in range(settings.retry_attempts):
        try:
            response = session.request(method, url, params=params, data=data, timeout=settings.request_timeout)
            if response.status_code < 400:
                return response
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
            if attempt == settings.retry_attempts - 1:
                response.raise_for_status()
        except requests.RequestException:
            if attempt == settings.retry_attempts - 1:
                raise
        record_request_retry()
        sleep_seconds = settings.backoff_base_seconds * (2**attempt)
        time.sleep(sleep_seconds)
    raise RuntimeError(f"Failed to fetch {url}")


def write_record(settings: Settings, record: SummaryRecord) -> None:
    path = record_path_for_page(settings, record.pageid)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record.to_dict(), ensure_ascii=False, indent=2)
    if path.exists():
        existing_text = path.read_text(encoding="utf-8")
        if existing_text == payload:
            return
        try:
            existing_payload = json.loads(existing_text)
        except json.JSONDecodeError:
            existing_payload = None
        if existing_payload == record.to_dict():
            return
    atomic_write_text(path, payload)


def atomic_write_text(path: Path, content: str) -> None:
    temp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    try:
        for attempt in range(ATOMIC_WRITE_REPLACE_ATTEMPTS):
            try:
                temp_path.replace(path)
                return
            except OSError:
                if attempt == ATOMIC_WRITE_REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(ATOMIC_WRITE_RETRY_SECONDS)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
