from concurrent.futures import Future
from dataclasses import replace
import json

import pytest
import requests

from moegirl_yomitan import fetcher as f
from moegirl_yomitan.config import Settings
from moegirl_yomitan.models import ManifestPage
from moegirl_yomitan.scheduler import (
    ApiJob, BatchCompletion, ConcurrencyController, EntryScheduler, PermanentApiError,
    ThrottledApiError, api_attempt, extract_job,
)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ImmediateExecutor:
    """Completed futures make scheduling order and virtual time deterministic."""
    def __init__(self, max_workers):
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future


def page(title):
    return ManifestPage("https://mzh.moegirl.org.cn/" + title, title, "today", "map")


def payload(title="A", pageid=1, extract="summary", **fields):
    return {"query": {"pages": {str(pageid): {
        "pageid": pageid, "title": title, "extract": extract, **fields}}}}


def http_error(status, retry_after=None):
    response = requests.Response()
    response.status_code = status
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    return requests.HTTPError(f"HTTP {status}", response=response)


def runner(tmp_path, monkeypatch, pages, attempt, *, clock=None, **options):
    clock = clock or Clock()
    settings = Settings(cache_dir=tmp_path / "cache", batch_size=1, concurrency=1, **options)
    batches = [pages[i:i + settings.batch_size] for i in range(0, len(pages), settings.batch_size)]
    progress = f.FetchProgressState(0, 0, len(pages), clock(), clock(), initial_pending_pages=len(pages))
    def checkpoint(settings, pages, progress, **kwargs):
        if clock() - progress.last_checkpoint_at >= f.CHECKPOINT_INTERVAL_SECONDS:
            progress.last_checkpoint_at = clock()
    monkeypatch.setattr(f, "save_manifest_checkpoint", checkpoint)
    scheduler = EntryScheduler(settings, pages, batches, progress, f.RecordCacheIndex({}, {}, {}),
                               f.NegativeCacheIndex(), clock=clock, jitter=lambda: 1.0,
                               idle_wait=clock.advance, executor_factory=ImmediateExecutor,
                               wait_for=lambda futures, **kwargs: ([next(iter(futures))], []), attempt=attempt)
    return scheduler, clock


def test_backoff_releases_worker_and_fallback_is_preferred(tmp_path, monkeypatch):
    calls = []
    def attempt(settings, url, data):
        calls.append((url, data["titles"]))
        if len(calls) == 1:
            raise http_error(503)
        return payload(data["titles"], 1 if data["titles"] == "A" else 2)
    scheduler, clock = runner(tmp_path, monkeypatch, [page("A"), page("B")], attempt)
    scheduler.run()
    assert [title for _, title in calls] == ["A", "B", "A"]
    assert "zh.moegirl" in calls[-1][0] and "mzh.moegirl" not in calls[-1][0]
    assert clock() == 1.0
    assert scheduler.preferred_endpoint == calls[-1][0]
    assert scheduler.progress.pending_pages_remaining == 0


def test_attempt_budget_covers_hosts_and_never_multiplies(tmp_path, monkeypatch):
    calls = []
    def attempt(settings, url, data):
        calls.append(url)
        raise requests.Timeout("slow")
    scheduler, clock = runner(tmp_path, monkeypatch, [page("A")], attempt, retry_attempts=3,
                              batch_retry_attempts=20)
    with pytest.raises(requests.Timeout):
        scheduler.run()
    assert len(calls) == 3 and len(set(calls)) == 2
    assert clock() == 3.0
    assert not scheduler.negative_index.by_source_url
    assert not scheduler.settings.records_dir.exists()


@pytest.mark.parametrize("error", [http_error(429, 70), ThrottledApiError("maxlag")])
def test_throttling_pauses_both_hosts_and_all_stages(tmp_path, monkeypatch, error):
    calls = []
    clock = Clock()
    def attempt(settings, url, data):
        calls.append((clock(), url, data["titles"]))
        if len(calls) == 1:
            raise error
        return payload(data["titles"], 1 if data["titles"] == "A" else 2)
    scheduler, _ = runner(tmp_path, monkeypatch, [page("A"), page("B")], attempt, clock=clock)
    scheduler.run()
    floor = 70 if isinstance(error, requests.HTTPError) else 1
    assert all(t >= floor for t, _, _ in calls[1:])
    assert len({url for _, url, _ in calls}) == 1


