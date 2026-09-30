from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

import pytest
import requests

from moegirl_yomitan import cli, fetcher, packaging, release_diff
from moegirl_yomitan.config import Settings
from moegirl_yomitan.models import ListedLink, ManifestPage, SummaryRecord
from moegirl_yomitan.sitemaps import canonical_article_url, merge_manifest_pages
from moegirl_yomitan.text import trim_summary


def cache_fixture(tmp_path: Path, *, limit=240, complete=False) -> Settings:
    settings = Settings(cache_dir=tmp_path / "cache", output_zip=tmp_path / "dist" / "dictionary.zip")
    page = ManifestPage("https://mzh.moegirl.org.cn/Main", "Main", "2026-09-30", "sitemap", pageid=1)
    record = SummaryRecord(
        1, "Main", page.source_url, page.source_url, page.lastmod, "abcdefghij", "2026-09-30",
        summary_char_limit=limit, summary_complete=complete,
    )
    fetcher.write_record(settings, record)
    fetcher.save_manifest(settings, [page])
    return settings


@pytest.mark.parametrize("name", [
    "summary_char_limit", "batch_size", "concurrency", "min_concurrency",
    "sitemap_concurrency", "chunk_size", "retry_attempts", "batch_retry_attempts",
])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_settings_reject_invalid_integers(name, value):
    with pytest.raises(ValueError):
        Settings(**{name: value})


