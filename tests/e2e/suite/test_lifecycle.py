"""Deletion and stream lifecycle on the stack: deleteSession runs `session.delete` on the jobs process (the session's
turn and file are gone, the file's objects purged from the bucket), deleteFile runs `file.purge` on the media-worker,
a dropped client connection is not a cancel (the turn completes and is stored), and resumeTurnEvents is the Phase-1
501 on both pods (IMPL-036; cross-pod attach goes through the idempotent duplicate, test_turns_attach.py)."""

from __future__ import annotations

import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import Json, eventually, live_turn, s3_keys, tag, turn_body
from tests.e2e.suite.multipod import wait_for
from tests.e2e.suite.turns import new_session, run_turn, stored_turn
from tests.unit.media_pipeline import media_fixtures as fx

from noema.media.quarantine import derivative_prefix, quarantine_key

pytestmark = [pytest.mark.e2e]
TERMINAL = frozenset({"ready", "partial", "rejected", "failed"})


async def upload_png(stack: Stack, *, session_id: str | None = None) -> Json:
    """A small PNG through the media-worker (processed), optionally bound to a session (`session_id` before `file`)."""
    fields = {"purpose": "input", **({"session_id": session_id} if session_id else {})}
    files = {"file": (f"pixel-{tag()}.png", fx.png(64, 48), "image/png")}
    accepted = await stack.client(0).request("POST", "/v1/files", data=fields, files=files, expect=202)
    path = f"/v1/files/{accepted.json()['id']}"
    file = await eventually(stack.client(1), path, lambda b: b["status"] in TERMINAL, timeout_s=90)
    assert file["status"] in ("ready", "partial"), file  # a tiny PNG has nothing to OCR (partial)
    return file


async def objects(stack: Stack, tenant: str, file_id: str) -> list[str]:
    return [
        *await s3_keys(stack, quarantine_key(tenant, file_id)),
        *await s3_keys(stack, derivative_prefix(tenant, file_id)),
    ]


async def gone(stack: Stack, path: str, *, timeout_s: float = 60.0) -> None:
    """GET `path` on api-1 until 404 (a job on another process removes it)."""

    async def missing() -> bool:
        return (await stack.client(0).request("GET", path)).status_code == 404

    await wait_for(missing, timeout_s=timeout_s, every_s=0.5)


async def purged(stack: Stack, file_id: str, *, timeout_s: float = 60.0) -> None:
    """No object of the file is left in the bucket."""

    async def empty() -> bool:
        return not await objects(stack, "t_acme", file_id)

    await wait_for(empty, timeout_s=timeout_s, every_s=0.5)


async def test_delete_session_runs_the_job_on_the_jobs_process(stack: Stack, mock: Mock) -> None:
    await mock.load(
        sc.document("e2e-delete-session", [sc.stream(sc.SOL, first_text_ms=200, step_ms=20)], sc.router(ms=100))
    )
    session_id = await new_session(stack.client(0))
    turn = await run_turn(stack.client(0), f"What is the capital of France? ({tag()})", session_id=session_id)
    await stored_turn(stack, turn.turn_id)
    file = await upload_png(stack, session_id=session_id)
    assert await objects(stack, "t_acme", file["id"])  # the original and its derivatives
    await stack.client(1).request("DELETE", f"/v1/sessions/{session_id}", expect=202)
    await stack.client(0).request("GET", f"/v1/sessions/{session_id}", expect=404)  # marked deleted at once
    await gone(stack, f"/v1/turns/{turn.turn_id}")  # session.delete (jobs): the session's turns,
    await gone(stack, f"/v1/files/{file['id']}")  # its files (FileService.purge) ...
    await purged(stack, file["id"])  # ... down to the bucket objects


async def test_delete_file_runs_the_purge_on_the_media_worker(stack: Stack) -> None:
    file = await upload_png(stack)
    await stack.client(1).request("DELETE", f"/v1/files/{file['id']}", expect=200)
    await stack.client(0).request("GET", f"/v1/files/{file['id']}", expect=404)  # reads stop at once
    await stack.client(0).request("GET", f"/v1/files/{file['id']}/derivatives/raster", expect=404)
    await purged(stack, file["id"])  # file.purge (media-worker): the quarantine object and every derivative


async def test_a_dropped_connection_is_not_a_cancel(stack: Stack, mock: Mock) -> None:
    await mock.load(sc.document("e2e-disconnect", [sc.stream(sc.SOL, first_text_ms=300, step_ms=300)], sc.router()))
    async with live_turn(stack.client(0), turn_body(f"What is the capital of France? ({tag()})")) as turn:
        await turn.until(lambda e: e.event == "generation.committed")
        turn_id = turn.turn_id
    # the client went away mid-stream (the connection is closed here); the turn runs to its end on api-1
    stored = await stored_turn(stack, turn_id, timeout_s=30)
    assert stored["text"] == sc.TEXT, stored  # the whole answer: not cut by a cancel
    assert [a["outcome"] for a in stored["attempts"]] == ["committed"], stored["attempts"]


async def test_resume_turn_events_is_501_on_both_pods(stack: Stack, mock: Mock) -> None:
    await mock.load(sc.document("e2e-resume", [sc.stream(sc.SOL, first_text_ms=200, step_ms=20)], sc.router(ms=100)))
    turn = await run_turn(stack.client(0), f"What is the capital of France? ({tag()})")
    for replica in (0, 1):
        response = await stack.client(replica).request(
            "GET", f"/v1/turns/{turn.turn_id}/events", headers={"last-event-id": "3"}, expect=501
        )
        assert response.json()["error"]["code"] == "not_implemented"
