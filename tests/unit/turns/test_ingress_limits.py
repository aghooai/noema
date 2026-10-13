"""Ingress limits and the finished-turn registry:
- the data class never fails open (IMPL-004, spec-04 §12): a sessionless `store: true` turn with Postgres down at
  ingress is 500 `internal_error` (retryable), not stored under a laxer class; `store: false` continues;
- `history.last_n` (no contract maximum) is capped at `HISTORY_SCAN_LIMIT` for the `TurnRepo.recent` read;
- finished turns expire from the cancel registry by their resume window (heap, no full rebuild)."""

from __future__ import annotations

from typing import Any

import pytest
from tests.fakes.db_repos import FakeTurnRepo
from tests.golden.sse_scenarios import document, router_rules, stream

from noema.core.errors import ErrorCode, NoemaError
from noema.turns.context.history import DECISION_CONTEXT_TURNS, HISTORY_SCAN_LIMIT

from .harness import STICKY_SOL, USER_BODY, TurnHarness, make_harness, principal, run_turn, running

pytestmark = pytest.mark.unit

SOL = "us.openai.gpt-6.1-sol"
SECRET = {**USER_BODY, "input": [{"role": "user", "content": [{"type": "input_text", "text": "my SSN secret"}]}]}


def _h(*, project_class: str | None = None) -> TurnHarness:
    sol = stream(SOL, first_event_ms=100, first_text_ms=720, chunks=["secret answer"], step_ms=100)
    h = make_harness(scenario=document("limits", [sol], router_rules()))
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    if project_class is not None:
        h.world.db.store.seed(
            "project",
            {"tenant_id": "t_acme", "id": "p1", "name": "p1", "default_data_class": project_class, "deleted_at": None},
        )
    return h


async def test_restricted_project_default_applies_to_sessionless_turns() -> None:
    h = _h(project_class="restricted")
    async with running(h):
        runner = await run_turn(h, SECRET)
    assert runner.p.data_class == "restricted"
    (row,) = h.world.db.store.rows("turn")
    assert row["content_policy"] == "metadata_only" and row["input_parts"] is None and row["output_text"] is None


async def test_postgres_down_at_ingress_fails_a_sessionless_store_true_turn_closed() -> None:
    h = _h(project_class="restricted")
    h.world.db.store.available = False
    error: NoemaError | None = None
    async with running(h):
        try:
            runner = await h.start(SECRET)
        except NoemaError as exc:
            error = exc
        else:  # a regression: stop the turn so the runtime closes, then fail
            h.world.db.store.available = True
            runner.request_cancel("cancelled")
            await runner.done.wait()
    assert error is not None and error.code is ErrorCode.INTERNAL_ERROR and error.spec.retryable
    assert h.world.db.store.rows("turn") == [] and h.world.admission.live_streams("t_acme", "p1") == 0


async def test_postgres_down_at_ingress_lets_a_sessionless_store_false_turn_continue() -> None:
    h = _h(project_class="restricted")
    h.world.db.store.available = False
    async with running(h):
        runner = await run_turn(h, {**SECRET, "store": False})
    assert runner.result is not None and runner.result.status == "completed"
    assert h.world.db.store.rows("turn") == []


@pytest.mark.parametrize(
    ("history", "limit"),
    [
        ({"mode": "last_n", "last_n": 2_000_000_000}, HISTORY_SCAN_LIMIT),
        ({"mode": "last_n", "last_n": 1}, DECISION_CONTEXT_TURNS),  # the decision context still needs 2
        ({"mode": "last_n", "last_n": 10}, 10),
        ({"mode": "auto"}, HISTORY_SCAN_LIMIT),
    ],
)
async def test_history_reads_are_capped(monkeypatch: pytest.MonkeyPatch, history: Any, limit: int) -> None:
    seen: list[int] = []
    original = FakeTurnRepo.recent

    async def spy(self: Any, session_id: str, *, max_seq: int, limit: int) -> Any:
        seen.append(limit)
        return await original(self, session_id, max_seq=max_seq, limit=limit)

    monkeypatch.setattr(FakeTurnRepo, "recent", spy)
    h = _h()
    async with running(h):
        await run_turn(h, {**USER_BODY, "session_id": "ses_1", "history": history})
    assert seen == [limit]


async def test_finished_turns_expire_from_the_cancel_registry_after_the_resume_window() -> None:
    h = _h()
    window = h.world.config.current().platform.events.resume_window_s
    async with running(h):
        old = await run_turn(h, {**USER_BODY, "store": False})
        await h.clock.sleep(window + 1)
        new = await run_turn(h, {**USER_BODY, "store": False})  # its finish evicts the expired entry
        assert set(h.service.finished) == {new.turn_id}
        assert await h.service.cancel(principal(), new.turn_id) == {"turn_id": new.turn_id, "status": "completed"}
        with pytest.raises(NoemaError) as gone:
            await h.service.cancel(principal(), old.turn_id)
    assert gone.value.code is ErrorCode.NOT_FOUND


def test_more_than_max_media_parts_is_a_400_before_any_lookup() -> None:
    """A turn's media parts (file_id and inline images) are capped at `MAX_MEDIA_PARTS` while the body is read, before
    step 6's file lookups (400 `schema_violation`, `param: input`; Wave E, like createDecision)."""
    from noema.turns.request import MAX_MEDIA_PARTS, parse_turn_request

    def body(n: int) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": "what are these?"}]
        content += [{"type": "input_image", "file_id": f"file_{i:04d}"} for i in range(n)]
        return {**USER_BODY, "input": [{"role": "user", "content": content}]}

    assert len(parse_turn_request(body(MAX_MEDIA_PARTS)).file_parts) == MAX_MEDIA_PARTS
    with pytest.raises(NoemaError) as err:
        parse_turn_request(body(MAX_MEDIA_PARTS + 1))
    assert (err.value.code, err.value.param) == (ErrorCode.SCHEMA_VIOLATION, "input")
    assert err.value.details == {"max_media_parts": MAX_MEDIA_PARTS}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


async def test_nul_in_the_input_or_output_is_stored_as_u_fffd() -> None:
    """U+0000 cannot be stored in a Postgres text or jsonb column: the turn's stored input and output carry U+FFFD
    instead (same length, so redaction spans stay valid), so the finalize neither fails nor poisons the outbox and
    getTurn finds the turn its 200 named (Wave E fix)."""
    sol = stream(SOL, first_event_ms=100, first_text_ms=720, chunks=["an\x00swer"], step_ms=100)
    h = make_harness(scenario=document("nul", [sol], router_rules()))
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    content = [{"type": "input_text", "text": "hi \x00 there"}, {"type": "input_json", "json": {"k\x00": ["v\x00"]}}]
    body = {**USER_BODY, "session_id": "ses_1", "input": [{"role": "user", "content": content}]}
    async with running(h):
        runner = await run_turn(h, body)
    assert runner.result is not None and runner.result.status == "completed"
    (row,) = h.world.db.store.rows("turn")
    assert not [s for s in _strings(row) if "\x00" in s]
    assert row["output_text"] == "an�swer"
    assert row["input_parts"][0]["text"] == "hi � there"
