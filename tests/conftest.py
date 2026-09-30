from __future__ import annotations

import sys
from pathlib import Path
import pytest
import requests


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