def test_failed_link_continuation_retains_extract_and_previous_links(tmp_path, monkeypatch):
    calls = []
    failed = False
    def attempt(settings, url, data):
        nonlocal failed
        calls.append(dict(data))
        if data["prop"] == "extracts":
            return payload(extract="A可以指：")
        if "plcontinue" not in data:
            result = payload(extract="<h2>heading</h2><ul><li><b>First</b></li><li><b>Second</b></li></ul>",
                             links=[{"ns": 0, "title": "First"}, {"ns": 4, "title": "Other"}])
            result["continue"] = {"plcontinue": "next", "continue": "||"}
            return result
        if not failed:
            failed = True
            raise http_error(502)
        return {"query": {"pages": {"1": {"pageid": 1, "title": "A",
                                          "links": [{"ns": 0, "title": "Second"}]}}}}
    scheduler, _ = runner(tmp_path, monkeypatch, [page("A")], attempt)
    scheduler.run()
    assert len([d for d in calls if d["prop"] == "extracts"]) == 1
    assert len([d for d in calls if "plcontinue" in d]) == 2
    assert all("exintro" not in d for d in calls if d["prop"] == "extracts|links")
    record = f.load_record(f.record_path_for_page(scheduler.settings, 1), strict=True)
    assert [link.title for link in record.listed_links] == ["First", "Second"]


def test_extract_continuation_retries_only_current_step(tmp_path, monkeypatch):
    calls = []
    def attempt(settings, url, data):
        calls.append(dict(data))
        if len(calls) == 1:
            result = payload("A", 1)
            result["continue"] = {"excontinue": 1}
            return result
        if len(calls) == 2:
            raise requests.ConnectionError("offline")
        return payload("B", 2)
    # Construct a two-page job without changing the shared fixture's defaults.
    scheduler, _ = runner(tmp_path, monkeypatch, [page("A"), page("B")], attempt)
    scheduler.extracts.clear()
    scheduler.extracts.append(extract_job(scheduler.pages, BatchCompletion(2, 0)))
    scheduler.backlog_limit = 4
    scheduler.run()
    assert [d.get("excontinue") for d in calls] == [None, 1, 1]
    assert scheduler.extract_pages == 2
    assert scheduler.progress.records_fetched == 2


def test_split_retains_successful_sibling_and_completed_continuation(tmp_path, monkeypatch):
    calls = []
    def attempt(settings, url, data):
        titles = data["titles"].split("|")
        calls.append((titles, data.get("excontinue")))
        if len(calls) == 1:
            result = payload("A", 1)
            result["continue"] = {"excontinue": 1}
            return result
        if len(titles) > 1:
            raise http_error(413)
        return payload(titles[0], {"A": 1, "B": 2, "C": 3}[titles[0]])
    scheduler, _ = runner(tmp_path, monkeypatch, [page("A"), page("B"), page("C")], attempt)
    scheduler.extracts.clear()
    scheduler.extracts.append(extract_job(scheduler.pages, BatchCompletion(3, 0)))
    scheduler.backlog_limit = 6
    scheduler.run()
    assert calls == [(["A", "B", "C"], None), (["A", "B", "C"], 1), (["B"], None), (["C"], None)]
    assert scheduler.progress.records_fetched == 3


def test_malformed_final_step_does_not_poison_continuation_state(tmp_path, monkeypatch):
    calls = []
    def attempt(settings, url, data):
        calls.append(data.get("excontinue"))
        if len(calls) == 1:
            result = payload("A", 1)
            result["continue"] = {"excontinue": 1}
            return result
        if len(calls) == 2:
            return {"query": {"pages": {"2": {"pageid": 2, "title": "B"}}}}
        return payload("B", 2)
    scheduler, _ = runner(tmp_path, monkeypatch, [page("A"), page("B")], attempt)
    scheduler.extracts.clear()
    scheduler.extracts.append(extract_job(scheduler.pages, BatchCompletion(2, 0)))
    scheduler.backlog_limit = 4
    scheduler.run()
    assert calls == [None, 1, 1]
    assert scheduler.extract_pages == 2


