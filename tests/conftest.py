from __future__ import annotations

import sys
from pathlib import Path
import pytest
import requests
import json


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def prevent_unexpected_network_requests(request, monkeypatch):
    if request.node.name == "test_smoke_build_and_package":
        return

    def fail_request(*args, **kwargs):
        pytest.fail("Unexpected network request in an offline test")

    monkeypatch.setattr(requests.sessions.Session, "request", fail_request)


@pytest.fixture
def mock_batches(monkeypatch):
    """Keep workflow fixtures while mocking the new one-request transport boundary."""
    from moegirl_yomitan import fetcher, scheduler
    original = scheduler.extract_job
    pages_by_payload = {}
    callback = None

    def job(pages, batch):
        result = original(pages, batch)
        pages_by_payload[json.dumps(result.payload, sort_keys=True)] = pages
        return result

    def attempt(settings, url, payload):
        fetcher.get_thread_session(settings, fetcher.SESSION_POOL_BATCH)
        pages = pages_by_payload[json.dumps(payload, sort_keys=True)]
        records = callback(settings, pages)
        returned, redirects = {}, []
        for i, (page, record) in enumerate(zip(pages, records)):
            if record is None:
                returned[str(-i - 1)] = {"title": page.title_from_url, "missing": ""}
                if page.pageid is not None:
                    returned[str(-i - 1)]["pageid"] = page.pageid
            else:
                returned[str(record.pageid)] = {"pageid": record.pageid,
                    "title": record.canonical_title, "extract": record.summary}
                if page.title_from_url != record.canonical_title:
                    redirects.append({"from": page.title_from_url, "to": record.canonical_title})
        return {"query": {"pages": returned, "redirects": redirects}}

    monkeypatch.setattr(scheduler, "extract_job", job)
    monkeypatch.setattr(scheduler, "api_attempt", attempt)

    def install(function):
        nonlocal callback
        callback = function
    return install
