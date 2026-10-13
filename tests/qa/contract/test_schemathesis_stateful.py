"""Gate Q1, stateful: schemathesis' state machine over the links it infers from the bundle (dependency analysis: 60
transitions, e.g. createSession -> getSession / updateSession / listSessionTurns / deleteSession / createTurn
(session_id), createTurn (stream: false) -> getTurn / cancelTurn, createDecision -> getDecision, uploadFile ->
getFile -> watchFile / deleteFile, listLabelingRuns -> listLabelingDropped (run_id)).

Every step goes to api-1 with real auth (fuzzkit.auth_headers); `before_call` keeps the producers productive.
Pass criterion per step: fuzzkit.GATED (no 5xx but resumeTurnEvents' 501, declared status / content type /
headers, schema-valid body, the ContractValidator), plus two stateful checks: use_after_free (a resource read
back 2xx after its successful DELETE) and ensure_resource_availability (a resource read back 404 right after its
successful create), except reads of a turn created with `store: false`, which are 404 by contract (G11).
Failures are collected and asserted at the end.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, ClassVar

import httpx
import orjson
import pytest
from hypothesis import HealthCheck, Phase, settings
from schemathesis import CheckContext, CheckFunction, Response
from schemathesis.specs.openapi.checks import ensure_resource_availability, use_after_free
from tests.qa.contract.fuzzkit import GATED, SCHEMA, Case, Tally, auth_headers
from tests.qa.contract.resources import REPORT_DIR, TERMINAL_FILE

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(1800)]
SCENARIOS = int(os.environ.get("NOEMA_QA_STATEFUL_SCENARIOS", "150"))
STEPS = int(os.environ.get("NOEMA_QA_STATEFUL_STEPS", "10"))
END_USER = "x-aghoo-end-user"
IDEMPOTENCY = "Idempotency-Key"
AUTHFAIL_PAUSE_S = 0.15
SETTLE_S, SETTLE_POLL_S = 60.0, 0.25


def _ephemeral_turn_read(ctx: CheckContext, case: Case) -> bool:
    parent = ctx._find_parent(case_id=case.id)
    body = parent.body if parent is not None else None
    return isinstance(body, dict) and body.get("store") is False


def resource_availability(ctx: CheckContext, response: Response, case: Case) -> bool | None:
    if _ephemeral_turn_read(ctx, case):
        return None
    return ensure_resource_availability(ctx, response, case)


CHECKS: list[CheckFunction] = [*GATED, use_after_free, resource_availability]
Base: Any = SCHEMA.as_state_machine()


class StackStateMachine(Base):  # type: ignore[misc]
    state: ClassVar[dict[str, Any]] = {}
    tally: ClassVar[Tally] = Tally()
    failures: ClassVar[list[str]] = []

    def before_call(self, case: Case) -> None:
        """Steers createTurn towards resources the links can follow: `stream: false` (an SSE body has no `turn_id`
        to extract). uploadFile stays as generated: since finding 18 the file part carries one of the declared
        media types, so some uploads are accepted (202; `after_call` lets them settle) and the rest answer 415 (no
        accepted extension) or 400; the stateless test's real-id pass covers the file operations."""
        if case.headers and END_USER in case.headers:  # Phase 1: 422 feature_not_available (see the stateless test)
            del case.headers[END_USER]
        key = case.headers.get(IDEMPOTENCY) if case.headers else None
        if isinstance(key, str) and 0 < len(key) <= 115:  # a key generated again in a later scenario (or by the
            # stateless test) replays or answers 409 depending on what ran before: one fresh key per call (a valid
            # key stays valid: <= 128 printable ASCII; a negative case stays invalid)
            case.headers[IDEMPOTENCY] = f"{key}-{uuid.uuid4().hex[:12]}"
        op_id = case.operation.definition.raw["operationId"]
        if op_id == "createTurn" and isinstance(case.body, dict):
            case.body["stream"] = False

    def after_call(self, response: Response, case: Case) -> None:
        """An accepted upload is processed asynchronously: wait (bounded getFile poll) until it leaves quarantined /
        processing, so every later step that uses the file (createTurn / createDecision file parts, getFile,
        watchFile) meets a settled state. Otherwise a step's answer depends on the media sandbox's backlog, and
        hypothesis' stateful data tree raises FlakyStrategyDefinition when the same choices meet another answer."""
        super().after_call(response, case)
        if case.operation.definition.raw["operationId"] != "uploadFile" or response.status_code != 202:
            return
        file_id = orjson.loads(response.content)["id"]
        headers = auth_headers(self.state, "getFile")
        deadline = time.monotonic() + SETTLE_S
        with httpx.Client(base_url=self.state["endpoints"]["api"][0], headers=headers, timeout=30.0) as http:
            while http.get(f"/v1/files/{file_id}").json().get("status") not in TERMINAL_FILE:
                assert time.monotonic() < deadline, f"{file_id} not settled after {SETTLE_S} s"
                time.sleep(SETTLE_POLL_S)

    def get_call_kwargs(self, case: Case) -> dict[str, Any]:
        op_id = str(case.operation.definition.raw["operationId"])
        return {"base_url": self.state["endpoints"]["api"][0], "headers": auth_headers(self.state, op_id)}

    def validate_response(
        self, response: Response, case: Case, additional_checks: list[CheckFunction] | None = None, **kwargs: Any
    ) -> None:
        op_id = str(case.operation.definition.raw["operationId"])
        self.tally.add(op_id, response)
        if response.status_code == 401:
            time.sleep(AUTHFAIL_PAUSE_S)
        try:
            case.validate_response(
                response,
                checks=CHECKS,
                headers=kwargs.get("headers"),
                transport_kwargs=kwargs,
                recorder=getattr(self, "recorder", None),
            )
        except (Exception, BaseExceptionGroup) as exc:
            detail = " | ".join(str(exc).strip().splitlines()[:12])
            self.failures.append(f"- {op_id} {response.status_code}: {detail}")


def test_stateful_links_on_the_stack(sync_stack: dict[str, Any]) -> None:
    StackStateMachine.state = sync_stack
    StackStateMachine.run(
        settings=settings(
            max_examples=SCENARIOS,
            stateful_step_count=STEPS,
            deadline=None,
            derandomize=True,
            database=None,
            phases=[Phase.explicit, Phase.generate],
            suppress_health_check=list(HealthCheck),
        )
    )
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    lines = StackStateMachine.tally.lines()
    (REPORT_DIR / "schemathesis-stateful.txt").write_text("\n".join(lines) + "\n")
    (REPORT_DIR / "schemathesis-stateful-failures.json").write_bytes(orjson.dumps(StackStateMachine.failures))
    print("\n".join(lines))
    failures = StackStateMachine.failures
    assert not failures, f"{len(failures)} stateful contract failure(s):\n" + "\n".join(failures[:20])
