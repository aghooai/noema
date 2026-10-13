"""Sync client: resources, errors, retries, pagination and file streams."""

from __future__ import annotations

import io
import json
import os

import httpx
import pytest
import respx

from aghoo import (
    Aghoo,
    AghooConnectionError,
    AghooError,
    AghooTimeoutError,
    DerivativeContent,
    FileObject,
    Session,
    _retry,
)
from support import API_KEY, BASE, fixture_json, respond, sse_frame


def test_error_envelope_maps_to_aghoo_error(mock: respx.MockRouter, client: Aghoo) -> None:
    mock.post("/v1/decisions").mock(side_effect=lambda r: respond("createDecision.400"))
    with pytest.raises(AghooError) as info:
        client.decisions.create([], input="x")
    e = info.value
    assert (e.status, e.type, e.code, e.param, e.request_id, e.retryable, e.retry_after_s, e.details) == (
        400,
        "invalid_request_error",
        "schema_violation",
        "questions[0].choices",
        "req_01JA0000000000000000000000",
        False,
        None,
        None,
    )
    assert str(e) == "questions[0].choices: too short"
    assert "schema_violation" in repr(e)


def test_retry_after_and_one_idempotency_key(mock: respx.MockRouter, client: Aghoo, sleeps: list[float]) -> None:
    route = mock.post("/v1/decisions").mock(
        side_effect=[
            respond("createDecision.429"),
            httpx.Response(200, json={"model": "m", "answers": {}, "usage": {}, "decision_id": "dec_1"}),
        ]
    )
    client.decisions.create([], output_format="jev", input="x")
    keys = [c.request.headers["idempotency-key"] for c in route.calls]
    assert len(keys) == 2
    assert keys[0] == keys[1]
    assert sleeps == [2.0]


def test_full_jitter_and_max_retries(mock: respx.MockRouter, client: Aghoo, sleeps: list[float]) -> None:
    route = mock.get("/v1/sessions/ses_1").mock(side_effect=lambda r: respond("createTurn.500"))
    with pytest.raises(AghooError, match="internal error"):
        client.sessions.retrieve("ses_1")
    assert route.call_count == 3
    assert sleeps == [0.25, 0.5]
    assert [_retry.delay_s(a, None) for a in (2, 3, 4, 9)] == [1.0, 2.0, 4.0, 4.0]
    assert _retry.delay_s(0, 61) is None


def test_no_retry_for_non_retryable_or_disabled(mock: respx.MockRouter, client: Aghoo, sleeps: list[float]) -> None:
    route = mock.get("/v1/sessions/ses_x").mock(side_effect=lambda r: respond("getSession.404"))
    with pytest.raises(AghooError, match="session not found"):
        client.sessions.retrieve("ses_x")
    assert route.call_count == 1
    busy = mock.post("/v1/sessions").mock(side_effect=lambda r: respond("createTurn.409"))
    with pytest.raises(AghooError) as info:
        client.sessions.create(options={"max_retries": 0})
    assert (info.value.code, info.value.retryable, busy.call_count) == ("session_busy", True, 1)
    assert sleeps == []


def test_connect_errors_and_timeouts(mock: respx.MockRouter, client: Aghoo, sleeps: list[float]) -> None:
    route = mock.get("/v1/profiles").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(AghooConnectionError, match="refused"):
        client.profiles.list()
    assert route.call_count == 3
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(AghooTimeoutError):
        client.profiles.list(options={"max_retries": 0, "timeout": 0.5})


def test_non_envelope_status_is_mapped_and_retried(mock: respx.MockRouter, client: Aghoo, sleeps: list[float]) -> None:
    route = mock.get("/v1/profiles").mock(
        side_effect=[httpx.Response(502, text="<html>bad gateway</html>"), respond("listProfiles.200")]
    )
    assert [p.version for p in client.profiles.list(kind="generation")] == ["1", "7"]
    assert route.calls[0].request.url.params["kind"] == "generation"
    route.mock(side_effect=lambda r: httpx.Response(502, text="bad", headers={"x-request-id": "req_p"}))
    with pytest.raises(AghooError) as info:
        client.profiles.list(options={"max_retries": 0})
    assert (info.value.code, info.value.type, info.value.request_id, info.value.retryable) == (
        "unknown_error",
        "server_error",
        "req_p",
        True,
    )


