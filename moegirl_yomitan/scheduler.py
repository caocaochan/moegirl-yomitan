"""Resumable entry API requests; all workflow state belongs to the scheduler thread."""
from __future__ import annotations

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass, field, replace
import heapq
import itertools
import json
import math
import random
import threading
import time
from typing import Any, Callable

import requests

from . import fetcher as f
from .config import Settings
from .models import ListedLink, ManifestPage, SummaryRecord
from .sitemaps import canonical_article_url


WINDOW_SECONDS = 30.0
MIN_WINDOW_REQUESTS = 10
PROBE_SUPPRESSION_SECONDS = 300.0
TRANSIENT_STATUS = {429, 500, 502, 503, 504}
OVERSIZED_STATUS = {413, 414, 431}


class PermanentApiError(f.ApiResponseError):
    pass


class ThrottledApiError(f.ApiResponseError):
    pass


def api_attempt(settings: Settings, url: str, payload: dict, *, session=None) -> dict:
    """Exactly one HTTP request; no fallback, backoff, or workflow mutation."""
    session = session or f.get_thread_session(settings, f.SESSION_POOL_BATCH)
    response = session.request("POST", url, data=payload, timeout=settings.request_timeout)
    try:
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError as exc:
            raise f.ApiResponseError("API response was not valid JSON", response=response) from exc
        if isinstance(result, dict) and ("error" in result or "errors" in result):
            errors = result.get("errors", [result.get("error")])
            if not isinstance(errors, list):
                errors = [errors]
            if any(isinstance(e, dict) and e.get("code") == "maxlag" for e in errors):
                raise ThrottledApiError("MediaWiki maxlag", response=response)
            raise PermanentApiError(f"MediaWiki API error: {errors}", response=response)
        return f.validate_api_payload(result)
    finally:
        response.close()


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else 0.0


@dataclass
class Window:
    started_at: float
    elapsed: float = 0.0
    attempts: int = 0
    retries: int = 0
    pages: int = 0
    latencies: list[float] = field(default_factory=list)


def window_summary(windows: list[Window]) -> tuple[float, float, float]:
    elapsed = sum(w.elapsed for w in windows)
    attempts = sum(w.attempts for w in windows)
    return (
        sum(w.pages for w in windows) / elapsed if elapsed else 0.0,
        percentile([x for w in windows for x in w.latencies], .95),
        sum(w.retries for w in windows) / attempts if attempts else 0.0,
    )


class ConcurrencyController:
    def __init__(self, settings: Settings, now: float, log: Callable[[str], None], *, enabled: bool = True):
        self.settings = settings
        self.current = max(settings.min_concurrency, min(4, settings.concurrency))
        self.enabled = enabled
        self.log = log
        self.window = Window(now)
        self.history: deque[Window] = deque(maxlen=4)
        self.probe: tuple[int, tuple[float, float, float]] | None = None
        self.probe_windows: list[Window] = []
        self.suppressed_until = now

    def change(self, value: int, reason: str) -> None:
        value = max(self.settings.min_concurrency, min(self.settings.concurrency, value))
        if value != self.current:
            self.log(f"Entry concurrency: {self.current}->{value}; {reason}")
            self.current = value

    def failure(self, now: float, *, throttled: bool) -> None:
        self.window.attempts += 1
        self.window.retries += 1
        self.change(self.current // 2 if throttled else self.current - 1,
                    "shared throttling cooldown" if throttled else "transient request failure")
        if self.probe is not None:
            self.suppressed_until = now + PROBE_SUPPRESSION_SECONDS
        self.history.clear()
        self.probe = None
        self.probe_windows.clear()

    def success(self, seconds: float, pages: int, *, extract: bool) -> None:
        self.window.attempts += 1
        if extract:
            self.window.pages += pages
            self.window.latencies.append(seconds)

    def evaluate(self, now: float, remaining_pages: int) -> None:
        if now - self.window.started_at < WINDOW_SECONDS:
            return
        window = self.window
        window.elapsed = now - window.started_at
        self.window = Window(now)
        if not self.enabled:
            return
        if len(window.latencies) < MIN_WINDOW_REQUESTS:
            # An under-sampled window cannot substantiate a probe or recovery.
            if self.probe is not None:
                self.change(self.probe[0], "throughput probe has insufficient samples")
                self.probe = None
                self.suppressed_until = now + PROBE_SUPPRESSION_SECONDS
            self.history.clear()
            self.probe_windows.clear()
            return
        self.history.append(window)
        if self.probe is not None:
            self.probe_windows.append(window)
            if len(self.probe_windows) < 2:
                return
            old_level, baseline = self.probe
            rate, p95, retries = window_summary(self.probe_windows)
            if rate >= baseline[0] * 1.1 and retries < .01 and p95 <= baseline[1] * 1.5:
                self.log(f"Entry concurrency: retained {self.current}; throughput probe improved by >=10%")
            else:
                self.change(old_level, "throughput probe did not improve sufficiently")
                self.suppressed_until = now + PROBE_SUPPRESSION_SECONDS
            self.probe = None
            self.probe_windows.clear()
            self.history.clear()
            return
        if len(self.history) == 4:
            baseline = window_summary(list(self.history)[:2])
            recent = window_summary(list(self.history)[2:])
            if recent[0] <= baseline[0] * .8 and recent[1] >= baseline[1] * 1.5:
                self.change(self.current - 1, "two windows of lower throughput and higher p95")
                self.history.clear()
                self.suppressed_until = now + PROBE_SUPPRESSION_SECONDS
                return
        if (len(self.history) >= 2 and self.current < self.settings.concurrency
                and now >= self.suppressed_until
                and remaining_pages >= 2 * self.settings.batch_size * self.settings.concurrency):
            baseline = window_summary(list(self.history)[-2:])
            if baseline[0] > 0 and baseline[2] < .01:
                self.probe = (self.current, baseline)
                self.probe_windows.clear()
                self.change(self.current + 1, "probing after two healthy throughput windows")


