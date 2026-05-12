from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Callable, Any
from zipfile import ZIP_DEFLATED, ZipFile

from pypinyin import Style, lazy_pinyin

from .config import Settings
from .fetcher import atomic_write_text, load_manifest, load_record, record_path_for_page
from .models import SummaryRecord
from .versioning import resolve_build_version

FULLWIDTH_ALIAS_PATTERN = re.compile(r"^(?P<base>.+?)（[^（）]+）$")
HANZI_RUN_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
READING_COLLAPSE_SPACE_PATTERN = re.compile(r"\s+")
READING_SPACE_BEFORE_PUNCTUATION_PATTERN = re.compile(r"\s+([,.:;!?%)\]}>:：；，。！？、）】》」』])")
READING_SPACE_BEFORE_OPENING_PATTERN = re.compile(r"\s+([(\[<{（【《「『])")
READING_SPACE_AFTER_OPENING_PATTERN = re.compile(r"([(\[<{（【《「『])\s+")
READING_PUNCTUATION_WITH_TRAILING_SPACE_PATTERN = re.compile(r"([,.:;!?:：；，。！？、])(?=\S)")
STRUCTURED_CONTENT_LANG = "zh-Hans"
BUILD_STATE_SCHEMA_VERSION = 2
FINGERPRINT_ALGORITHM_VERSION = "packaged-content-v2"
_PINYIN_DATA_READY = False
ProgressReporter = Callable[[str], None]