def test_headers_api_key_env_and_custom_idempotency_key(
    mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = mock.post("/v1/sessions").mock(side_effect=lambda r: respond("createSession.201"))
    with Aghoo(base_url=BASE) as anonymous:
        anonymous.sessions.create()
    assert "authorization" not in route.calls[0].request.headers
    monkeypatch.setenv("AGHOO_API_KEY", "ak_test_env.secret")
    monkeypatch.setenv("AGHOO_BASE_URL", BASE + "/")
    with Aghoo() as env_client:
        env_client.sessions.create(metadata={"a": "b"}, options={"idempotency_key": "order-17"})
    request = route.calls[1].request
    assert request.headers["authorization"] == "Bearer ak_test_env.secret"
    assert request.headers["idempotency-key"] == "order-17"
    assert request.headers["aghoo-version"] == "2026-10-01"
    assert json.loads(request.content) == {"metadata": {"a": "b"}}
    assert "AGHOO_API_KEY" in os.environ


def test_sessions_crud_and_lazy_pages(mock: respx.MockRouter, client: Aghoo) -> None:
    pages = mock.get("/v1/sessions").mock(
        side_effect=lambda r: respond(
            "listSessions.page2" if r.url.params.get("after") == "c2" else "listSessions.page1"
        )
    )
    it = client.sessions.list(limit=2)
    first = next(it)
    assert isinstance(first, Session)
    assert pages.call_count == 1
    assert [first.id, *(s.id for s in it)] == ["ses_3", "ses_2", "ses_1"]
    assert [dict(c.request.url.params) for c in pages.calls] == [{"limit": "2"}, {"limit": "2", "after": "c2"}]
    mock.patch("/v1/sessions/ses_1").mock(side_effect=lambda r: respond("updateSession.200"))
    updated = client.sessions.update("ses_1", data_class="confidential")
    assert updated.data_class == "confidential"
    patch = mock.calls.last.request
    assert "idempotency-key" not in patch.headers
    assert json.loads(patch.content) == {"data_class": "confidential"}
    mock.delete("/v1/sessions/ses_1").mock(side_effect=lambda r: respond("deleteSession.202"))
    assert client.sessions.delete("ses_1").deletion_job_id == "job_01JA"
    mock.get("/v1/sessions/ses_1/turns").mock(
        side_effect=lambda r: respond(
            "listSessionTurns.page1" if "after" not in r.url.params else "listSessionTurns.page2"
        )
    )
    turns = list(client.sessions.turns("ses_1"))
    assert [(t.seq, t.role) for t in turns] == [(1, "user"), (1, "assistant"), (2, "user"), (2, "assistant")]
    r = turns[0].redactions[0]
    assert turns[0].text[r.start : r.end] == "[redacted]"


def test_files_upload_derivative_watch(mock: respx.MockRouter, client: Aghoo) -> None:
    upload = mock.post("/v1/files").mock(side_effect=lambda r: respond("uploadFile.202"))
    f = client.files.upload(
        io.BytesIO(b"%PDF-1.7"), filename="invoice.pdf", session_id="ses_1", media_type="application/pdf"
    )
    assert isinstance(f, FileObject)
    assert f.status == "quarantined"
    req = upload.calls[0].request
    assert req.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="invoice.pdf"' in req.content
    assert b"%PDF-1.7" in req.content
    assert b'name="purpose"\r\n\r\ninput' in req.content
    assert b'name="session_id"\r\n\r\nses_1' in req.content
    mock.get("/v1/files/file_1/derivatives/markdown").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/markdown; charset=utf-8"}, content=b"# Hi")
    )
    assert client.files.derivative("file_1", "markdown") == DerivativeContent(b"# Hi", "text/markdown")
    frames = fixture_json("watchFile.events")["frames"]
    body = (
        b": ping\n\n" + sse_frame(0, "other", {}) + b"".join(sse_frame(x["id"], x["event"], x["data"]) for x in frames)
    )
    mock.get("/v1/files/file_1/events").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
    )
    assert [s.status for s in client.files.watch("file_1")] == ["quarantined", "processing", "ready"]
    mock.get("/v1/files/file_2/events").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse_frame(1, "file.updated", frames[0]["data"])
        )
    )
    seen: list[str] = []
    with pytest.raises(AghooConnectionError, match="terminal status"):
        seen.extend(s.status for s in client.files.watch("file_2"))
    assert seen == ["quarantined"]


def test_usage_and_admin_views(mock: respx.MockRouter, client: Aghoo) -> None:
    usage = mock.get("/v1/usage").mock(
        side_effect=lambda r: respond("getUsage.page2" if r.url.params.get("after") == "u2" else "getUsage.page1")
    )
    keys = [row.key for row in client.usage.list(group_by="model", from_="2026-09-11", to="2026-10-10")]
    assert keys == ["us.anthropic.claude-haiku-5-5", "us.openai.gpt-6.1-sol", None]
    assert dict(usage.calls[0].request.url.params) == {"group_by": "model", "from": "2026-09-11", "to": "2026-10-10"}
    mock.get("/v1/admin/calibrators").mock(side_effect=lambda r: respond("listCalibrators.200"))
    mock.get("/v1/admin/labeling/runs").mock(side_effect=lambda r: respond("listLabelingRuns.200"))
    mock.get("/v1/admin/labeling/dropped").mock(side_effect=lambda r: respond("listLabelingDropped.200"))
    cal = next(client.admin.calibrators.list(status="fitted"))
    assert (cal.id, cal.parameters.member_T["m1"], cal.reliability.bins[1].observed) == ("cal_1", 1.1, None)
    assert next(client.admin.labeling.runs()).counts.kept == 7
    assert next(client.admin.labeling.dropped(reason="disagreed")).labeler_answers[1].value == "other"
    mock.get("/v1/admin/calibrators").mock(side_effect=lambda r: respond("listCalibrators.403"))
    with pytest.raises(AghooError, match="may not read"):
        next(client.admin.calibrators.list())


def test_client_does_not_close_an_injected_http_client(mock: respx.MockRouter) -> None:
    mock.get("/v1/profiles").mock(side_effect=lambda r: respond("listProfiles.200"))
    http = httpx.Client()
    with Aghoo(api_key=API_KEY, base_url=BASE, http_client=http) as c:
        c.profiles.list()
    assert not http.is_closed
    http.close()
