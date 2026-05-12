from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import requests

from moegirl_yomitan.config import Settings
from moegirl_yomitan import fetcher
from moegirl_yomitan.fetcher import (
    build_record_cache_index,
    fetch_pages,
    hydrate_pages_from_record_cache,
    page_needs_fetch,
    record_for_page,
    record_path_for_page,
    save_manifest,
    write_record,
)
from moegirl_yomitan.models import ManifestPage, SummaryRecord


@pytest.fixture(autouse=True)
def clear_thread_local_sessions() -> None:
    fetcher.close_thread_session(fetcher.SESSION_POOL_BATCH)
    fetcher.close_thread_session(fetcher.SESSION_POOL_SITEMAP)
    yield
    fetcher.close_thread_session(fetcher.SESSION_POOL_BATCH)
    fetcher.close_thread_session(fetcher.SESSION_POOL_SITEMAP)


def make_page(lastmod: str = "2026-04-28T00:00:00Z") -> ManifestPage:
    return make_page_with_title("萌娘", lastmod=lastmod)


def make_page_with_title(title: str, lastmod: str = "2026-04-28T00:00:00Z") -> ManifestPage:
    return ManifestPage(
        source_url=f"https://mzh.moegirl.org.cn/{title}",
        title_from_url=title,
        lastmod=lastmod,
        sitemap_url="https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-1.xml",
    )


def make_record(lastmod: str = "2026-04-28T00:00:00Z", summary: str = "这是摘要。") -> SummaryRecord:
    return SummaryRecord(
        pageid=1,
        canonical_title="萌娘",
        article_url="https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98",
        source_url="https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98",
        lastmod=lastmod,
        summary=summary,
        retrieved_at="2026-04-28T12:00:00+00:00",
    )


def make_record_for_page(page: ManifestPage, pageid: int, summary: str = "这是摘要。") -> SummaryRecord:
    return SummaryRecord(
        pageid=pageid,
        canonical_title=page.title_from_url,
        article_url=page.source_url,
        source_url=page.source_url,
        lastmod=page.lastmod,
        summary=summary,
        retrieved_at="2026-04-28T12:00:00+00:00",
    )


def test_hydrate_pages_from_record_cache_repairs_incomplete_manifest_entry(tmp_path: Path) -> None:
    settings = Settings(cache_dir=tmp_path / "cache")
    record = make_record()
    write_record(settings, record)

    page = make_page()
    index = build_record_cache_index(settings)

    hydrated = hydrate_pages_from_record_cache(settings, [page], index)

    assert hydrated == 1
    assert page.pageid == record.pageid
    assert page.canonical_title == record.canonical_title
    assert page.article_url == record.article_url
    assert page.record_path == "records/1.json"


def test_page_with_stale_cached_record_is_marked_pending(tmp_path: Path) -> None:
    settings = Settings(cache_dir=tmp_path / "cache")
    write_record(settings, make_record(lastmod="2026-04-27T00:00:00Z"))

    page = make_page(lastmod="2026-04-28T00:00:00Z")
    index = build_record_cache_index(settings)
    hydrate_pages_from_record_cache(settings, [page], index)

    assert page_needs_fetch(page, record_for_page(page, index)) is True


