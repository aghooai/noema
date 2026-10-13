"""AsyncAghoo: the async code paths of every resource (the request building is shared with the sync client)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from aghoo import (
    AghooConnectionError,
    AghooError,
    AsyncAghoo,
    Choice,
    DecisionResult,
    DecisionSet,
    DerivativeContent,
    JevDecisionResponse,
    Predicate,
    Score,
    choice,
    predicate,
    score,
)
from support import API_KEY, BASE, example, fixture_json, respond, sse_frame


class Routing(DecisionSet):
    department: Choice[str] = choice("Which team?", ["billing", "technical", "shipping", "other"])
    needs_human: Predicate = predicate("Asks for a human.")
    urgency: Score[str] = score("How urgent?", ["low", "medium", "high"])


async def test_decisions(mock: respx.MockRouter, aclient: AsyncAghoo) -> None:
    route = mock.post("/v1/decisions").mock(return_value=httpx.Response(200, json=example("decision-result.valid")))
    typed = await aclient.decisions.create(Routing, input="I was charged twice.")
    assert typed.department.type == "choice"
    assert json.loads(route.calls[0].request.content)["questions"] == Routing.questions()
    plain = await aclient.decisions.create(Routing.questions(), input="x")
    assert isinstance(plain, DecisionResult)
    route.mock(return_value=httpx.Response(200, json=example("decision-jev-response.valid")))
    assert isinstance(await aclient.decisions.create(Routing, output_format="jev", input="x"), JevDecisionResponse)
    mock.get("/v1/decisions/dec_1").mock(return_value=httpx.Response(200, json=example("decision-result.valid")))
    assert (await aclient.decisions.retrieve("dec_1")).object == "decision"


async def test_profiles_sessions_and_pages(mock: respx.MockRouter, aclient: AsyncAghoo, sleeps: list[float]) -> None:
    mock.get("/v1/profiles").mock(side_effect=[httpx.Response(503, text="busy"), respond("listProfiles.200")])
    assert len(await aclient.profiles.list()) == 2
    assert sleeps == [0.25]
    mock.post("/v1/sessions").mock(side_effect=lambda r: respond("createSession.201"))
    mock.get("/v1/sessions/ses_1").mock(side_effect=lambda r: respond("getSession.200"))
    mock.patch("/v1/sessions/ses_1").mock(side_effect=lambda r: respond("updateSession.200"))
    mock.delete("/v1/sessions/ses_1").mock(side_effect=lambda r: respond("deleteSession.202"))
    assert (await aclient.sessions.create(data_class="internal")).id == "ses_1"
    assert (await aclient.sessions.retrieve("ses_1")).owner == "prn_01JA"
    assert (await aclient.sessions.update("ses_1", metadata={})).id == "ses_1"
    assert (await aclient.sessions.delete("ses_1")).deleted is True
    mock.get("/v1/sessions").mock(
        side_effect=lambda r: respond(
            "listSessions.page2" if r.url.params.get("after") == "c2" else "listSessions.page1"
        )
    )
    assert [s.id async for s in aclient.sessions.list()] == ["ses_3", "ses_2", "ses_1"]
    mock.get("/v1/sessions/ses_1/turns").mock(side_effect=lambda r: respond("listSessionTurns.page2"))
    assert [t.text async for t in aclient.sessions.turns("ses_1", after="t2")] == ["Thanks", "You're welcome."]


async def test_files(mock: respx.MockRouter, aclient: AsyncAghoo) -> None:
    upload = mock.post("/v1/files").mock(side_effect=lambda r: respond("uploadFile.202"))
    assert (await aclient.files.upload(b"%PDF", filename="a.pdf")).id == "file_1"
    assert b'name="session_id"' not in upload.calls[0].request.content
    mock.get("/v1/files/file_1").mock(side_effect=lambda r: respond("getFile.200"))
    mock.delete("/v1/files/file_1").mock(side_effect=lambda r: respond("deleteFile.200"))
    assert (await aclient.files.retrieve("file_1")).status == "ready"
    assert (await aclient.files.delete("file_1")).deleted is True
    derivative = mock.get("/v1/files/file_1/derivatives/page_images").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG")
    )
    assert await aclient.files.derivative("file_1", "page_images", page=2) == DerivativeContent(b"\x89PNG", "image/png")
    assert derivative.calls[0].request.url.params["page"] == "2"
    assert derivative.calls[0].request.headers["accept"] == "*/*"
    frames = fixture_json("watchFile.events")["frames"]
    mock.get("/v1/files/file_1/events").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"".join(sse_frame(f["id"], f["event"], f["data"]) for f in frames),
        )
    )
    assert [f.status async for f in aclient.files.watch("file_1")] == ["quarantined", "processing", "ready"]
    mock.get("/v1/files/file_2/events").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"")
    )
    with pytest.raises(AghooConnectionError, match="first event"):
        async for _ in aclient.files.watch("file_2", options={"max_retries": 0}):
            pass


async def test_usage_and_admin(mock: respx.MockRouter, aclient: AsyncAghoo) -> None:
    mock.get("/v1/usage").mock(side_effect=lambda r: respond("getUsage.page2"))
    assert [r.line async for r in aclient.usage.list(limit=1)] == ["cache_savings"]
    mock.get("/v1/admin/calibrators").mock(side_effect=lambda r: respond("listCalibrators.200"))
    mock.get("/v1/admin/labeling/runs").mock(side_effect=lambda r: respond("listLabelingRuns.200"))
    mock.get("/v1/admin/labeling/dropped").mock(side_effect=lambda r: respond("listLabelingDropped.200"))
    assert [c.status async for c in aclient.admin.calibrators.list()] == ["fitted"]
    assert [r.consensus_rate async for r in aclient.admin.labeling.runs()] == [0.7]
    assert [d.content.excerpt async for d in aclient.admin.labeling.dropped()] == ["I was charged twice"]
    mock.get("/v1/admin/labeling/runs").mock(side_effect=lambda r: respond("listCalibrators.403"))
    with pytest.raises(AghooError, match=r"may not read calibrators"):
        async for _ in aclient.admin.labeling.runs():
            pass


async def test_injected_http_client_is_not_closed(mock: respx.MockRouter) -> None:
    mock.get("/v1/profiles").mock(side_effect=lambda r: respond("listProfiles.200"))
    http = httpx.AsyncClient()
    async with AsyncAghoo(api_key=API_KEY, base_url=BASE, http_client=http) as c:
        await c.profiles.list(kind="decision")
    assert not http.is_closed
    await http.aclose()