@pytest.mark.parametrize("name", ["request_timeout", "backoff_base_seconds", "adaptive_backoff_cap_seconds"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_settings_reject_invalid_floats(name, value):
    with pytest.raises(ValueError):
        Settings(**{name: value})


@pytest.mark.parametrize("timeout", [(1,), (1, 0), (float("nan"), 1), (1, 2, 3)])
def test_settings_reject_invalid_timeout_pairs(timeout):
    with pytest.raises(ValueError):
        Settings(request_timeout=timeout)


def test_settings_reject_invalid_batch_and_concurrency_bounds():
    with pytest.raises(ValueError):
        Settings(batch_size=21)
    with pytest.raises(ValueError):
        Settings(concurrency=1, min_concurrency=2)
    with pytest.raises(ValueError):
        Settings(output_zip=Path("moegirl-yomitan-index.json"))


@pytest.mark.parametrize("flag", ["--limit", "--chunk-size", "--summary-char-limit", "--concurrency", "--sitemap-concurrency"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_cli_rejects_invalid_integers(flag, value):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["build", flag, value])


@pytest.mark.parametrize("flag", ["--request-timeout", "--backoff-base-seconds"])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_cli_rejects_nonfinite_floats(flag, value):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["fetch", flag, value])


def test_one_character_summary_limit():
    assert trim_summary("abc", 1) == "\u2026"


@pytest.mark.parametrize("payload", [
    {"error": {"code": "maxlag"}}, {"errors": [{"code": "badvalue"}]}, [], {},
    {"query": {"pages": []}}, {"query": {"pages": {"1": None}}},
    {"query": {"pages": {"1": {"pageid": 1, "title": "Main"}}}},
    {"query": {"pages": {"1": {"pageid": 1, "title": "Main", "extract": None}}}},
    {"query": {"pages": {}}},
])
def test_failed_api_outcomes_never_enter_negative_cache(tmp_path, monkeypatch, payload):
    settings = Settings(cache_dir=tmp_path / "cache", batch_retry_attempts=1, concurrency=1)
    page = ManifestPage("https://mzh.moegirl.org.cn/Main", "Main", "today", "map")
    monkeypatch.setattr(fetcher, "discover_pages", lambda *a, **k: [page])
    monkeypatch.setattr(fetcher, "fetch_extract_payload", lambda *a, **k: payload)
    with pytest.raises(fetcher.ApiResponseError):
        fetcher.fetch_pages(settings)
    assert fetcher.load_negative_cache(settings).by_source_url == {}
    assert fetcher.page_needs_fetch(page, None)


@pytest.mark.parametrize("page_payload", [
    {"title": "Main", "missing": ""},
    {"pageid": 1, "title": "Main", "extract": ""},
])
def test_confirmed_missing_and_empty_outcomes_are_cached(tmp_path, monkeypatch, page_payload):
    settings = Settings(cache_dir=tmp_path / "cache", concurrency=1)
    page = ManifestPage("https://mzh.moegirl.org.cn/Main", "Main", "today", "map")
    monkeypatch.setattr(fetcher, "discover_pages", lambda *a, **k: [page])
    monkeypatch.setattr(fetcher, "fetch_extract_payload", lambda *a, **k: {"query": {"pages": {"1": page_payload}}})
    fetcher.fetch_pages(settings)
    negative = fetcher.load_negative_cache(settings).by_source_url[page.source_url]
    assert not fetcher.page_needs_fetch(page, None, negative)
    assert page.fetched_lastmod == ""


def test_legacy_negative_cache_is_invalidated(tmp_path):
    settings = Settings(cache_dir=tmp_path)
    settings.negative_cache_path.write_text(json.dumps({
        "schema_version": 1,
        "entries": {"url": {"lastmod": "today", "record_schema_version": 2}},
    }), encoding="utf-8")
    index = fetcher.load_negative_cache(settings)
    assert index.by_source_url == {}
    fetcher.save_negative_cache(settings, index)
    assert json.loads(settings.negative_cache_path.read_text())["schema_version"] == 2


def test_extract_continuation_is_merged(monkeypatch):
    calls = []
    results = [
        {"query": {"pages": {
            "1": {"pageid": 1, "title": "One", "extract": "first"},
            "2": {"pageid": 2, "title": "Two"},
        }}, "continue": {"excontinue": 1, "continue": "||"}},
        {"query": {"pages": {"2": {"pageid": 2, "title": "Two", "extract": "second"}}}},
    ]
    def request(session, settings, payload):
        calls.append(dict(payload))
        return results.pop(0)
    monkeypatch.setattr(fetcher, "request_api_payload", request)
    payload = fetcher.fetch_extract_payload(object(), Settings(), ["One", "Two"])
    assert payload["query"]["pages"]["1"]["extract"] == "first"
    assert payload["query"]["pages"]["2"]["extract"] == "second"
    assert calls[0]["exlimit"] == "2"
    assert calls[1]["excontinue"] == 1


def test_repeated_extract_continuation_fails(monkeypatch):
    monkeypatch.setattr(fetcher, "request_api_payload", lambda *a: {
        "query": {"pages": {}}, "continue": {"excontinue": 0},
    })
    with pytest.raises(fetcher.ApiResponseError, match="repeated"):
        fetcher.fetch_extract_payload(object(), Settings(), ["One"])


def test_non_json_api_response_fails(monkeypatch):
    class Response:
        def json(self):
            raise ValueError("HTML challenge")
    monkeypatch.setattr(fetcher, "request_with_retry", lambda *a, **k: Response())
    with pytest.raises(fetcher.ApiResponseError, match="valid JSON"):
        fetcher.fetch_extract_payload(object(), Settings(), ["One"])


def test_packaging_failure_preserves_existing_assets(tmp_path, monkeypatch):
    settings = cache_fixture(tmp_path)
    packaging.package_dictionary(settings)
    before = [p.read_bytes() for p in (settings.output_zip, settings.output_index)]
    def fail(record):
        raise RuntimeError("serialization failed")
    monkeypatch.setattr(packaging, "build_term_entries", fail)
    with pytest.raises(RuntimeError):
        packaging.package_dictionary(settings)
    assert [p.read_bytes() for p in (settings.output_zip, settings.output_index)] == before
    assert list(settings.output_zip.parent.glob(".moegirl-build-*")) == []


@pytest.mark.parametrize("existing", [False, True])
def test_publication_failure_rolls_back_assets(tmp_path, monkeypatch, existing):
    settings = cache_fixture(tmp_path)
    if existing:
        packaging.package_dictionary(settings)
        before = [p.read_bytes() for p in (settings.output_zip, settings.output_index)]
    original = Path.replace
    def fail_index(path, target):
        if Path(target) == settings.output_index:
            raise PermissionError("index is locked")
        return original(path, target)
    monkeypatch.setattr(Path, "replace", fail_index)
    with pytest.raises(PermissionError):
        packaging.package_dictionary(settings)
    if existing:
        assert [p.read_bytes() for p in (settings.output_zip, settings.output_index)] == before
    else:
        assert not settings.output_zip.exists() and not settings.output_index.exists()


def test_blocked_rollback_preserves_recovery_files(tmp_path, monkeypatch):
    settings = cache_fixture(tmp_path)
    packaging.package_dictionary(settings)
    previous_zip = settings.output_zip.read_bytes()
    original = Path.replace
    def fail_publication_and_rollback(path, target):
        if Path(target) == settings.output_index or path.name == "previous-0":
            raise PermissionError("locked")
        return original(path, target)
    monkeypatch.setattr(Path, "replace", fail_publication_and_rollback)
    with pytest.raises(packaging.PublicationRollbackError, match="Recovery files are preserved"):
        packaging.package_dictionary(settings)
    recovery_dirs = list(settings.output_zip.parent.glob(".moegirl-build-*"))
    assert len(recovery_dirs) == 1
    assert (recovery_dirs[0] / "previous-0").read_bytes() == previous_zip


def test_staged_assets_have_identical_index_bytes(tmp_path):
    settings = cache_fixture(tmp_path)
    packaging.package_dictionary(settings)
    with ZipFile(settings.output_zip) as archive:
        assert archive.read("index.json") == settings.output_index.read_bytes()


def test_empty_build_fails_without_outputs(tmp_path):
    settings = Settings(cache_dir=tmp_path / "cache", output_zip=tmp_path / "out.zip")
    with pytest.raises(ValueError, match="No usable records"):
        packaging.package_dictionary(settings)
    assert not settings.output_zip.exists() and not settings.output_index.exists()


def test_summary_limit_changes_work_online_and_offline(tmp_path):
    settings = cache_fixture(tmp_path, limit=10)
    smaller = replace(settings, summary_char_limit=5)
    assert len(packaging.load_packaged_records(smaller)[0].summary) == 5
    larger = replace(settings, summary_char_limit=20)
    with pytest.raises(ValueError, match="run fetch"):
        packaging.package_dictionary(larger)
    page = fetcher.load_manifest(settings)[0]
    index = fetcher.build_record_cache_index(settings)
    assert fetcher.page_needs_fetch(page, fetcher.record_for_page(page, index), summary_char_limit=20)
    assert not fetcher.page_needs_fetch(page, fetcher.record_for_page(page, index), summary_char_limit=5)
    assert fetcher.load_record(fetcher.record_path_for_page(settings, 1)).summary == "abcdefghij"


def test_complete_summary_can_use_larger_limit(tmp_path):
    settings = cache_fixture(tmp_path, limit=10, complete=True)
    larger = replace(settings, summary_char_limit=100)
    assert packaging.load_packaged_records(larger)[0].summary == "abcdefghij"
    index = fetcher.build_record_cache_index(settings)
    page = fetcher.load_manifest(settings)[0]
    assert not fetcher.page_needs_fetch(page, fetcher.record_for_page(page, index), summary_char_limit=100)


def test_fetch_records_summary_limit_and_completeness(monkeypatch):
    page = ManifestPage("url", "Main", "today", "map")
    monkeypatch.setattr(fetcher, "fetch_extract_payload", lambda *a: {
        "query": {"pages": {"1": {"pageid": 1, "title": "Main", "extract": "abcdef"}}},
    })
    record = fetcher.fetch_batch_with_session(object(), Settings(summary_char_limit=3), [page])[0]
    assert record.summary_char_limit == 3
    assert not record.summary_complete
    assert len(record.summary) == 3


def test_aliases_remain_fresh_on_second_fetch(tmp_path, monkeypatch):
    settings = Settings(cache_dir=tmp_path / "cache", concurrency=1)
    calls = []
    def discover(*args, **kwargs):
        pages = [ManifestPage("https://mzh.moegirl.org.cn/" + title, title, stamp, "map")
                 for title, stamp in [("Main", "new"), ("Alias", "old")]]
        previous = {p.source_url: p for p in fetcher.load_manifest(settings)}
        return merge_manifest_pages(pages, previous)
    def batch(settings, pages):
        calls.append([p.title_from_url for p in pages])
        return [SummaryRecord(1, "Main", p.source_url, p.source_url, p.lastmod, "summary", "today") for p in pages]
    monkeypatch.setattr(fetcher, "discover_pages", discover)
    monkeypatch.setattr(fetcher, "fetch_batch", batch)
    fetcher.fetch_pages(settings)
    pages = fetcher.fetch_pages(settings)
    assert len(calls) == 1
    assert {p.fetched_lastmod for p in pages} == {"new", "old"}
    assert len(packaging.load_packaged_records(settings)) == 1


def test_known_pageid_redirect_resolves_canonical_title(monkeypatch):
    page = ManifestPage("url", "Old_title", "today", "map", pageid=1, canonical_title="Old title")
    monkeypatch.setattr(fetcher, "fetch_extract_payload", lambda *a, **k: {
        "query": {
            "redirects": [{"from": "Old title", "to": "New title"}],
            "pages": {"2": {"pageid": 2, "title": "New title", "extract": "summary"}},
        },
    })
    record = fetcher.fetch_batch_with_session(object(), Settings(), [page])[0]
    assert record.pageid == 2 and record.canonical_title == "New title"


def test_redirect_alias_refresh_uses_source_title(monkeypatch):
    pages = [
        ManifestPage("alias", "Alias", "changed", "map", pageid=1, canonical_title="Old target"),
        ManifestPage("old", "Old_target", "today", "map", pageid=1, canonical_title="Old target"),
    ]
    calls = []
    def payload(session, settings, titles, **kwargs):
        calls.append((titles, kwargs))
        return {"query": {
            "normalized": [{"from": "Old_target", "to": "Old target"}],
            "redirects": [{"from": "Alias", "to": "New target"}],
            "pages": {
                "1": {"pageid": 1, "title": "Old target", "extract": "old"},
                "2": {"pageid": 2, "title": "New target", "extract": "new"},
            },
        }}
    monkeypatch.setattr(fetcher, "fetch_extract_payload", payload)
    records = fetcher.fetch_batch_with_session(object(), Settings(), pages)
    assert calls == [(["Alias", "Old_target"], {})]
    assert [record.pageid for record in records] == [2, 1]


def test_missing_alias_does_not_reuse_another_requested_page(monkeypatch):
    pages = [
        ManifestPage("alias", "Alias", "changed", "map", pageid=1, canonical_title="Main"),
        ManifestPage("main", "Main", "today", "map", pageid=1, canonical_title="Main"),
    ]
    monkeypatch.setattr(fetcher, "fetch_extract_payload", lambda *a, **k: {"query": {"pages": {
        "-1": {"title": "Alias", "missing": ""},
        "1": {"pageid": 1, "title": "Main", "extract": "summary"},
    }}})
    records = fetcher.fetch_batch_with_session(object(), Settings(), pages)
    assert records[0] is None and records[1].pageid == 1


def test_confirmed_empty_outcome_excludes_previous_record(tmp_path, monkeypatch):
    settings = cache_fixture(tmp_path)
    page = fetcher.load_manifest(settings)[0]
    page.lastmod = "changed"
    monkeypatch.setattr(fetcher, "discover_pages", lambda *a, **k: [page])
    monkeypatch.setattr(fetcher, "fetch_extract_payload", lambda *a, **k: {
        "query": {"pages": {"1": {"pageid": 1, "title": "Main", "extract": ""}}},
    })
    fetcher.fetch_pages(settings)
    index = fetcher.build_record_cache_index(settings)
    negative = fetcher.load_negative_cache(settings).by_source_url[page.source_url]
    assert not fetcher.page_needs_fetch(page, fetcher.record_for_page(page, index), negative)
    assert packaging.load_packaged_records(settings) == []
    fetcher.record_path_for_page(settings, 1).write_text("{bad", encoding="utf-8")
    assert packaging.load_packaged_records(settings) == []


def test_record_pageid_must_match_cache_filename(tmp_path):
    settings = cache_fixture(tmp_path)
    path = fetcher.record_path_for_page(settings, 1)
    payload = json.loads(path.read_text())
    payload["pageid"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert fetcher.load_record(path) is None
    with pytest.raises(ValueError, match="filename"):
        packaging.package_dictionary(settings)


def test_corrupt_reference_preserves_existing_outputs(tmp_path):
    settings = cache_fixture(tmp_path)
    packaging.package_dictionary(settings)
    before = [p.read_bytes() for p in (settings.output_zip, settings.output_index)]
    fetcher.record_path_for_page(settings, 1).write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match="Unusable cached record"):
        packaging.package_dictionary(settings)
    assert [p.read_bytes() for p in (settings.output_zip, settings.output_index)] == before


def test_legacy_records_keep_default_cache_compatibility(tmp_path):
    settings = cache_fixture(tmp_path, limit=None)
    index = fetcher.build_record_cache_index(settings)
    page = fetcher.load_manifest(settings)[0]
    assert not fetcher.page_needs_fetch(page, fetcher.record_for_page(page, index))
    assert packaging.load_packaged_records(settings)
    with pytest.raises(ValueError, match="run fetch"):
        packaging.load_packaged_records(replace(settings, summary_char_limit=500))


@pytest.mark.parametrize("contents", ["{truncated", "[]", '{"pageid": "bad"}', '{"pageid": 1}'])
def test_bad_record_is_refetched_but_blocks_packaging(tmp_path, monkeypatch, contents):
    settings = cache_fixture(tmp_path)
    path = fetcher.record_path_for_page(settings, 1)
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="Unusable cached record"):
        packaging.package_dictionary(settings)
    assert not settings.output_zip.exists()
    monkeypatch.setattr(fetcher, "discover_pages", lambda *a, **k: fetcher.load_manifest(settings))
    monkeypatch.setattr(fetcher, "fetch_batch", lambda s, pages: [
        SummaryRecord(1, "Main", p.source_url, p.source_url, p.lastmod, "repaired", "today") for p in pages
    ])
    fetcher.fetch_pages(settings)
    assert packaging.load_packaged_records(settings)[0].summary == "repaired"


def test_missing_referenced_record_blocks_packaging(tmp_path):
    settings = cache_fixture(tmp_path)
    fetcher.record_path_for_page(settings, 1).unlink()
    with pytest.raises(ValueError, match="Missing cached record"):
        packaging.package_dictionary(settings)


def test_summary_and_dependency_changes_invalidate_fingerprints(tmp_path, monkeypatch):
    settings = cache_fixture(tmp_path)
    fingerprint = packaging.build_dictionary_content_fingerprint(settings)
    packaging.save_build_state(settings, fingerprint)
    smaller = replace(settings, summary_char_limit=3)
    assert packaging.build_dictionary_content_fingerprint(smaller) != fingerprint
    monkeypatch.setattr(packaging, "pinyin_dependency_versions", lambda: {"pypinyin": "changed", "pypinyin-dict": "changed"})
    result = packaging.build_dictionary_content_fingerprint_result(settings)
    assert result.recomputed_records == 1 and result.reused_records == 0
    assert result.fingerprint != fingerprint


@pytest.mark.parametrize("title", ["A?B", "A#B", "A%B", "? HEARTBEAT", "\u65e5\u5411(One Two)"])
def test_article_urls_encode_titles_as_paths(title):
    split = urlsplit(canonical_article_url(title))
    assert split.query == split.fragment == ""
    assert unquote(split.path[1:]) == title.replace(" ", "_")


def test_release_diff_uses_original_article_link(tmp_path):
    settings = cache_fixture(tmp_path)
    record = packaging.load_packaged_records(settings)[0]
    record.listed_links = [ListedLink("Other", "https://mzh.moegirl.org.cn/Other")]
    entry = release_diff.release_entry_from_term_entry(packaging.build_term_entry(record))
    assert entry.article_url == record.article_url


class MockSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def response(status, retry_after=None):
    result = requests.Response()
    result.status_code = status
    result._content = b""
    result._content_consumed = True
    if retry_after is not None:
        result.headers["Retry-After"] = retry_after
    return result


@pytest.mark.parametrize("status", [400, 403, 413, 414, 431, 404])
def test_permanent_http_failures_are_not_retried(status, monkeypatch):
    session = MockSession([response(status)])
    sleeps = []
    monkeypatch.setattr(fetcher.time, "sleep", sleeps.append)
    with pytest.raises(requests.HTTPError):
        fetcher.request_with_retry(session, "url", Settings())
    assert len(session.calls) == 1 and sleeps == []


@pytest.mark.parametrize("outcome", [requests.ConnectionError("offline"), response(503), response(429, "7")])
def test_transient_failures_retry_and_respect_retry_after(outcome, monkeypatch):
    session = MockSession([outcome, response(200)])
    sleeps = []
    monkeypatch.setattr(fetcher.time, "sleep", sleeps.append)
    assert fetcher.request_with_retry(session, "url", Settings()).status_code == 200
    assert len(session.calls) == 2
    assert sleeps == ([7.0] if isinstance(outcome, requests.Response) and outcome.status_code == 429 else [1.0])


def test_retry_after_http_date():
    value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    assert 28 <= fetcher.retry_after_seconds(value) <= 30
    assert fetcher.retry_after_seconds("garbage") == 0


def test_release_pagination_and_timeout(monkeypatch):
    calls = []
    class Session:
        def get(self, url, **kwargs):
            calls.append(kwargs)
            result = response(200)
            result.json = lambda: [{"tag_name": str(i)} for i in range(100)] if len(calls) == 1 else [{"tag_name": "last"}]
            return result
    releases = release_diff.fetch_github_releases(Session())
    assert len(releases) == 101
    assert [call["params"]["page"] for call in calls] == ["1", "2"]
    assert all(call["timeout"] == release_diff.REQUEST_TIMEOUT for call in calls)


def test_release_asset_download_has_timeout():
    calls = []
    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            result = response(200)
            result._content = b"archive"
            return result
    asset = release_diff.ReleaseAsset("2026.09.30", "page", "download")
    assert release_diff.download_release_asset(Session(), asset) == b"archive"
    assert calls == [("download", {"timeout": release_diff.REQUEST_TIMEOUT})]


def test_first_release_diff_uses_empty_baseline(tmp_path, monkeypatch):
    settings = cache_fixture(tmp_path)
    packaging.package_dictionary(settings)
    monkeypatch.setattr(release_diff, "fetch_github_releases", lambda session: [])
    html = release_diff.build_release_diff_html(head_version="2026.09.30", head_zip=settings.output_zip)
    assert "First dictionary release" in html and "Added entries: 1" in html


def test_diff_cli_writes_utf8_file(tmp_path, monkeypatch, capsys):
    html = '<html>\u840c\u5a18</html>'
    monkeypatch.setattr(cli, "build_release_diff_html", lambda **kwargs: html)
    target = tmp_path / "nested" / "diff.html"
    assert cli.main(["diff-releases", "--head-version", "2026.09.30", "--head-zip", "head.zip", "--output", str(target)]) == 0
    assert target.read_bytes() == html.encode("utf-8")
    assert capsys.readouterr().out == ""


def test_vendored_schema_provenance():
    root = Path(__file__).parent / "fixtures" / "yomitan"
    provenance = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
    assert len(provenance["commit"]) == 40
    for name, metadata in provenance["files"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == metadata["sha256"]
        assert provenance["commit"] in metadata["url"]
