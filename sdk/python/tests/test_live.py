"""Live checks against a running Noema API (no model calls). Skipped unless NOEMA_LIVE=1; set NOEMA_LIVE_BASE_URL
(a loopback address: the test run allows sockets to 127.0.0.1 only) and NOEMA_LIVE_API_KEY."""

from __future__ import annotations

import os

import pytest

from aghoo import Aghoo, AghooError

pytestmark = pytest.mark.live


@pytest.fixture
def live() -> Aghoo:
    base_url, api_key = os.environ.get("NOEMA_LIVE_BASE_URL"), os.environ.get("NOEMA_LIVE_API_KEY")
    if not base_url or not api_key:
        pytest.skip("set NOEMA_LIVE_BASE_URL and NOEMA_LIVE_API_KEY")
    return Aghoo(base_url=base_url, api_key=api_key)


def test_profiles_sessions_and_usage(live: Aghoo) -> None:
    with live:
        assert all(p.version.isdigit() for p in live.profiles.list())
        session = live.sessions.create(metadata={"sdk": "python-live"})
        assert live.sessions.retrieve(session.id).id == session.id
        assert live.sessions.update(session.id, metadata={}).metadata == {}
        assert live.sessions.delete(session.id).deleted is True
        next(iter(live.usage.list(limit=1)), None)
        with pytest.raises(AghooError) as info:
            live.sessions.retrieve("ses_does_not_exist")
        assert info.value.code == "session_not_found"