def test_terminal_link_failure_does_not_commit_partial_entry(tmp_path, monkeypatch):
    def attempt(settings, url, data):
        if data["prop"] == "extracts":
            return payload(extract="A可以指：")
        raise PermanentApiError("badvalue")
    scheduler, _ = runner(tmp_path, monkeypatch, [page("A")], attempt)
    with pytest.raises(PermanentApiError):
        scheduler.run()
    assert scheduler.progress.pending_pages_remaining == 1
    assert not scheduler.settings.records_dir.exists()
    assert scheduler.pages[0].fetched_lastmod is None
    assert not scheduler.negative_index.by_source_url


def test_link_lookup_is_shared_but_source_freshness_is_separate(tmp_path, monkeypatch):
    calls = []
    def attempt(settings, url, data):
        calls.append(data["prop"])
        if data["prop"] == "extracts":
            result = payload("Main", 1, "Main可以指：")
            if data["titles"] != "Main":
                result["query"]["redirects"] = [{"from": data["titles"], "to": "Main"}]
            return result
        return payload("Main", 1, "<ul><li><b>Target</b></li></ul>", links=[{"ns": 0, "title": "Target"}])
    pages = [page("Alias"), replace(page("Main"), lastmod="yesterday")]
    scheduler, _ = runner(tmp_path, monkeypatch, pages, attempt)
    scheduler.run()
    assert calls.count("extracts|links") == 1
    assert [p.fetched_lastmod for p in pages] == ["today", "yesterday"]
    assert scheduler.progress.records_fetched == 2


def test_backlog_and_shared_request_ceiling(tmp_path, monkeypatch):
    pages = [page(str(i)) for i in range(30)]
    observed = []
    def attempt(settings, url, data):
        kind = "links" if data["prop"] == "extracts|links" else "extract"
        active_links = sum(j.kind == "links" for j, _ in scheduler.active.values()) + (kind == "links")
        observed.append((len(scheduler.active) + 1, active_links,
                         len(scheduler.link_jobs) + scheduler.reserved_pages,
                         scheduler.remaining_extract_pages))
        title = data["titles"]
        if kind == "extract":
            return payload(title, int(title) + 1, title + "可以指：")
        return payload(title, int(title) + 1, "<ul><li><b>Target</b></li></ul>",
                       links=[{"ns": 0, "title": "Target"}])
    scheduler, _ = runner(tmp_path, monkeypatch, pages, attempt)
    scheduler.settings = replace(scheduler.settings, concurrency=4)
    scheduler.controller = ConcurrencyController(scheduler.settings, 0, lambda _: None)
    scheduler.backlog_limit = 8
    scheduler.run()
    assert max(active for active, _, _, _ in observed) <= 4
    assert max(backlog for _, _, backlog, _ in observed) <= 8
    assert all(links <= 1 for _, links, _, remaining in observed if remaining)
    assert scheduler.progress.pending_pages_remaining == 0


def test_terminal_failure_drains_running_attempts_without_new_submissions(tmp_path, monkeypatch):
    calls = []
    def attempt(settings, url, data):
        title = data["titles"]
        calls.append(title)
        if title == "A":
            raise http_error(400)
        return payload(title, ord(title))
    scheduler, _ = runner(tmp_path, monkeypatch, [page(t) for t in "ABCDE"], attempt)
    scheduler.settings = replace(scheduler.settings, concurrency=4)
    scheduler.controller = ConcurrencyController(scheduler.settings, 0, lambda _: None)
    scheduler.backlog_limit = 8
    with pytest.raises(requests.HTTPError):
        scheduler.run()
    assert calls == list("ABCD")
    assert scheduler.progress.records_fetched == 3
    assert scheduler.progress.pending_pages_remaining == 2
    assert not scheduler.negative_index.by_source_url
    assert {p.canonical_title for p in scheduler.pages if p.fetched_lastmod} == set("BCD")


def test_fetch_failure_forces_final_checkpoint_and_keeps_incomplete_page_pending(tmp_path, monkeypatch):
    from moegirl_yomitan import scheduler as module
    settings = Settings(cache_dir=tmp_path / "cache", concurrency=1, retry_attempts=1)
    pages = [page("A"), page("B")]
    monkeypatch.setattr(f, "discover_pages", lambda *args, **kwargs: pages)
    def attempt(settings, url, data):
        if "B" in data["titles"]:
            raise http_error(400)
        return payload()
    # Separate batches allow A to commit before B fails.
    settings = replace(settings, batch_size=1)
    monkeypatch.setattr(module, "api_attempt", attempt)
    with pytest.raises(requests.HTTPError):
        f.fetch_pages(settings)
    manifest = json.loads(settings.manifest_path.read_text(encoding="utf-8"))
    assert manifest["progress"]["pages_pending_fetch"] == 1
    assert manifest["pages"][0]["fetched_lastmod"] == "today"
    assert manifest["pages"][1]["fetched_lastmod"] is None
    assert settings.record_cache_index_path.exists()


