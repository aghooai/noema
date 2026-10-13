"""Usage-line ids and the `usage_line` outbox payload (spec-11 §2, IMPL-020).

Every recorder derives its line id deterministically, `h(x) = sha256(x).hexdigest()[:26]`, so an outbox replay of
the same line is an `ON CONFLICT DO NOTHING` no-op at the sink and Σ lines equals the response usage exactly:

| Recorder | Line(s) | Id |
|---|---|---|
| `DecisionRun.finalize` | decision, verification, waste | `h(f"{decision_id}:{line}:{model_id}")` |
| `DecisionRun.finalize` on an L2 hit | cache_savings | `h(f"{decision_id}:cache_savings")` |
| the orchestrator at turn completion | generation, waste | `h(f"{attempt_id}:{line}")` |
| the labeling job | labeling | `h(f"labeling:{job_id}:{attempt}:{question_name}:{model_id}")` |

The other domains compute the same ids themselves (domain modules never import each other); these functions are
the reference the ledger tests pin.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Final

from noema.core.protocols.ops import UsageLine
from noema.core.protocols.storage import OutboxRecord
from noema.core.values import JsonValue, UsageLineName

LINE_ID_LEN: Final = 26


def line_hash(text: str) -> str:
    """`sha256(text).hexdigest()[:26]` (spec-11 §2)."""
    return hashlib.sha256(text.encode()).hexdigest()[:LINE_ID_LEN]


def decision_line_id(decision_id: str, line: UsageLineName, model_id: str) -> str:
    """One decision, verification or waste line per (decision, line, Bedrock id)."""
    return line_hash(f"{decision_id}:{line}:{model_id}")


def cache_savings_line_id(decision_id: str) -> str:
    """The cache_savings line of an L2 hit."""
    return line_hash(f"{decision_id}:cache_savings")


def attempt_line_id(attempt_id: str, line: UsageLineName) -> str:
    """The generation (committed) or waste line of one attempt."""
    return line_hash(f"{attempt_id}:{line}")


def labeling_line_id(job_id: str, attempt: int, question_name: str, model_id: str) -> str:
    """One labeling line per (job attempt, question, labeler)."""
    return line_hash(f"labeling:{job_id}:{attempt}:{question_name}:{model_id}")


def rfc3339(at: datetime) -> str:
    """UTC RFC 3339 with a `Z` suffix (the outbox payload convention, spec-02 §7). Naive datetimes are UTC."""
    aware = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def usage_line_payload(line: UsageLine, at: datetime) -> dict[str, JsonValue]:
    """The `noema.usage_line` row of `line` (cost as a decimal string, `at` RFC 3339 UTC)."""
    u = line.usage
    return {
        "tenant_id": line.tenant_id,
        "project_id": line.project_id,
        "id": line.id,
        "line": line.line,
        "model_id": line.model_id,
        "region": line.region,
        "session_id": line.session_id,
        "turn_id": line.turn_id,
        "decision_id": line.decision_id,
        "attempt_id": line.attempt_id,
        "labeling_run_id": line.labeling_run_id,
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cached_input_tokens": u.cached_input_tokens,
        "cache_write_input_tokens": u.cache_write_input_tokens,
        "reasoning_tokens": u.reasoning_tokens,
        "tokens_estimated": u.tokens_estimated,
        "cost_usd": format(line.cost.usd, "f"),
        "cost_estimated": line.cost.estimated,
        "at": rfc3339(at),
    }


def usage_line_record(line: UsageLine, at: datetime) -> OutboxRecord:
    """The `usage_line` outbox record of `line` (`id` = the line id, the target primary key)."""
    return OutboxRecord(kind="usage_line", id=line.id, tenant_id=line.tenant_id, payload=usage_line_payload(line, at))
