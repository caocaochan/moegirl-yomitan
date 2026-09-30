"""Bounded live download comparison; writes only to a separate benchmark cache."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import random
import sys
import threading
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from moegirl_yomitan import fetcher as f
from moegirl_yomitan.config import Settings
from moegirl_yomitan.scheduler import EntryScheduler, PermanentApiError, api_attempt, percentile


def positive_int(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def run_case(name, pages, root, *, ceiling, adaptive, max_seconds):
    settings = Settings(cache_dir=root / name, concurrency=ceiling,
                        min_concurrency=1 if adaptive else ceiling,
                        retry_attempts=2, request_timeout=(10.0, 30.0))
    pages = [replace(p, fetched_lastmod=None) for p in pages]
    batches = [pages[i:i + settings.batch_size] for i in range(0, len(pages), settings.batch_size)]
    started = time.monotonic()
    progress = f.FetchProgressState(0, 0, len(pages), started, started, initial_pending_pages=len(pages),
                                    request_retry_baseline=f.request_retry_count())
    durations = []
    active = peak = 0
    lock = threading.Lock()

    def measured_attempt(settings, url, data):
        nonlocal active, peak
        if time.monotonic() - started >= max_seconds:
            raise PermanentApiError("Benchmark time limit reached")
        request_started = time.monotonic()
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            return api_attempt(settings, url, data)
        finally:
            with lock:
                durations.append(time.monotonic() - request_started)
                active -= 1

    scheduler = EntryScheduler(settings, pages, batches, progress,
                               f.RecordCacheIndex({}, {}, {}), f.NegativeCacheIndex(),
                               attempt=measured_attempt, adaptive=adaptive)
    error = None
    try:
        scheduler.run()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        f.save_manifest_checkpoint(settings, pages, progress, negative_index=scheduler.negative_index, force=True)
    elapsed = time.monotonic() - started
    completed = len(pages) - progress.pending_pages_remaining
    result = {"case": name, "requested_entries": len(pages), "finalized_entries": completed,
              "usable_records": progress.records_fetched, "elapsed_seconds": round(elapsed, 3),
              "completed_entries_per_second": round(completed / elapsed, 2),
              "requests": len(durations), "retries": f.request_retry_count() - progress.request_retry_baseline,
              "request_p50_seconds": round(percentile(durations, .5), 3),
              "request_p95_seconds": round(percentile(durations, .95), 3),
              "peak_active_requests": peak, "final_concurrency": scheduler.controller.current,
              "error": error}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-cache", type=Path, default=Settings.cache_dir)
    parser.add_argument("--pages-per-case", type=positive_int, default=1200)
    parser.add_argument("--max-seconds-per-case", type=positive_int, default=120)
    parser.add_argument("--seed", type=int, default=9302026)
    args = parser.parse_args()
    source = Settings(cache_dir=args.source_cache)
    eligible = [p for p in f.load_manifest(source) if p.pageid is not None]
    if len(eligible) < 3 * args.pages_per_case:
        parser.error("source manifest needs at least three times --pages-per-case cached page IDs")
    chosen = random.Random(args.seed).sample(eligible, 3 * args.pages_per_case)
    # Balance cached summary length and disambiguation prevalence across distinct cases.
    def workload_key(page):
        path = source.cache_dir / page.record_path if page.record_path else f.record_path_for_page(source, page.pageid)
        record = f.load_record(path)
        return (bool(record and f.summary_may_list_links(record.summary)), len(record.summary) if record else 0)
    chosen.sort(key=workload_key)
    groups = [chosen[i::3] for i in range(3)]
    rng = random.Random(args.seed + 1)
    for group in groups:
        rng.shuffle(group)
    root = Path(".cache/download-benchmark") / uuid4().hex[:12]
    root.mkdir(parents=True)
    results = []
    for name, group, ceiling, adaptive in (
        ("fixed-4", groups[0], 4, False),
        ("adaptive-4", groups[1], 4, True),
        ("adaptive-8", groups[2], 8, True),
    ):
        results.append(run_case(name, group, root, ceiling=ceiling, adaptive=adaptive,
                                max_seconds=args.max_seconds_per_case))
    warm = groups[0][:min(200, len(groups[0]))]
    results.append(run_case("repeated-cached-4", warm, root, ceiling=4, adaptive=False,
                            max_seconds=args.max_seconds_per_case))
    report = root / "results.json"
    report.write_text(json.dumps({"seed": args.seed, "results": results,
                                 "note": "Distinct balanced batches have no client-cache skips; server cache state is uncontrolled. "
                                         "The repeated case is reported separately. Short runs may not exercise concurrency probes."},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Benchmark report: {report.resolve()}", flush=True)
    return 1 if any(r["error"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
