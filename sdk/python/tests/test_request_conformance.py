"""Every request the SDK builds passes the server's own request gate (params, headers, Content-Type, JSON Schema and
the semantic decision rules), so the SDK cannot send what the contract rejects."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from aghoo import Aghoo, AghooError, DecisionSet, Predicate, predicate
from support import example, fixture_json, goldens, respond, sse_frame

gate_mod = pytest.importorskip("noema.contracts.gate", reason="needs the Noema repo's request gate")
rules = pytest.importorskip("noema.contracts.rules")
GATE = gate_mod.RequestGate.from_bundle()


def _operation(request: httpx.Request) -> tuple[str, dict[str, str]]:
    for op_id, op in GATE.operations.items():
        pattern = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", op.path) + "$"
        m = re.match(pattern, request.url.path)
        if m and op.method.upper() == request.method:
            return op_id, m.groupdict()
    raise AssertionError(f"no operation for {request.method} {request.url.path}")


def check(request: httpx.Request) -> str:
    """Runs the gate on one captured request; returns its operation id."""
    op_id, path = _operation(request)
    query: dict[str, list[str]] = {}
    for k, v in request.url.params.multi_items():
        query.setdefault(k, []).append(v)
    GATE.check_params(op_id, path=path, query=query, headers=dict(request.headers))
    ct = request.headers.get("content-type")
    if ct is not None and ct.startswith("multipart/form-data"):
        GATE.check_content_type(op_id, ct)
        return op_id
    body = GATE.check_body(op_id, ct, request.content)
    if op_id == "createDecision":
        rules.check_decision_request(body)
    if op_id == "createTurn" and isinstance(body, dict) and "questions" in body.get("decisions", {}):
        rules.check_decision_request(body["decisions"], param_prefix="decisions.")
    return op_id


class Human(DecisionSet):
    needs_human: Predicate = predicate("Asks for a human.")


def _sse(name: str) -> httpx.Response:
    frames = fixture_json(name)["frames"]
    body = b"".join(sse_frame(f["id"], f["event"], f["data"]) for f in frames)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


G01 = goldens()["G01"]


def _resume(c: Aghoo) -> None:
    with pytest.raises(AghooError, match="resume is not served"):
        c.turns.stream(input=G01["request"]["body"]["input"], last_event_id="7", turn_id="turn_1").final_text()


CALLS: list[tuple[str, Callable[[Aghoo], Any]]] = [
    ("decision set", lambda c: c.decisions.create(Human, input="Call me", profile="router-default@1", deadline_ms=900)),
    ("decision list", lambda c: c.decisions.create(example("decision-request.valid")["questions"], input="x")),
    ("decision json state", lambda c: c.decisions.create(Human, **example("decision-request.valid-json-state"))),
    ("decision jev", lambda c: c.decisions.create(Human, output_format="jev", input="x", escalation="off")),
    ("decision retrieve", lambda c: c.decisions.retrieve("dec_1")),
    ("turn create", lambda c: c.turns.create(**G01["request"]["body"], decisions={"questions": Human.questions()})),
    ("turn stream", lambda c: c.turns.stream(**G01["request"]["body"]).final_text()),
    ("turn resume", _resume),
    ("turn retrieve", lambda c: c.turns.retrieve("turn_1")),
    ("turn cancel", lambda c: c.turns.cancel("turn_1")),
    ("profiles", lambda c: c.profiles.list(kind="decision")),
    ("session create", lambda c: c.sessions.create(metadata={"a": "b"}, data_class="internal", retention="default")),
    ("session retrieve", lambda c: c.sessions.retrieve("ses_1")),
    ("session list", lambda c: list(c.sessions.list(limit=2))),
    ("session update", lambda c: c.sessions.update("ses_1", metadata={}, data_class="confidential")),
    ("session turns", lambda c: list(c.sessions.turns("ses_1", limit=10, after="t2"))),
    ("session delete", lambda c: c.sessions.delete("ses_1")),
    ("file upload", lambda c: c.files.upload(b"%PDF-1.7", filename="a.pdf", session_id="ses_1")),
    ("file retrieve", lambda c: c.files.retrieve("file_1")),
    ("file watch", lambda c: list(c.files.watch("file_1"))),
    ("file delete", lambda c: c.files.delete("file_1")),
    ("file derivative", lambda c: c.files.derivative("file_1", "page_images", page=1)),
    ("usage", lambda c: list(c.usage.list(group_by="day", from_="2026-09-01", to="2026-09-30", limit=5))),
    ("calibrators", lambda c: list(c.admin.calibrators.list(profile_id="p", question_name="q", status="active"))),
    ("labeling runs", lambda c: list(c.admin.labeling.runs(status="completed", limit=1))),
    ("labeling dropped", lambda c: list(c.admin.labeling.dropped(run_id="lrun_1", reason="refused"))),
]


@pytest.fixture
def server(mock: respx.MockRouter) -> respx.MockRouter:
    turn_sse = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=b"".join(sse_frame(e["id"], e["type"], e["data"]) for e in G01["events"]),
    )
    mock.post("/v1/decisions").mock(
        side_effect=lambda r: httpx.Response(
            200,
            json=example(
                "decision-jev-response.valid" if json.loads(r.content).get("output_format") else "decision-result.valid"
            ),
        )
    )
    mock.get("/v1/decisions/dec_1").mock(return_value=httpx.Response(200, json=example("decision-result.valid")))
    mock.post("/v1/turns").mock(
        side_effect=lambda r: turn_sse if json.loads(r.content)["stream"] else respond("createTurn.200")
    )
    mock.get("/v1/turns/turn_1/events").mock(side_effect=lambda r: respond("resumeTurnEvents.501"))
    mock.get("/v1/turns/turn_1").mock(side_effect=lambda r: respond("getTurn.200"))
    mock.post("/v1/turns/turn_1/cancel").mock(side_effect=lambda r: respond("cancelTurn.202"))
    mock.get("/v1/profiles").mock(side_effect=lambda r: respond("listProfiles.200"))
    mock.post("/v1/sessions").mock(side_effect=lambda r: respond("createSession.201"))
    mock.get("/v1/sessions").mock(side_effect=lambda r: respond("listSessions.page2"))
    mock.get("/v1/sessions/ses_1").mock(side_effect=lambda r: respond("getSession.200"))
    mock.patch("/v1/sessions/ses_1").mock(side_effect=lambda r: respond("updateSession.200"))
    mock.delete("/v1/sessions/ses_1").mock(side_effect=lambda r: respond("deleteSession.202"))
    mock.get("/v1/sessions/ses_1/turns").mock(side_effect=lambda r: respond("listSessionTurns.page2"))
    mock.post("/v1/files").mock(side_effect=lambda r: respond("uploadFile.202"))
    mock.get("/v1/files/file_1").mock(side_effect=lambda r: respond("getFile.200"))
    mock.delete("/v1/files/file_1").mock(side_effect=lambda r: respond("deleteFile.200"))
    mock.get("/v1/files/file_1/events").mock(side_effect=lambda r: _sse("watchFile.events"))
    mock.get("/v1/files/file_1/derivatives/page_images").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG")
    )
    mock.get("/v1/usage").mock(side_effect=lambda r: respond("getUsage.page2"))
    mock.get("/v1/admin/calibrators").mock(side_effect=lambda r: respond("listCalibrators.200"))
    mock.get("/v1/admin/labeling/runs").mock(side_effect=lambda r: respond("listLabelingRuns.200"))
    mock.get("/v1/admin/labeling/dropped").mock(side_effect=lambda r: respond("listLabelingDropped.200"))
    return mock


@pytest.mark.parametrize(("label", "call"), CALLS, ids=[c[0] for c in CALLS])
def test_request_passes_the_server_gate(
    label: str, call: Callable[[Aghoo], Any], server: respx.MockRouter, client: Aghoo
) -> None:
    call(client)
    assert server.calls, label
    for captured in server.calls:
        check(captured.request)


def test_every_sdk_operation_is_exercised(server: respx.MockRouter, client: Aghoo) -> None:
    seen = set()
    for _, call in CALLS:
        call(client)
    for captured in server.calls:
        seen.add(check(captured.request))
    served = set(GATE.operations) - {"healthz", "readyz", "createDecisionOpenAICompat"}
    assert seen == served