@dataclass(frozen=True)
class PackagedRecordFingerprint:
    pageid: int
    canonical_title: str
    lastmod: str
    record_path: str
    file_size: int
    file_mtime_ns: int
    fingerprint: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PackagedRecordFingerprint" | None:
        try:
            pageid = data["pageid"]
            canonical_title = data["canonical_title"]
            lastmod = data["lastmod"]
            record_path = data["record_path"]
            file_size = data["file_size"]
            file_mtime_ns = data["file_mtime_ns"]
            fingerprint = data["fingerprint"]
        except KeyError:
            return None

        if not isinstance(pageid, int):
            return None
        if not all(isinstance(value, str) for value in (canonical_title, lastmod, record_path, fingerprint)):
            return None
        if not isinstance(file_size, int) or not isinstance(file_mtime_ns, int):
            return None
        return cls(
            pageid=pageid,
            canonical_title=canonical_title,
            lastmod=lastmod,
            record_path=record_path,
            file_size=file_size,
            file_mtime_ns=file_mtime_ns,
            fingerprint=fingerprint,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pageid": self.pageid,
            "canonical_title": self.canonical_title,
            "lastmod": self.lastmod,
            "record_path": self.record_path,
            "file_size": self.file_size,
            "file_mtime_ns": self.file_mtime_ns,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class DictionaryFingerprintResult:
    fingerprint: str
    records: list[PackagedRecordFingerprint]
    reused_records: int
    recomputed_records: int


def package_dictionary(settings: Settings) -> Path:
    ordered_records = load_packaged_records(settings)
    build_version = resolve_build_version()
    index_data = build_index(settings, revision=build_version)
    serialized_index = json.dumps(index_data, ensure_ascii=False, indent=2)

    settings.output_zip.parent.mkdir(parents=True, exist_ok=True)
    settings.output_index.parent.mkdir(parents=True, exist_ok=True)
    settings.output_index.write_text(serialized_index, encoding="utf-8")
    with ZipFile(settings.output_zip, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("index.json", serialized_index)
        for file_number, chunk in enumerate(chunked(ordered_records, settings.chunk_size), start=1):
            entries = [entry for record in chunk for entry in build_term_entries(record)]
            archive.writestr(
                f"term_bank_{file_number}.json",
                json.dumps(entries, ensure_ascii=False, separators=(",", ":")),
            )
    return settings.output_zip


def load_packaged_records(settings: Settings) -> list[SummaryRecord]:
    manifest_pages = load_manifest(settings)
    deduped: dict[int, SummaryRecord] = {}
    for page in manifest_pages:
        if page.pageid is None:
            continue
        record = load_record(packaged_record_path(settings, page))
        if record is None or record.lastmod != page.lastmod:
            continue
        existing = deduped.get(record.pageid)
        if existing is None or (record.canonical_title, record.pageid) < (existing.canonical_title, existing.pageid):
            deduped[record.pageid] = record

    return sorted(deduped.values(), key=lambda item: (item.canonical_title.casefold(), item.pageid))


def build_dictionary_content_fingerprint(settings: Settings, progress: ProgressReporter | None = None) -> str:
    return build_dictionary_content_fingerprint_result(settings, progress=progress).fingerprint


def build_dictionary_content_fingerprint_result(
    settings: Settings,
    progress: ProgressReporter | None = None,
) -> DictionaryFingerprintResult:
    started_at = time.monotonic()
    previous_state = load_build_state(settings)
    previous_records = load_record_fingerprint_cache(previous_state)
    manifest_pages = load_manifest(settings)
    report_progress(progress, f"Loaded manifest with {len(manifest_pages)} pages.")

    records: dict[int, PackagedRecordFingerprint] = {}
    reused_records = 0
    recomputed_records = 0

    can_reuse_previous = previous_state.get("algorithm_version") == FINGERPRINT_ALGORITHM_VERSION
    for processed, page in enumerate(manifest_pages, start=1):
        if page.pageid is None:
            continue

        record_path = packaged_record_path(settings, page)
        try:
            record_stat = record_path.stat()
        except OSError:
            continue

        relative_record_path = packaged_record_relative_path(settings, record_path)
        cached = previous_records.get(str(page.pageid)) if can_reuse_previous else None
        entry: PackagedRecordFingerprint | None = None
        if cached is not None and cached_matches_page(cached, page, relative_record_path, record_stat):
            entry = cached
            reused_records += 1
        else:
            record = load_record(record_path)
            if record is None or record.lastmod != page.lastmod:
                continue
            entry = PackagedRecordFingerprint(
                pageid=record.pageid,
                canonical_title=record.canonical_title,
                lastmod=record.lastmod,
                record_path=relative_record_path,
                file_size=record_stat.st_size,
                file_mtime_ns=record_stat.st_mtime_ns,
                fingerprint=build_packaged_record_fingerprint(record),
            )
            recomputed_records += 1

        existing = records.get(entry.pageid)
        if existing is None or (entry.canonical_title, entry.pageid) < (existing.canonical_title, existing.pageid):
            records[entry.pageid] = entry

        if processed % 10_000 == 0:
            report_progress(
                progress,
                f"Checked {processed} manifest pages; reused {reused_records}, recomputed {recomputed_records}.",
            )

    ordered_records = sorted(records.values(), key=lambda item: (item.canonical_title.casefold(), item.pageid))
    fingerprint = compose_dictionary_content_fingerprint(settings, ordered_records)
    elapsed = time.monotonic() - started_at
    report_progress(
        progress,
        (
            f"Packaged fingerprint ready for {len(ordered_records)} records "
            f"(reused {reused_records}, recomputed {recomputed_records}) in {elapsed:.1f}s."
        ),
    )
    return DictionaryFingerprintResult(
        fingerprint=fingerprint,
        records=ordered_records,
        reused_records=reused_records,
        recomputed_records=recomputed_records,
    )


def load_record_fingerprint_cache(state: dict) -> dict[str, PackagedRecordFingerprint]:
    if state.get("schema_version") != BUILD_STATE_SCHEMA_VERSION:
        return {}
    raw_records = state.get("record_fingerprints")
    if not isinstance(raw_records, dict):
        return {}

    records: dict[str, PackagedRecordFingerprint] = {}
    for key, value in raw_records.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        record = PackagedRecordFingerprint.from_dict(value)
        if record is not None:
            records[key] = record
    return records


def cached_matches_page(
    cached: PackagedRecordFingerprint,
    page,
    relative_record_path: str,
    record_stat,
) -> bool:
    return (
        cached.pageid == page.pageid
        and cached.lastmod == page.lastmod
        and cached.record_path == relative_record_path
        and cached.file_size == record_stat.st_size
        and cached.file_mtime_ns == record_stat.st_mtime_ns
    )


def build_packaged_record_fingerprint(record: SummaryRecord) -> str:
    serialized = json.dumps(
        build_term_entries(record),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compose_dictionary_content_fingerprint(settings: Settings, records: list[PackagedRecordFingerprint]) -> str:
    payload = {
        "algorithm_version": FINGERPRINT_ALGORITHM_VERSION,
        "chunk_size": settings.chunk_size,
        "index": build_stable_index_data(settings),
        "records": [{"pageid": record.pageid, "fingerprint": record.fingerprint} for record in records],
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def packaged_record_path(settings: Settings, page) -> Path:
    if page.record_path:
        return settings.cache_dir / page.record_path
    return record_path_for_page(settings, page.pageid)


def packaged_record_relative_path(settings: Settings, record_path: Path) -> str:
    try:
        return record_path.relative_to(settings.cache_dir).as_posix()
    except ValueError:
        return record_path.as_posix()


def report_progress(progress: ProgressReporter | None, message: str) -> None:
    if progress is not None:
        progress(message)


def build_dictionary_content_payload(settings: Settings) -> dict:
    records = load_packaged_records(settings)
    term_banks = []
    for chunk in chunked(records, settings.chunk_size):
        term_banks.append([entry for record in chunk for entry in build_term_entries(record)])

    return {
        "index": build_stable_index_data(settings),
        "termBanks": term_banks,
    }


def build_stable_index_data(settings: Settings) -> dict:
    index_data = build_index(settings, revision="build-fingerprint")
    index_data.pop("revision", None)
    return index_data


def load_build_state(settings: Settings) -> dict:
    if not settings.build_state_path.exists():
        return {}
    try:
        data = json.loads(settings.build_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_last_build_fingerprint(settings: Settings) -> str | None:
    fingerprint = load_build_state(settings).get("content_fingerprint")
    return fingerprint if isinstance(fingerprint, str) and fingerprint else None


def save_build_state(settings: Settings, fingerprint: str, progress: ProgressReporter | None = None) -> None:
    result = build_dictionary_content_fingerprint_result(settings, progress=progress)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        settings.build_state_path,
        json.dumps(
            {
                "schema_version": BUILD_STATE_SCHEMA_VERSION,
                "algorithm_version": FINGERPRINT_ALGORITHM_VERSION,
                "content_fingerprint": fingerprint,
                "record_fingerprints": {str(record.pageid): record.to_dict() for record in result.records},
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
    )


def build_index(settings: Settings, revision: str | None = None) -> dict:
    build_version = resolve_build_version() if revision is None else revision
    return {
        "title": settings.dictionary_title,
        "format": 3,
        "revision": build_version,
        "sequenced": True,
        "isUpdatable": True,
        "indexUrl": settings.dictionary_update_index_url,
        "downloadUrl": settings.dictionary_update_download_url,
        "sourceLanguage": "zh",
        "targetLanguage": "zh",
        "url": settings.dictionary_source_url,
        "description": "来自萌娘百科的简短导语摘要词典。仅包含摘要与原文链接，不提供全文。",
        "attribution": "内容来源：萌娘百科（Moegirlpedia）。本词典仅提供摘要，非全文，请通过词条链接访问原文。",
    }


def build_term_entry(record: SummaryRecord) -> list:
    return build_term_entry_for_term(record, record.canonical_title)


def build_term_entries(record: SummaryRecord) -> list[list]:
    entries = [build_term_entry(record)]
    alias = alias_term_for_title(record.canonical_title)
    if alias is not None:
        entries.append(build_term_entry_for_term(record, alias, score=-1))
    return entries


def build_term_entry_for_term(record: SummaryRecord, term: str, score: int = 0) -> list:
    return [
        term,
        term_reading_for_term(term),
        "",
        "",
        score,
        [
            {
                "type": "structured-content",
                "content": [
                    {"tag": "div", "lang": STRUCTURED_CONTENT_LANG, "content": [record.summary]},
                    {
                        "tag": "div",
                        "lang": STRUCTURED_CONTENT_LANG,
                        "content": [
                            {"tag": "a", "href": record.article_url, "content": ["查看原文"]},
                        ],
                    },
                ],
            }
        ],
        record.pageid,
        "",
    ]


def alias_term_for_title(title: str) -> str | None:
    match = FULLWIDTH_ALIAS_PATTERN.fullmatch(title)
    if match is None:
        return None

    base = match.group("base").rstrip()
    if not base or base == title:
        return None
    return base


def ensure_pinyin_phrase_data_loaded() -> None:
    global _PINYIN_DATA_READY
    if _PINYIN_DATA_READY:
        return

    from pypinyin_dict.phrase_pinyin_data import large_pinyin

    large_pinyin.load()
    _PINYIN_DATA_READY = True


def reading_separator_text(text: str) -> str:
    return "".join(character for character in text if not character.isalnum() and not character.isspace())


def term_reading_for_term(term: str) -> str:
    ensure_pinyin_phrase_data_loaded()
    pieces: list[str] = []
    converted_any = False
    last_end = 0

    for match in HANZI_RUN_PATTERN.finditer(term):
        start, end = match.span()
        if start > last_end:
            separator = reading_separator_text(term[last_end:start])
            if separator:
                pieces.append(separator)

        reading_parts = lazy_pinyin(
            match.group(),
            style=Style.TONE,
            strict=False,
            neutral_tone_with_five=False,
            tone_sandhi=False,
        )
        if reading_parts:
            pieces.append(" ".join(reading_parts))
            converted_any = True
        last_end = end

    if last_end < len(term):
        separator = reading_separator_text(term[last_end:])
        if separator:
            pieces.append(separator)

    if not converted_any:
        return ""

    return normalize_reading_text(" ".join(piece for piece in pieces if piece))


def normalize_reading_text(text: str) -> str:
    normalized = READING_COLLAPSE_SPACE_PATTERN.sub(" ", text).strip()
    normalized = READING_SPACE_BEFORE_PUNCTUATION_PATTERN.sub(r"\1", normalized)
    normalized = READING_SPACE_BEFORE_OPENING_PATTERN.sub(r"\1", normalized)
    normalized = READING_SPACE_AFTER_OPENING_PATTERN.sub(r"\1", normalized)
    normalized = READING_PUNCTUATION_WITH_TRAILING_SPACE_PATTERN.sub(r"\1 ", normalized)
    return READING_COLLAPSE_SPACE_PATTERN.sub(" ", normalized).strip()


def chunked(items: list[SummaryRecord], chunk_size: int) -> list[list[SummaryRecord]]:
    return [items[index : index + chunk_size] for index in range(0, len(items), chunk_size)]