@dataclass
class BatchCompletion:
    remaining: int
    started_at: float | None = None


@dataclass(eq=False)
class ApiJob:
    kind: str
    payload: dict
    pages: list[ManifestPage] = field(default_factory=list)
    batch: BatchCompletion | None = None
    use_pageids: bool = False
    merged: dict = field(default_factory=lambda: {"query": {"pages": {}}})
    seen: set[str] = field(default_factory=set)
    extracted: set[str] = field(default_factory=set)
    continuation_count: int = 0
    attempts: int = 0
    url: str | None = None
    attempted_hosts: set[str] = field(default_factory=set)
    reserved: bool = False
    ready_at: float = 0.0
    key: tuple[int, str] | None = None
    waiters: list[tuple[ManifestPage, SummaryRecord, BatchCompletion]] = field(default_factory=list)
    html: str = ""
    linked_titles: set[str] = field(default_factory=set)
    saw_extract: bool = False


@dataclass(frozen=True)
class AttemptOutcome:
    payload: dict | None
    error: BaseException | None
    seconds: float


def extract_job(pages: list[ManifestPage], batch: BatchCompletion) -> ApiJob:
    use_pageids = all(p.pageid is not None and p.canonical_title == p.title_from_url.replace("_", " ")
                      for p in pages)
    payload = {"action": "query", "prop": "extracts", "exintro": "1", "explaintext": "1",
               "exlimit": str(len(pages)), "redirects": "1", "format": "json"}
    payload["pageids" if use_pageids else "titles"] = "|".join(
        str(p.pageid) if use_pageids else p.title_from_url for p in pages)
    return ApiJob("extract", payload, pages=pages, batch=batch, use_pageids=use_pageids)


