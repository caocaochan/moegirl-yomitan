from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from zipfile import ZipFile

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
from moegirl_yomitan.models import ListedLink, ManifestPage, SummaryRecord
from moegirl_yomitan.packaging import package_dictionary


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


def test_atomic_write_text_replaces_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    path.write_text("old", encoding="utf-8")

    fetcher.atomic_write_text(path, "new")

    assert path.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.glob("record.json.*.tmp")) == []


def test_atomic_write_text_retries_transient_replace_failure(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "record.json"
    path.write_text("old", encoding="utf-8")
    original_replace = type(path).replace
    replace_calls = 0

    def flaky_replace(self: Path, target: Path) -> Path:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            raise PermissionError("temporarily locked")
        return original_replace(self, target)

    monkeypatch.setattr(type(path), "replace", flaky_replace)
    monkeypatch.setattr(fetcher.time, "sleep", lambda seconds: None)

    fetcher.atomic_write_text(path, "new")

    assert replace_calls == 2
    assert path.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.glob("record.json.*.tmp")) == []


def test_atomic_write_text_cleans_up_temp_file_after_replace_failure(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "record.json"
    path.write_text("old", encoding="utf-8")
    replace_calls = 0

    def failing_replace(self: Path, target: Path) -> Path:
        nonlocal replace_calls
        replace_calls += 1
        raise PermissionError("locked")

    monkeypatch.setattr(type(path), "replace", failing_replace)
    monkeypatch.setattr(fetcher.time, "sleep", lambda seconds: None)

    with pytest.raises(PermissionError):
        fetcher.atomic_write_text(path, "new")

    assert replace_calls == fetcher.ATOMIC_WRITE_REPLACE_ATTEMPTS
    assert path.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob("record.json.*.tmp")) == []


def test_old_record_json_loads_without_listed_links_and_is_marked_pending(tmp_path: Path) -> None:
    settings = Settings(cache_dir=tmp_path / "cache")
    record_path = record_path_for_page(settings, 1)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "pageid": 1,
                "canonical_title": "萌娘",
                "article_url": "https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98",
                "source_url": "https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98",
                "lastmod": "2026-04-28T00:00:00Z",
                "summary": "这是摘要。",
                "retrieved_at": "2026-04-28T12:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    record = fetcher.load_record(record_path)
    index = build_record_cache_index(settings)
    cached = index.by_pageid[1]

    assert record is not None
    assert record.schema_version == 1
    assert record.listed_links == []
    assert page_needs_fetch(make_page(), cached) is True


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
    cached = record_for_page(page, index)
    assert cached is not None
    assert cached.pageid == record.pageid
    assert cached.canonical_title == record.canonical_title
    assert cached.source_url == record.source_url