@pytest.mark.parametrize("cap,expected", [(5, [0, 5, 45]), (60, [0, 30, 70])])
def test_jitter_cap_and_retry_after_floor(tmp_path, monkeypatch, cap, expected):
    clock = Clock()
    calls = []
    def attempt(settings, url, data):
        calls.append(clock())
        if len(calls) < 3:
            raise http_error(503, 40 if len(calls) == 2 else None)
        return payload()
    scheduler, _ = runner(tmp_path, monkeypatch, [page("A")], attempt, clock=clock,
                          backoff_base_seconds=25, adaptive_backoff_cap_seconds=cap)
    scheduler.jitter = lambda: 1.2
    scheduler.run()
    assert calls == expected


def test_under_sampled_window_does_not_leave_probe_running_forever():
    c = ConcurrencyController(Settings(concurrency=8), 0, lambda _: None)
    add_window(c, 30)
    add_window(c, 60)
    assert c.current == 5
    add_window(c, 90, requests=0)
    add_window(c, 120, requests=0)
    assert c.current == 4 and c.probe is None


def test_request_latency_excludes_cache_writes(tmp_path, monkeypatch):
    clock = Clock()
    def attempt(settings, url, data):
        clock.advance(1)
        return payload(data["titles"], ord(data["titles"]))
    scheduler, _ = runner(tmp_path, monkeypatch, [page("A"), page("B")], attempt, clock=clock)
    scheduler.settings = replace(scheduler.settings, concurrency=2)
    scheduler.controller = ConcurrencyController(scheduler.settings, clock(), lambda _: None)
    scheduler.backlog_limit = 4
    original_write = f.write_record
    def slow_write(settings, record):
        clock.advance(30)
        original_write(settings, record)
    monkeypatch.setattr(f, "write_record", slow_write)
    scheduler.run()
    assert list(scheduler.latencies) == [1.0, 1.0]
    assert scheduler.progress.scheduler_metrics["request_p95_seconds"] == 1.0


@pytest.mark.parametrize("kind", ["extract", "links"])
def test_repeated_continuation_has_bounded_retries_and_never_negative_caches(tmp_path, monkeypatch, kind):
    calls = []
    def attempt(settings, url, data):
        calls.append(dict(data))
        result = payload(extract="A可以指：" if kind == "links" else "summary")
        if kind == "extract":
            result["continue"] = {"excontinue": 1}
        elif data["prop"] == "extracts|links":
            result["continue"] = {"plcontinue": "same"}
        return result
    scheduler, _ = runner(tmp_path, monkeypatch, [page("A")], attempt, retry_attempts=2)
    with pytest.raises(f.ApiResponseError, match="repeated"):
        scheduler.run()
    assert len(calls) == (3 if kind == "extract" else 4)
    assert not scheduler.negative_index.by_source_url
    assert not scheduler.settings.records_dir.exists()


def test_scheduled_records_match_synchronous_builder(tmp_path, monkeypatch):
    pages = [page("Alias"), page("Ambiguous"), page("Missing")]
    summary_payload = {"query": {
        "redirects": [{"from": "Alias", "to": "Main"}],
        "pages": {"1": {"pageid": 1, "title": "Main", "extract": "  中文摘要。\n" * 30},
                  "2": {"pageid": 2, "title": "Ambiguous", "extract": "Ambiguous可以指："},
                  "-1": {"title": "Missing", "missing": ""}}}}
    html_payload = payload("Ambiguous", 2,
        "<h2>Heading</h2><ul><li><b>Target</b></li><li><b>Target</b></li><li><b>Unlinked</b></li></ul>",
        links=[{"ns": 0, "title": "Target"}, {"ns": 4, "title": "Unlinked"}])
    def api_data(session, settings, data):
        return summary_payload if data["prop"] == "extracts" else html_payload
    monkeypatch.setattr(f, "request_api_payload", api_data)
    settings = Settings(summary_char_limit=40)
    expected = f.fetch_batch_with_session(object(), settings, pages)
    scheduler, _ = runner(tmp_path, monkeypatch, pages, lambda s, u, d: api_data(None, s, d))
    scheduler.settings = replace(scheduler.settings, summary_char_limit=40)
    scheduler.extracts.clear()
    scheduler.extracts.append(extract_job(pages, BatchCompletion(3)))
    scheduler.backlog_limit = 6
    scheduler.run()
    for reference in expected:
        if reference is None:
            continue
        actual = f.load_record(f.record_path_for_page(scheduler.settings, reference.pageid), strict=True)
        before, after = reference.to_dict(), actual.to_dict()
        before.pop("retrieved_at")
        after.pop("retrieved_at")
        assert after == before
    assert pages[2].fetched_lastmod == ""