class EntryScheduler:
    def __init__(self, settings: Settings, pages: list[ManifestPage], batches: list[list[ManifestPage]],
                 progress: f.FetchProgressState, record_index: f.RecordCacheIndex,
                 negative_index: f.NegativeCacheIndex, *, clock=None, jitter=None,
                 wait_for=None, idle_wait=None, executor_factory=None, attempt=None, adaptive=True):
        self.settings, self.pages, self.progress = settings, pages, progress
        self.record_index, self.negative_index = record_index, negative_index
        if progress.initial_pending_pages == 0:
            progress.initial_pending_pages = progress.pending_pages_remaining
        self.clock = clock or time.monotonic
        self.jitter = jitter or (lambda: random.uniform(.8, 1.2))
        self.wait_for = wait_for or wait
        self.idle_wait = idle_wait or threading.Event().wait
        self.executor_factory = executor_factory or ThreadPoolExecutor
        self.attempt = attempt or api_attempt
        now = self.clock()
        self.controller = ConcurrencyController(settings, now, f.log_status, enabled=adaptive)
        self.extracts = deque(extract_job(b, BatchCompletion(len(b))) for b in batches)
        self.links: deque[ApiJob] = deque()
        self.delayed: list[tuple[float, int, ApiJob]] = []
        self.sequence = itertools.count()
        self.active: dict[Any, tuple[ApiJob, float]] = {}
        self.link_jobs: dict[tuple[int, str], ApiJob] = {}
        self.link_results: dict[tuple[int, str], list[ListedLink]] = {}
        self.backlog_limit = 2 * settings.batch_size * settings.concurrency
        self.reserved_pages = 0
        self.remaining_extract_pages = sum(len(b) for b in batches)
        self.endpoints = f.host_fallback_candidates(settings.extracts_api_url)
        self.preferred_endpoint = self.endpoints[0]
        self.cooldown_until = now
        self.started_at = now
        self.extract_pages = 0
        self.latencies: deque[float] = deque(maxlen=200)
        self.error: BaseException | None = None

    def enqueue(self, job: ApiJob, delay: float = 0.0) -> None:
        job.ready_at = self.clock() + delay
        if delay:
            heapq.heappush(self.delayed, (job.ready_at, next(self.sequence), job))
        elif job.kind == "extract":
            self.extracts.appendleft(job)  # Continue existing workflows before starting new batches.
        else:
            self.links.append(job)

    def perform(self, url: str, payload: dict) -> AttemptOutcome:
        started = self.clock()
        try:
            result = self.attempt(self.settings, url, payload)
            return AttemptOutcome(result, None, max(0.0, self.clock() - started))
        except BaseException as exc:
            return AttemptOutcome(None, exc, max(0.0, self.clock() - started))

    def promote(self, now: float) -> None:
        while self.delayed and self.delayed[0][0] <= now:
            _, _, job = heapq.heappop(self.delayed)
            self.enqueue(job)

    def next_job(self) -> ApiJob | None:
        active_links = sum(j.kind == "links" for j, _ in self.active.values())
        quota = (max(1, self.controller.current // 4) if self.remaining_extract_pages
                 else self.controller.current)
        if self.links and active_links < quota:
            return self.links.popleft()
        if self.extracts:
            job = self.extracts[0]
            if job.reserved or len(self.link_jobs) + self.reserved_pages + len(job.pages) <= self.backlog_limit:
                self.extracts.popleft()
                if not job.reserved:
                    job.reserved = True
                    self.reserved_pages += len(job.pages)
                    if job.batch is not None and job.batch.started_at is None:
                        job.batch.started_at = self.clock()
                return job
        return None

    def commit(self, page: ManifestPage, record: SummaryRecord | None, batch: BatchCompletion) -> None:
        if record is None:
            f.mark_negative_cache_entry(self.negative_index, page)
            page.fetched_lastmod = ""
        else:
            f.write_record(self.settings, record)
            is_new = record.pageid not in self.record_index.by_pageid
            f.add_record_to_cache_index(self.settings, self.record_index, record)
            f.clear_negative_cache_entry(self.negative_index, page.source_url)
            page.pageid, page.canonical_title = record.pageid, record.canonical_title
            page.article_url = record.article_url
            page.record_path = f.record_relative_path(self.settings, f.record_path_for_page(self.settings, record.pageid))
            page.fetched_lastmod = page.lastmod
            self.progress.records_fetched += 1
            if is_new:
                self.progress.new_pageids_seen.add(record.pageid)
        self.progress.pending_pages_remaining = max(0, self.progress.pending_pages_remaining - 1)
        batch.remaining -= 1
        if batch.remaining == 0:
            self.progress.successful_batches += 1
            self.progress.batches_since_checkpoint += 1
            self.progress.recent_batch_seconds.append(self.clock() - (batch.started_at or self.started_at))

    def accept_extract(self, job: ApiJob, result: dict) -> int:
        candidate = deepcopy(job.merged)
        query = result["query"]
        for pageid, page in query["pages"].items():
            candidate["query"]["pages"].setdefault(pageid, {}).update(page)
        for name in ("normalized", "redirects"):
            if name in query:
                candidate["query"].setdefault(name, []).extend(query[name])
        continuation = result.get("continue")
        token = json.dumps(continuation, sort_keys=True) if continuation else ""
        records = None
        if continuation:
            if ("excontinue" not in continuation or token in job.seen
                    or job.continuation_count >= len(job.pages)):
                raise f.ApiResponseError("Unfinished, repeated, or excessive extract continuation")
        else:
            records = f.records_from_extract_payload(self.settings, job.pages, candidate, use_pageids=job.use_pageids)
        # Only mutate accumulated state after the complete step has validated.
        newly_extracted = {key for key, value in query["pages"].items() if "extract" in value} - job.extracted
        job.extracted.update(newly_extracted)
        job.merged = candidate
        if continuation:
            job.seen.add(token)
            job.continuation_count += 1
            job.payload.update(continuation)
            self.new_step(job)
        else:
            assert records is not None
            self.finish_extract_pages(job, job.pages, records)
        return len(newly_extracted)

    def finish_extract_pages(self, job: ApiJob, pages: list[ManifestPage],
                             records: list[SummaryRecord | None]) -> None:
        self.reserved_pages -= len(pages)
        self.remaining_extract_pages -= len(pages)
        assert job.batch is not None
        for page, record in zip(pages, records):
            if record is None or not f.summary_may_list_links(record.summary):
                self.commit(page, record, job.batch)
                continue
            key = (record.pageid, record.canonical_title)
            if key in self.link_results:
                self.commit(page, replace(record, listed_links=self.link_results[key]), job.batch)
                continue
            if key not in self.link_jobs:
                link_job = ApiJob("links", {"action": "query", "prop": "extracts|links", "redirects": "1",
                                          "format": "json", "titles": record.canonical_title,
                                          "plnamespace": "0", "pllimit": "max"}, key=key)
                self.link_jobs[key] = link_job
                self.links.append(link_job)
            self.link_jobs[key].waiters.append((page, record, job.batch))

    def accept_links(self, job: ApiJob, result: dict) -> None:
        html, saw_extract = job.html, job.saw_extract
        titles = set(job.linked_titles)
        pages = [p for p in result["query"]["pages"].values() if "missing" not in p]
        if pages:
            page = pages[0]
            saw_extract = saw_extract or "extract" in page
            html = html or page.get("extract", "")
            links = page.get("links", [])
            if not isinstance(links, list) or any(not isinstance(link, dict) for link in links):
                raise f.ApiResponseError("API links must be a list of objects")
            titles.update(link["title"] for link in links
                          if link.get("ns") == 0 and isinstance(link.get("title"), str) and link["title"])
        continuation = result.get("continue")
        token = json.dumps(continuation, sort_keys=True) if continuation else ""
        if continuation and ("plcontinue" not in continuation or token in job.seen):
            raise f.ApiResponseError("Unfinished or repeated link continuation")
        if not continuation and not saw_extract:
            raise f.ApiResponseError("API response omitted HTML extract")
        job.html, job.saw_extract, job.linked_titles = html, saw_extract, titles
        if continuation:
            job.seen.add(token)
            job.payload.update(continuation)
            self.new_step(job)
        else:
            ordered = dict.fromkeys(f.extract_listed_item_titles(html))
            links = [ListedLink(title=title, url=canonical_article_url(title)) for title in ordered if title in titles]
            assert job.key is not None
            self.link_results[job.key] = links
            del self.link_jobs[job.key]
            for page, record, batch in job.waiters:
                self.commit(page, replace(record, listed_links=links), batch)

    def new_step(self, job: ApiJob) -> None:
        job.attempts = 0
        job.url = None
        job.attempted_hosts.clear()
        self.enqueue(job)

    def failed(self, job: ApiJob, exc: requests.RequestException) -> None:
        now = self.clock()
        self.progress.failed_batch_attempts += 1  # Compatibility name; now counts API attempt failures.
        response = exc.response
        status = response.status_code if response is not None else None
        if status in OVERSIZED_STATUS and job.kind == "extract" and len(job.pages) > 1:
            pending = []
            for page in job.pages:
                try:
                    records = f.records_from_extract_payload(self.settings, [page], job.merged,
                                                             use_pageids=job.use_pageids)
                except f.ApiResponseError:
                    pending.append(page)
                else:
                    self.finish_extract_pages(job, [page], records)
            midpoint = max(1, len(pending) // 2)
            for subset in (pending[midpoint:], pending[:midpoint]):
                if not subset:
                    continue
                assert job.batch is not None
                child = extract_job(subset, job.batch)
                child.reserved = True
                child.merged = deepcopy(job.merged)
                child.extracted = set(job.extracted)
                self.enqueue(child)
            return
        throttled = status == 429 or isinstance(exc, ThrottledApiError)
        alternate = next((url for url in self.endpoints if url != job.url), None)
        endpoint_rejection = status in {403, 404}
        retryable = (not isinstance(exc, PermanentApiError) and
                     (isinstance(exc, f.ApiResponseError) or status is None
                      or status in TRANSIENT_STATUS or endpoint_rejection))
        if endpoint_rejection and (alternate is None or alternate in job.attempted_hosts):
            retryable = False
        if retryable:
            self.controller.failure(now, throttled=throttled)
        delay = min(30.0, self.settings.adaptive_backoff_cap_seconds,
                    self.settings.backoff_base_seconds * 2 ** min(job.attempts - 1, 30) * self.jitter())
        if response is not None:
            delay = max(delay, f.retry_after_seconds(response.headers.get("Retry-After")))
        if throttled:
            self.cooldown_until = max(self.cooldown_until, now + delay)
        if not retryable or job.attempts >= self.settings.retry_attempts:
            raise exc
        if not throttled and alternate is not None:
            job.url = alternate
        f.record_request_retry()
        self.enqueue(job, delay)
        f.log_status(f"API step retry: stage={job.kind}, attempt={job.attempts + 1}/{self.settings.retry_attempts}, "
                     f"delay={delay:.1f}s, error={f.format_request_error(exc)}")

    def update_progress(self) -> None:
        now = self.clock()
        self.progress.current_concurrency = self.controller.current
        self.progress.scheduler_metrics = {
            "extract_queue": len(self.extracts), "link_queue": len(self.links),
            "delayed_extract_steps": sum(j.kind == "extract" for _, _, j in self.delayed),
            "delayed_link_steps": sum(j.kind == "links" for _, _, j in self.delayed),
            "unresolved_links": len(self.link_jobs), "active_requests": len(self.active),
            "failed_api_attempts": self.progress.failed_batch_attempts,
            "delayed_retries": sum(job.attempts > 0 for _, _, job in self.delayed),
            "cooldown_seconds": round(max(0.0, self.cooldown_until - now), 2),
            "request_p50_seconds": round(percentile(list(self.latencies), .5), 3),
            "request_p95_seconds": round(percentile(list(self.latencies), .95), 3),
            "extract_pages_per_second": round(self.extract_pages / max(.001, now - self.started_at), 2),
            "finalized_entries_per_second": round(
                (self.progress.initial_pending_pages - self.progress.pending_pages_remaining)
                / max(.001, now - self.started_at), 2),
        }

    def run(self) -> None:
        try:
            with f.session_registry_scope(f.SESSION_POOL_BATCH):
                with self.executor_factory(max_workers=self.settings.concurrency) as executor:
                    while self.extracts or self.links or self.delayed or self.active:
                        now = self.clock()
                        self.promote(now)
                        self.controller.evaluate(now, self.remaining_extract_pages)
                        if self.error is None and now >= self.cooldown_until:
                            while len(self.active) < self.controller.current:
                                job = self.next_job()
                                if job is None:
                                    break
                                job.url = job.url or self.preferred_endpoint
                                job.attempts += 1
                                job.attempted_hosts.add(job.url)
                                submitted = self.clock()
                                future = executor.submit(self.perform, job.url, dict(job.payload))
                                self.active[future] = (job, submitted)
                        self.update_progress()
                        f.save_manifest_checkpoint(self.settings, self.pages, self.progress,
                                                   negative_index=self.negative_index)
                        if self.error is not None and not self.active:
                            raise self.error
                        deadlines = [self.controller.window.started_at + WINDOW_SECONDS,
                                     self.progress.last_checkpoint_at + f.CHECKPOINT_INTERVAL_SECONDS]
                        if self.cooldown_until > now:
                            deadlines.append(self.cooldown_until)
                        elif self.delayed:
                            deadlines.append(self.delayed[0][0])
                        timeout = max(.001, min(deadlines) - now)
                        if not self.active:
                            self.idle_wait(timeout)
                            continue
                        try:
                            completed, _ = self.wait_for(self.active, timeout=timeout, return_when=FIRST_COMPLETED)
                        except KeyboardInterrupt as exc:
                            self.error = self.error or exc
                            continue  # Drain running attempts and publish only complete entries.
                        for future in completed:
                            job, submitted = self.active.pop(future)
                            seconds = max(0.0, self.clock() - submitted)
                            try:
                                outcome = future.result()
                                seconds = outcome.seconds
                                self.latencies.append(seconds)
                                if outcome.error is not None:
                                    raise outcome.error
                                result = f.validate_api_payload(outcome.payload)
                                count = self.accept_extract(job, result) if job.kind == "extract" else 0
                                if job.kind == "links":
                                    self.accept_links(job, result)
                                self.extract_pages += count
                                self.controller.success(seconds, count, extract=job.kind == "extract")
                                self.preferred_endpoint = job.url or self.preferred_endpoint
                            except requests.RequestException as exc:
                                try:
                                    if self.error is None:
                                        self.failed(job, exc)
                                    else:
                                        self.progress.failed_batch_attempts += 1
                                except BaseException as terminal:
                                    self.error = terminal
                            except BaseException as terminal:
                                self.error = terminal
                    if self.error is not None:
                        raise self.error
        finally:
            self.update_progress()