def test_write_record_skips_unchanged_payload(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache")
    record = make_record()
    write_record(settings, record)

    atomic_calls: list[Path] = []

    def fake_atomic_write(path: Path, content: str) -> None:
        atomic_calls.append(path)

    monkeypatch.setattr(fetcher, "atomic_write_text", fake_atomic_write)

    write_record(settings, record)

    assert atomic_calls == []


def test_write_record_rewrites_changed_payload(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache")
    original = make_record(summary="旧摘要。")
    updated = make_record(summary="新摘要。")
    write_record(settings, original)

    original_atomic_write = fetcher.atomic_write_text
    atomic_calls: list[Path] = []

    def tracking_atomic_write(path: Path, content: str) -> None:
        atomic_calls.append(path)
        original_atomic_write(path, content)

    monkeypatch.setattr(fetcher, "atomic_write_text", tracking_atomic_write)

    write_record(settings, updated)

    assert atomic_calls == [record_path_for_page(settings, updated.pageid)]
    stored = json.loads(record_path_for_page(settings, updated.pageid).read_text(encoding="utf-8"))
    assert stored["summary"] == "新摘要。"


def test_limited_record_cache_index_loads_only_referenced_records(tmp_path: Path) -> None:
    settings = Settings(cache_dir=tmp_path / "cache")
    record = make_record()
    write_record(settings, record)
    settings.records_dir.joinpath("999.json").write_text("{not-json", encoding="utf-8")

    page = make_page()
    page.pageid = record.pageid
    page.record_path = "records/1.json"

    index = fetcher.build_record_cache_index_for_pages(settings, [page])

    assert index.count == 1
    assert record_for_page(page, index) == record


def test_fetch_pages_reuses_cached_record_when_manifest_is_incomplete(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache", batch_size=1, concurrency=1)
    initial_record = make_record()

    def discovered_page() -> list[ManifestPage]:
        return [make_page()]

    fetch_calls: list[list[ManifestPage]] = []

    def fake_discover_pages(settings: Settings, session, limit=None) -> list[ManifestPage]:
        return discovered_page()

    def fake_fetch_batch(settings: Settings, batch: list[ManifestPage]) -> list[SummaryRecord | None]:
        fetch_calls.append(batch)
        return [initial_record]

    monkeypatch.setattr(fetcher, "discover_pages", fake_discover_pages)
    monkeypatch.setattr(fetcher, "fetch_batch", fake_fetch_batch)

    first_pages = fetch_pages(settings, limit=1)
    assert len(fetch_calls) == 1
    assert first_pages[0].pageid == initial_record.pageid

    manifest = json.loads(settings.manifest_path.read_text(encoding="utf-8"))
    for page in manifest["pages"]:
        page["pageid"] = None
        page["canonical_title"] = None
        page["article_url"] = None
        page["record_path"] = None
    settings.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def fail_fetch_batch(settings: Settings, batch: list[ManifestPage]) -> list[SummaryRecord | None]:
        raise AssertionError("cached record should have prevented a refetch")

    monkeypatch.setattr(fetcher, "fetch_batch", fail_fetch_batch)

    second_pages = fetch_pages(settings, limit=1)

    assert second_pages[0].pageid == initial_record.pageid
    progress = json.loads(settings.manifest_path.read_text(encoding="utf-8"))["progress"]
    assert progress["pages_pending_fetch"] == 0


def test_fetch_pages_second_run_keeps_cached_record_mtime(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache", batch_size=1, concurrency=1)
    initial_record = make_record()

    def fake_discover_pages(settings: Settings, session, limit=None) -> list[ManifestPage]:
        return [make_page()]

    fetch_calls = 0

    def fake_fetch_batch(settings: Settings, batch: list[ManifestPage]) -> list[SummaryRecord | None]:
        nonlocal fetch_calls
        fetch_calls += 1
        return [initial_record]

    monkeypatch.setattr(fetcher, "discover_pages", fake_discover_pages)
    monkeypatch.setattr(fetcher, "fetch_batch", fake_fetch_batch)

    fetch_pages(settings, limit=1)
    record_path = record_path_for_page(settings, initial_record.pageid)
    first_mtime = record_path.stat().st_mtime_ns

    time.sleep(0.01)

    def fail_fetch_batch(settings: Settings, batch: list[ManifestPage]) -> list[SummaryRecord | None]:
        raise AssertionError("identical second run should not refetch")

    monkeypatch.setattr(fetcher, "fetch_batch", fail_fetch_batch)

    fetch_pages(settings, limit=1)

    assert fetch_calls == 1
    assert record_path.stat().st_mtime_ns == first_mtime


def test_fetch_extract_payload_uses_post(monkeypatch) -> None:
    settings = Settings()
    captured: dict[str, object] = {}

    class DummyResponse:
        def json(self) -> dict:
            return {"query": {"pages": {}}}

    def fake_request_with_retry(session, url, settings, method="GET", params=None, data=None):
        captured["method"] = method
        captured["params"] = params
        captured["data"] = data
        return DummyResponse()

    monkeypatch.setattr(fetcher, "request_with_retry", fake_request_with_retry)

    payload = fetcher.fetch_extract_payload(object(), settings, ["萌娘", "舰队Collection"])

    assert payload == {"query": {"pages": {}}}
    assert captured["method"] == "POST"
    assert captured["params"] is None
    assert captured["data"] is not None
    assert captured["data"]["titles"] == "萌娘|舰队Collection"


def test_fetch_text_from_candidates_reports_all_failures(monkeypatch) -> None:
    settings = Settings()
    candidates = [
        "https://mzh.moegirl.org.cn/sitemap/sitemap-index-zhmoegirl.xml",
        "https://zh.moegirl.org.cn/sitemap/sitemap-index-zhmoegirl.xml",
    ]

    def fake_fetch_text_with_retry(session, url, settings, validator=None):
        response = requests.Response()
        response.status_code = 503 if "mzh." in url else 504
        raise requests.HTTPError(f"failed {url}", response=response)

    monkeypatch.setattr(fetcher, "fetch_text_with_retry", fake_fetch_text_with_retry)

    with pytest.raises(requests.HTTPError) as exc_info:
        fetcher.fetch_text_from_candidates(object(), candidates, settings)

    message = str(exc_info.value)
    assert candidates[0] in message
    assert candidates[1] in message
    assert "HTTPError status=503" in message
    assert "HTTPError status=504" in message


def test_discover_pages_with_limit_stops_after_enough_entries(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache")
    sitemap_index_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-0.xml</loc></sitemap>
      <sitemap><loc>https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-1.xml</loc></sitemap>
    </sitemapindex>
    """
    first_sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url>
        <loc>https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98</loc>
        <lastmod>2026-04-28T00:00:00Z</lastmod>
      </url>
    </urlset>
    """
    fetched_sitemaps: list[str] = []

    monkeypatch.setattr(fetcher, "fetch_text_from_candidates", lambda *args, **kwargs: sitemap_index_xml)

    def fake_fetch_sitemap_text_with_fallback(session, url: str, settings: Settings) -> str:
        fetched_sitemaps.append(url)
        return first_sitemap_xml

    monkeypatch.setattr(fetcher, "fetch_sitemap_text_with_fallback", fake_fetch_sitemap_text_with_fallback)
    monkeypatch.setattr(
        fetcher,
        "fetch_sitemaps_in_parallel",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("limited discovery should not fetch in parallel")),
    )

    pages = fetcher.discover_pages(settings, object(), limit=1)

    assert [page.title_from_url for page in pages] == ["萌娘"]
    assert fetched_sitemaps == ["https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-0.xml"]


def test_fetch_batch_splits_oversized_requests_and_preserves_order(monkeypatch) -> None:
    settings = Settings()
    pages = [make_page_with_title(title) for title in ("甲", "乙", "丙", "丁")]
    observed_calls: list[tuple[str, ...]] = []

    def fake_fetch_extract_payload(session, settings, titles: list[str]) -> dict:
        observed_calls.append(tuple(titles))
        if len(titles) > 2:
            response = requests.Response()
            response.status_code = 414
            raise requests.HTTPError("URI too large", response=response)
        return {
            "query": {
                "pages": {
                    str(index): {
                        "pageid": index,
                        "title": title,
                        "extract": f"{title} 摘要",
                    }
                    for index, title in enumerate(titles, start=1)
                }
            }
        }

    monkeypatch.setattr(fetcher, "fetch_extract_payload", fake_fetch_extract_payload)

    records = fetcher.fetch_batch(settings, pages)

    assert [record.canonical_title if record else None for record in records] == ["甲", "乙", "丙", "丁"]
    assert observed_calls == [("甲", "乙", "丙", "丁"), ("甲", "乙"), ("丙", "丁")]


def test_fetch_batch_split_preserves_none_placeholders(monkeypatch) -> None:
    settings = Settings()
    pages = [make_page_with_title(title) for title in ("甲", "乙", "丙", "丁")]

    def fake_fetch_extract_payload(session, settings, titles: list[str]) -> dict:
        if len(titles) > 2:
            response = requests.Response()
            response.status_code = 414
            raise requests.HTTPError("URI too large", response=response)

        pages_payload: dict[str, dict] = {}
        for index, title in enumerate(titles, start=1):
            if title == "乙":
                continue
            extract = "" if title == "丁" else f"{title} 摘要"
            pages_payload[str(index)] = {
                "pageid": index,
                "title": title,
                "extract": extract,
            }
        return {"query": {"pages": pages_payload}}

    monkeypatch.setattr(fetcher, "fetch_extract_payload", fake_fetch_extract_payload)

    records = fetcher.fetch_batch(settings, pages)

    assert [record.canonical_title if record else None for record in records] == ["甲", None, "丙", None]


def test_fetch_batch_single_page_oversize_error_is_raised(monkeypatch) -> None:
    settings = Settings()
    page = make_page_with_title("超长标题")

    def fake_fetch_extract_payload(session, settings, titles: list[str]) -> dict:
        response = requests.Response()
        response.status_code = 414
        raise requests.HTTPError("URI too large", response=response)

    monkeypatch.setattr(fetcher, "fetch_extract_payload", fake_fetch_extract_payload)

    with pytest.raises(requests.HTTPError):
        fetcher.fetch_batch(settings, [page])


def test_fetch_batch_reuses_one_session_per_thread(monkeypatch) -> None:
    settings = Settings()
    created_sessions: list[object] = []

    class DummySession:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    def fake_build_session(settings: Settings) -> DummySession:
        session = DummySession()
        created_sessions.append(session)
        return session

    def fake_fetch_extract_payload(session, settings, titles: list[str]) -> dict:
        return {
            "query": {
                "pages": {
                    "1": {
                        "pageid": 1,
                        "title": titles[0],
                        "extract": f"{titles[0]} 摘要",
                    }
                }
            }
        }

    monkeypatch.setattr(fetcher, "build_session", fake_build_session)
    monkeypatch.setattr(fetcher, "fetch_extract_payload", fake_fetch_extract_payload)

    first = fetcher.fetch_batch(settings, [make_page_with_title("甲")])
    second = fetcher.fetch_batch(settings, [make_page_with_title("乙")])

    assert [record.canonical_title if record else None for record in first] == ["甲"]
    assert [record.canonical_title if record else None for record in second] == ["乙"]
    assert len(created_sessions) == 1
    assert created_sessions[0].close_calls == 0

    fetcher.close_thread_session(fetcher.SESSION_POOL_BATCH)

    assert created_sessions[0].close_calls == 1


def test_run_adaptive_fetch_loop_closes_batch_worker_sessions(monkeypatch) -> None:
    settings = Settings(cache_dir=Path("unused-cache"), batch_size=1, concurrency=1)
    pages = [make_page_with_title("甲"), make_page_with_title("乙")]
    batches = [[pages[0]], [pages[1]]]
    created_sessions: list[object] = []

    class DummySession:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    def fake_build_session(settings: Settings) -> DummySession:
        session = DummySession()
        created_sessions.append(session)
        return session

    def fake_fetch_batch_with_session(session, settings: Settings, batch: list[ManifestPage]) -> list[SummaryRecord | None]:
        page = batch[0]
        pageid = 1 if page.title_from_url == "甲" else 2
        return [make_record_for_page(page, pageid=pageid)]

    progress_state = fetcher.FetchProgressState(
        cached_records_seen=0,
        pages_hydrated_from_records=0,
        pending_pages_remaining=len(pages),
        fetch_started_at=time.monotonic(),
        last_checkpoint_at=time.monotonic(),
    )

    monkeypatch.setattr(fetcher, "build_session", fake_build_session)
    monkeypatch.setattr(fetcher, "fetch_batch_with_session", fake_fetch_batch_with_session)
    monkeypatch.setattr(fetcher, "write_record", lambda settings, record: None)
    monkeypatch.setattr(fetcher, "save_manifest_checkpoint", lambda *args, **kwargs: None)

    fetcher.run_adaptive_fetch_loop(
        settings,
        pages,
        batches,
        progress_state,
        fetcher.RecordCacheIndex({}, {}, {}, 0),
    )

    assert len(created_sessions) == 1
    assert created_sessions[0].close_calls == 1


def test_fetch_sitemap_worker_reuses_one_session_per_thread(monkeypatch) -> None:
    settings = Settings()
    created_sessions: list[object] = []

    class DummySession:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    def fake_build_session(settings: Settings) -> DummySession:
        session = DummySession()
        created_sessions.append(session)
        return session

    monkeypatch.setattr(fetcher, "build_session", fake_build_session)
    monkeypatch.setattr(fetcher, "fetch_sitemap_text_with_fallback", lambda session, url, settings: f"<xml>{url}</xml>")

    first = fetcher.fetch_sitemap_worker(settings, "https://example.invalid/sitemap-1.xml")
    second = fetcher.fetch_sitemap_worker(settings, "https://example.invalid/sitemap-2.xml")

    assert first == "<xml>https://example.invalid/sitemap-1.xml</xml>"
    assert second == "<xml>https://example.invalid/sitemap-2.xml</xml>"
    assert len(created_sessions) == 1
    assert created_sessions[0].close_calls == 0

    fetcher.close_thread_session(fetcher.SESSION_POOL_SITEMAP)

    assert created_sessions[0].close_calls == 1


def test_fetch_sitemaps_in_parallel_closes_worker_sessions(monkeypatch) -> None:
    settings = Settings(sitemap_concurrency=1)
    created_sessions: list[object] = []

    class DummySession:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    def fake_build_session(settings: Settings) -> DummySession:
        session = DummySession()
        created_sessions.append(session)
        return session

    monkeypatch.setattr(fetcher, "build_session", fake_build_session)
    monkeypatch.setattr(fetcher, "fetch_sitemap_text_with_fallback", lambda session, url, settings: f"<xml>{url}</xml>")

    results = fetcher.fetch_sitemaps_in_parallel(
        settings,
        [
            "https://example.invalid/sitemap-1.xml",
            "https://example.invalid/sitemap-2.xml",
        ],
    )

    assert results == {
        "https://example.invalid/sitemap-1.xml": "<xml>https://example.invalid/sitemap-1.xml</xml>",
        "https://example.invalid/sitemap-2.xml": "<xml>https://example.invalid/sitemap-2.xml</xml>",
    }
    assert len(created_sessions) == 1
    assert created_sessions[0].close_calls == 1


def test_fetch_pages_throttles_manifest_checkpoints(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache", batch_size=1, concurrency=1)
    pages = [make_page_with_title(f"page-{index}") for index in range(101)]
    saved_progress: list[dict[str, int]] = []

    monkeypatch.setattr(fetcher, "CHECKPOINT_INTERVAL_SECONDS", 10_000.0)
    monkeypatch.setattr(fetcher, "discover_pages", lambda settings, session, limit=None: pages)
    monkeypatch.setattr(fetcher, "save_manifest", lambda settings, pages, progress=None: saved_progress.append(progress or {}))
    monkeypatch.setattr(fetcher, "write_record", lambda settings, record: None)
    monkeypatch.setattr(fetcher, "log_status", lambda message: None)

    def fake_fetch_batch(settings: Settings, batch: list[ManifestPage]) -> list[SummaryRecord | None]:
        return [make_record_for_page(batch[0], pageid=int(batch[0].title_from_url.split("-")[-1]) + 1)]

    monkeypatch.setattr(fetcher, "fetch_batch", fake_fetch_batch)

    fetch_pages(settings, limit=len(pages))

    assert len(saved_progress) == 3
    assert saved_progress[0]["batches_completed"] == 0
    assert saved_progress[1]["batches_completed"] == 100
    assert saved_progress[2]["batches_completed"] == 101
    assert saved_progress[2]["pages_pending_fetch"] == 0


def test_fetch_pages_final_checkpoint_is_written(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache", batch_size=1, concurrency=1)
    page = make_page()
    saved_progress: list[dict[str, int]] = []

    monkeypatch.setattr(fetcher, "CHECKPOINT_INTERVAL_SECONDS", 10_000.0)
    monkeypatch.setattr(fetcher, "discover_pages", lambda settings, session, limit=None: [page])
    monkeypatch.setattr(fetcher, "save_manifest", lambda settings, pages, progress=None: saved_progress.append(progress or {}))
    monkeypatch.setattr(fetcher, "write_record", lambda settings, record: None)
    monkeypatch.setattr(fetcher, "log_status", lambda message: None)
    monkeypatch.setattr(fetcher, "fetch_batch", lambda settings, batch: [make_record_for_page(batch[0], pageid=1)])

    fetch_pages(settings, limit=1)

    assert len(saved_progress) == 2
    assert saved_progress[-1]["batches_completed"] == 1
    assert saved_progress[-1]["pages_pending_fetch"] == 0


def test_fetch_pages_does_not_rebuild_record_index_after_fetch(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache", batch_size=1, concurrency=1)
    page = make_page()
    build_calls = 0

    monkeypatch.setattr(fetcher, "discover_pages", lambda settings, session, limit=None: [page])
    monkeypatch.setattr(fetcher, "save_manifest", lambda settings, pages, progress=None: None)
    monkeypatch.setattr(fetcher, "write_record", lambda settings, record: None)
    monkeypatch.setattr(fetcher, "log_status", lambda message: None)
    monkeypatch.setattr(fetcher, "fetch_batch", lambda settings, batch: [make_record_for_page(batch[0], pageid=1)])

    def fake_build_record_cache_index(settings: Settings) -> fetcher.RecordCacheIndex:
        nonlocal build_calls
        build_calls += 1
        return fetcher.RecordCacheIndex({}, {}, {}, 0)

    monkeypatch.setattr(fetcher, "build_record_cache_index", fake_build_record_cache_index)

    fetch_pages(settings)

    assert build_calls == 1


def test_fetch_pages_progress_counters_match_written_records(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache", batch_size=2, concurrency=1)
    pages = [make_page_with_title("甲"), make_page_with_title("乙")]
    saved_progress: list[dict[str, int]] = []

    monkeypatch.setattr(fetcher, "discover_pages", lambda settings, session, limit=None: pages)
    monkeypatch.setattr(fetcher, "save_manifest", lambda settings, pages, progress=None: saved_progress.append(progress or {}))
    monkeypatch.setattr(fetcher, "write_record", lambda settings, record: None)
    monkeypatch.setattr(fetcher, "log_status", lambda message: None)
    monkeypatch.setattr(
        fetcher,
        "fetch_batch",
        lambda settings, batch: [make_record_for_page(batch[0], pageid=10), None],
    )

    fetch_pages(settings, limit=2)

    final_progress = saved_progress[-1]
    assert final_progress["cached_records_seen"] == 1
    assert final_progress["batches_completed"] == 1
    assert final_progress["records_fetched"] == 1
    assert final_progress["pages_pending_fetch"] == 0


def test_fetch_pages_emits_periodic_status_updates(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache", batch_size=1, concurrency=1)
    page = make_page()
    messages: list[str] = []

    monkeypatch.setattr(fetcher, "discover_pages", lambda settings, session, limit=None: [page])
    monkeypatch.setattr(fetcher, "save_manifest", lambda settings, pages, progress=None: None)
    monkeypatch.setattr(fetcher, "write_record", lambda settings, record: None)
    monkeypatch.setattr(fetcher, "log_status", messages.append)
    monkeypatch.setattr(fetcher, "fetch_batch", lambda settings, batch: [make_record_for_page(batch[0], pageid=1)])

    result = fetch_pages(settings, limit=1)

    assert result[0].pageid == 1
    assert any(message.startswith("Discovering sitemap pages") for message in messages)
    assert any(message.startswith("Loading record cache index") for message in messages)
    assert any(message.startswith("Fetching 1 pending pages across 1 batches") for message in messages)
    assert any(message.startswith("Fetch progress:") for message in messages)