def add_window(controller, now, *, pages=200, seconds=1, retries=0, requests=10, remaining=10000):
    for _ in range(requests):
        controller.success(seconds, pages // requests, extract=True)
    controller.window.retries = retries
    controller.evaluate(now, remaining)


def test_concurrency_probe_accepts_improvement_and_respects_ceiling():
    logs = []
    c = ConcurrencyController(Settings(concurrency=5), 0, logs.append)
    add_window(c, 30)
    add_window(c, 60)
    assert c.current == 5
    add_window(c, 90, pages=240)
    add_window(c, 120, pages=240)
    assert c.current == 5 and c.probe is None
    add_window(c, 150, pages=240)
    add_window(c, 180, pages=240)
    assert c.current == 5
    assert any("retained" in line for line in logs)


@pytest.mark.parametrize("pages,seconds,retries", [(210, 1, 0), (240, 2, 0), (240, 1, 1)])
def test_bad_probe_reverts_and_is_suppressed(pages, seconds, retries):
    c = ConcurrencyController(Settings(concurrency=8), 0, lambda _: None)
    add_window(c, 30)
    add_window(c, 60)
    add_window(c, 90, pages=pages, seconds=seconds, retries=retries)
    add_window(c, 120, pages=pages, seconds=seconds, retries=retries)
    assert c.current == 4 and c.suppressed_until == 420
    add_window(c, 150)
    add_window(c, 180)
    assert c.current == 4


def test_latency_regression_throttling_and_minimum():
    c = ConcurrencyController(Settings(concurrency=4, min_concurrency=2), 0, lambda _: None)
    add_window(c, 30)
    add_window(c, 60)
    add_window(c, 90, pages=140, seconds=2)
    add_window(c, 120, pages=140, seconds=2)
    assert c.current == 3
    c.failure(121, throttled=True)
    assert c.current == 2
    c.failure(122, throttled=False)
    assert c.current == 2


def test_under_sampled_windows_and_small_tail_do_not_probe():
    c = ConcurrencyController(Settings(concurrency=8), 0, lambda _: None)
    add_window(c, 30, requests=9)
    add_window(c, 60)
    assert c.current == 4
    add_window(c, 90, remaining=1)
    assert c.current == 4


def test_transient_failure_recovers_after_two_healthy_windows():
    c = ConcurrencyController(Settings(concurrency=4), 0, lambda _: None)
    c.failure(0, throttled=False)
    assert c.current == 3
    c.evaluate(30, 10000)  # Discard the under-sampled failure window.
    add_window(c, 60)
    add_window(c, 90)
    assert c.current == 4 and c.probe is not None


def test_failure_during_probe_suppresses_that_probe():
    c = ConcurrencyController(Settings(concurrency=8), 0, lambda _: None)
    add_window(c, 30)
    add_window(c, 60)
    c.failure(61, throttled=False)
    assert c.current == 4 and c.probe is None and c.suppressed_until == 361
    add_window(c, 90)
    add_window(c, 120)
    assert c.current == 4


@pytest.mark.parametrize("status,error", [(200, {"code": "maxlag"}), (200, {"code": "badvalue"}), (429, None)])
def test_one_attempt_closes_response_and_classifies_api_errors(status, error):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps({"error": error} if error else {}).encode()
    response._content_consumed = True
    closed = []
    response.close = lambda: closed.append(True)
    class Session:
        calls = 0
        def request(self, *args, **kwargs):
            self.calls += 1
            return response
    session = Session()
    expected = requests.HTTPError if status == 429 else ThrottledApiError if error["code"] == "maxlag" else PermanentApiError
    with pytest.raises(expected):
        api_attempt(Settings(), "https://example.invalid", {}, session=session)
    assert session.calls == 1 and closed == [True]