def test_limited_record_cache_index_reports_scan_progress(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache")
    record = make_record()
    write_record(settings, record)
    messages: list[str] = []

    page = make_page()
    page.pageid = record.pageid
    page.record_path = "records/1.json"

    monkeypatch.setattr(fetcher, "RECORD_CACHE_INDEX_PROGRESS_INTERVAL", 1)
    monkeypatch.setattr(fetcher, "log_status", messages.append)

    index = fetcher.build_record_cache_index_for_pages(settings, [page])

    assert index.count == 1
    assert any(message == "Scanning 1 page-specific record cache files for index reuse..." for message in messages)
    assert any(message.startswith("Record cache index progress: checked=1/1, usable=1") for message in messages)


def test_record_cache_index_reports_scan_and_save_progress(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache")
    write_record(settings, make_record())
    second_page = make_page_with_title("乙")
    write_record(settings, make_record_for_page(second_page, pageid=2))
    messages: list[str] = []

    monkeypatch.setattr(fetcher, "RECORD_CACHE_INDEX_PROGRESS_INTERVAL", 1)
    monkeypatch.setattr(fetcher, "log_status", messages.append)

    index = build_record_cache_index(settings)

    assert index.count == 2
    assert any(message == "Scanning 2 record cache files for index reuse..." for message in messages)
    assert any(message.startswith("Record cache index progress: checked=1/2, usable=1") for message in messages)
    assert any(message.startswith("Record cache index progress: checked=2/2, usable=2") for message in messages)
    assert any(message == "Saving record cache index with 2 cached records..." for message in messages)


def test_record_cache_index_reuses_persisted_metadata_for_unchanged_records(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache")
    record = make_record()
    write_record(settings, record)

    first_index = build_record_cache_index(settings)
    assert first_index.count == 1
    assert settings.record_cache_index_path.exists()

    def fail_load_record(record_path: Path) -> SummaryRecord | None:
        raise AssertionError("unchanged record should have been reused from persisted index")

    monkeypatch.setattr(fetcher, "load_record", fail_load_record)

    second_index = build_record_cache_index(settings)

    cached = record_for_page(make_page(), second_index)
    assert cached is not None
    assert cached.pageid == record.pageid
    assert cached.canonical_title == record.canonical_title


def test_record_cache_index_reparses_only_changed_records(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache")
    first_record = make_record()
    second_page = make_page_with_title("乙")
    second_record = make_record_for_page(second_page, pageid=2)
    write_record(settings, first_record)
    write_record(settings, second_record)
    build_record_cache_index(settings)

    changed_second_record = make_record_for_page(second_page, pageid=2, summary="这是新的较长摘要。")
    write_record(settings, changed_second_record)
    original_load_record = fetcher.load_record
    load_calls: list[Path] = []

    def tracking_load_record(record_path: Path) -> SummaryRecord | None:
        load_calls.append(record_path)
        return original_load_record(record_path)

    monkeypatch.setattr(fetcher, "load_record", tracking_load_record)

    index = build_record_cache_index(settings)

    assert load_calls == [record_path_for_page(settings, changed_second_record.pageid)]
    cached = record_for_page(second_page, index)
    assert cached is not None
    assert cached.pageid == changed_second_record.pageid


def test_malformed_record_cache_index_falls_back_to_record_files(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache")
    record = make_record()
    write_record(settings, record)
    settings.record_cache_index_path.write_text("{not-json", encoding="utf-8")
    original_load_record = fetcher.load_record
    load_calls: list[Path] = []

    def tracking_load_record(record_path: Path) -> SummaryRecord | None:
        load_calls.append(record_path)
        return original_load_record(record_path)

    monkeypatch.setattr(fetcher, "load_record", tracking_load_record)

    index = build_record_cache_index(settings)

    assert load_calls == [record_path_for_page(settings, record.pageid)]
    assert index.count == 1


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


def test_full_fetch_and_package_retain_entries_removed_from_sitemap(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        cache_dir=tmp_path / "cache",
        output_zip=tmp_path / "dist" / "moegirl.zip",
        batch_size=2,
        concurrency=1,
        sitemap_concurrency=1,
    )
    first_page = make_page_with_title("甲")
    second_page = make_page_with_title("乙")
    records_by_title = {
        first_page.title_from_url: make_record_for_page(first_page, pageid=1),
        second_page.title_from_url: make_record_for_page(second_page, pageid=2),
    }
    sitemap_index_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-0.xml</loc></sitemap>
    </sitemapindex>
    """
    sitemap_runs = [
        """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://mzh.moegirl.org.cn/甲</loc>
            <lastmod>2026-04-28T00:00:00Z</lastmod>
          </url>
          <url>
            <loc>https://mzh.moegirl.org.cn/乙</loc>
            <lastmod>2026-04-28T00:00:00Z</lastmod>
          </url>
        </urlset>
        """,
        """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://mzh.moegirl.org.cn/甲</loc>
            <lastmod>2026-04-28T00:00:00Z</lastmod>
          </url>
        </urlset>
        """,
    ]
    sitemap_run_index = 0

    monkeypatch.setattr(fetcher, "fetch_text_from_candidates", lambda *args, **kwargs: sitemap_index_xml)

    def fake_fetch_sitemap_text_with_fallback(session, url: str, settings: Settings) -> str:
        return sitemap_runs[sitemap_run_index]

    def fake_fetch_batch(settings: Settings, batch: list[ManifestPage]) -> list[SummaryRecord | None]:
        return [records_by_title[page.title_from_url] for page in batch]

    monkeypatch.setattr(fetcher, "fetch_sitemap_text_with_fallback", fake_fetch_sitemap_text_with_fallback)
    monkeypatch.setattr(fetcher, "fetch_batch", fake_fetch_batch)

    first_pages = fetch_pages(settings)
    assert {page.canonical_title for page in first_pages} == {"甲", "乙"}

    sitemap_run_index = 1
    second_pages = fetch_pages(settings)

    assert {page.canonical_title for page in second_pages} == {"甲", "乙"}
    manifest = json.loads(settings.manifest_path.read_text(encoding="utf-8"))
    assert {page["canonical_title"] for page in manifest["pages"]} == {"甲", "乙"}

    output_path = package_dictionary(settings)
    with ZipFile(output_path) as archive:
        term_data = json.loads(archive.read("term_bank_1.json").decode("utf-8"))

    assert {entry[0] for entry in term_data} == {"甲", "乙"}


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


def test_fetch_batch_populates_listed_links_for_disambiguation_summary(monkeypatch) -> None:
    settings = Settings()
    page = make_page_with_title("日向花火")
    listed_links = [
        ListedLink(title="日向花火(火影忍者)", url="https://mzh.moegirl.org.cn/日向花火(火影忍者)"),
    ]
    requested_titles: list[str] = []

    def fake_fetch_extract_payload(session, settings, titles: list[str]) -> dict:
        return {
            "query": {
                "pages": {
                    "246707": {
                        "pageid": 246707,
                        "title": titles[0],
                        "extract": "日向花火可以指：",
                    }
                }
            }
        }

    def fake_fetch_listed_links(session, settings, title: str) -> list[ListedLink]:
        requested_titles.append(title)
        return listed_links

    monkeypatch.setattr(fetcher, "fetch_extract_payload", fake_fetch_extract_payload)
    monkeypatch.setattr(fetcher, "fetch_listed_links", fake_fetch_listed_links)

    records = fetcher.fetch_batch(settings, [page])

    assert records[0] is not None
    assert records[0].listed_links == listed_links
    assert requested_titles == ["日向花火"]


def test_fetch_listed_links_filters_broad_page_links_and_preserves_list_order(monkeypatch) -> None:
    settings = Settings()
    calls: list[dict | None] = []

    def fake_fetch_listed_links_payload(session, settings, title: str, continuation=None) -> dict:
        calls.append(continuation)
        if continuation is None:
            return {
                "query": {
                    "pages": {
                        "246707": {
                            "pageid": 246707,
                            "title": title,
                            "extract": (
                                "<p><b>日向花火</b>可以指：</p>"
                                "<h2>日向花火</h2>"
                                "<ul>"
                                "<li><b>日向花火(火影忍者)</b>————岸本齐史创作的漫画《火影忍者》的登场角色。</li>"
                                "<li><b>日向花火(Tropical KISS)</b>————Twinkle制作的游戏《Tropical KISS》的登场角色。</li>"
                                "</ul>"
                            ),
                            "links": [
                                {"ns": 0, "title": "Tropical KISS"},
                                {"ns": 0, "title": "岸本齐史"},
                                {"ns": 0, "title": "日向花火(Tropical KISS)"},
                                {"ns": 0, "title": "火影忍者"},
                            ],
                        }
                    }
                },
                "continue": {"plcontinue": "246707|0|日向花火(火影忍者)", "continue": "||"},
            }
        return {
            "query": {
                "pages": {
                    "246707": {
                        "pageid": 246707,
                        "title": title,
                        "links": [
                            {"ns": 0, "title": "日向花火(火影忍者)"},
                            {"ns": 4, "title": "萌娘百科:帮助"},
                        ],
                    }
                }
            }
        }

    monkeypatch.setattr(fetcher, "fetch_listed_links_payload", fake_fetch_listed_links_payload)

    links = fetcher.fetch_listed_links(object(), settings, "日向花火")

    assert calls == [None, {"plcontinue": "246707|0|日向花火(火影忍者)", "continue": "||"}]
    assert links == [
        ListedLink(title="日向花火(火影忍者)", url="https://mzh.moegirl.org.cn/日向花火(火影忍者)"),
        ListedLink(title="日向花火(Tropical KISS)", url="https://mzh.moegirl.org.cn/日向花火(Tropical KISS)"),
    ]


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
    monkeypatch.setattr(fetcher, "fetch_text_with_curl", lambda *args, **kwargs: (_ for _ in ()).throw(requests.HTTPError("curl failed")))

    with pytest.raises(requests.HTTPError) as exc_info:
        fetcher.fetch_text_from_candidates(object(), candidates, settings)

    message = str(exc_info.value)
    assert candidates[0] in message
    assert candidates[1] in message
    assert "HTTPError status=503" in message
    assert "HTTPError status=504" in message


def test_transport_fallback_returns_valid_curl_stdout_after_requests_403(monkeypatch) -> None:
    settings = Settings()
    response = requests.Response()
    response.status_code = 403

    def fail_requests(session, url, settings, validator=None):
        raise requests.HTTPError("403 Client Error: Forbidden", response=response)

    def fake_run(args, capture_output, encoding, text, check):
        assert args[0] == "curl"
        assert "--user-agent" in args
        assert settings.user_agent in args
        return subprocess.CompletedProcess(args, 0, stdout="<sitemapindex></sitemapindex>", stderr="")

    monkeypatch.setattr(fetcher, "fetch_text_with_retry", fail_requests)
    monkeypatch.setattr(fetcher.subprocess, "run", fake_run)

    text = fetcher.fetch_text_with_transport_fallback(
        object(),
        "https://mzh.moegirl.org.cn/sitemap/sitemap-index-zhmoegirl.xml",
        settings,
        validator=lambda value: value.endswith("</sitemapindex>"),
    )

    assert text == "<sitemapindex></sitemapindex>"


def test_transport_fallback_reports_requests_and_curl_failures(monkeypatch) -> None:
    settings = Settings()
    response = requests.Response()
    response.status_code = 403

    def fail_requests(session, url, settings, validator=None):
        raise requests.HTTPError("403 Client Error: Forbidden", response=response)

    def fake_run(args, capture_output, encoding, text, check):
        return subprocess.CompletedProcess(args, 22, stdout="", stderr="curl: (22) HTTP response code said error")

    monkeypatch.setattr(fetcher, "fetch_text_with_retry", fail_requests)
    monkeypatch.setattr(fetcher.subprocess, "run", fake_run)

    with pytest.raises(requests.HTTPError) as exc_info:
        fetcher.fetch_text_with_transport_fallback(object(), "https://example.invalid/sitemap.xml", settings)

    message = str(exc_info.value)
    assert "requests=(HTTPError status=403" in message
    assert "curl=(HTTPError: curl failed while fetching https://example.invalid/sitemap.xml" in message
    assert "curl: (22) HTTP response code said error" in message


def test_fetch_text_with_curl_validates_response(monkeypatch) -> None:
    settings = Settings()

    def fake_run(args, capture_output, encoding, text, check):
        return subprocess.CompletedProcess(args, 0, stdout="<sitemapindex>", stderr="")

    monkeypatch.setattr(fetcher.subprocess, "run", fake_run)

    with pytest.raises(requests.HTTPError) as exc_info:
        fetcher.fetch_text_with_curl(
            "https://example.invalid/sitemap.xml",
            settings,
            validator=lambda value: value.endswith("</sitemapindex>"),
        )

    assert "Incomplete response while fetching https://example.invalid/sitemap.xml via curl" in str(exc_info.value)


def test_fetch_sitemap_text_with_fallback_uses_curl_for_namespace_sitemaps(monkeypatch) -> None:
    settings = Settings()
    response = requests.Response()
    response.status_code = 403
    curl_urls: list[str] = []

    def fail_requests(session, url, settings, validator=None):
        raise requests.HTTPError("403 Client Error: Forbidden", response=response)

    def fake_run(args, capture_output, encoding, text, check):
        curl_urls.append(args[-1])
        return subprocess.CompletedProcess(args, 0, stdout="<urlset></urlset>", stderr="")

    monkeypatch.setattr(fetcher, "fetch_text_with_retry", fail_requests)
    monkeypatch.setattr(fetcher.subprocess, "run", fake_run)

    text = fetcher.fetch_sitemap_text_with_fallback(
        object(),
        "https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-0.xml",
        settings,
    )

    assert text == "<urlset></urlset>"
    assert curl_urls == ["https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-0.xml"]


def test_discover_pages_with_limit_stops_after_enough_entries(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache")
    messages: list[str] = []
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
    monkeypatch.setattr(fetcher, "log_status", messages.append)

    pages = fetcher.discover_pages(settings, object(), limit=1)

    assert [page.title_from_url for page in pages] == ["萌娘"]
    assert fetched_sitemaps == ["https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-0.xml"]
    assert any(message == "Downloading sitemap index..." for message in messages)
    assert any(message == "Found 2 namespace-zero sitemap files." for message in messages)
    assert any(message.startswith("Downloading sitemap file 1/2 for limited fetch") for message in messages)
    assert any(message.startswith("Sitemap download progress: 1/2 files") for message in messages)
    assert any(message == "Reached discovery limit of 1 pages after 1/2 sitemap files." for message in messages)


def test_discover_pages_emits_parallel_sitemap_progress(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(cache_dir=tmp_path / "cache", sitemap_concurrency=2)
    messages: list[str] = []
    sitemap_index_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-0.xml</loc></sitemap>
      <sitemap><loc>https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-1.xml</loc></sitemap>
    </sitemapindex>
    """
    sitemap_xml_by_url = {
        "https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-0.xml": """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://mzh.moegirl.org.cn/%E8%90%8C%E5%A8%98</loc>
            <lastmod>2026-04-28T00:00:00Z</lastmod>
          </url>
        </urlset>
        """,
        "https://mzh.moegirl.org.cn/sitemap/sitemap-zhmoegirl-NS_0-1.xml": """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://mzh.moegirl.org.cn/%E8%88%B0%E9%98%9FCollection</loc>
            <lastmod>2026-04-28T00:00:00Z</lastmod>
          </url>
        </urlset>
        """,
    }

    monkeypatch.setattr(fetcher, "fetch_text_from_candidates", lambda *args, **kwargs: sitemap_index_xml)
    monkeypatch.setattr(
        fetcher,
        "fetch_sitemap_text_with_fallback",
        lambda session, url, settings: sitemap_xml_by_url[url],
    )
    monkeypatch.setattr(fetcher, "log_status", messages.append)

    pages = fetcher.discover_pages(settings, object())

    assert sorted(page.title_from_url for page in pages) == ["舰队Collection", "萌娘"]
    assert any(message == "Downloading sitemap index..." for message in messages)
    assert any(message == "Found 2 namespace-zero sitemap files." for message in messages)
    assert any(message == "Downloading 2 sitemap files with 2 workers..." for message in messages)
    assert any(message.startswith("Sitemap download progress: 1/2 files, workers=2") for message in messages)
    assert any(message.startswith("Sitemap download progress: 2/2 files, workers=2") for message in messages)
    assert any(message == "Parsing sitemap page entries..." for message in messages)
    assert any(message == "Merging discovered pages with existing manifest..." for message in messages)


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
    assert any(message.startswith("Fetching pending pages: 1 pages, 1 batches") for message in messages)
    assert any(message.startswith("Fetch progress:") for message in messages)
