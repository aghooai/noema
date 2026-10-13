"""The W4 read-only admin views (IMPL-042; spec-08 §14.3 `CalibratorInfo`; spec-09 §3).

`list_calibrators`, `list_runs` and `list_dropped` open `db.uow(principal)` (tenant-wide: labeling and calibrator
rows have no project), page the repo (id DESC, keyset; a bad cursor or limit raises `schema_violation`) and map the
rows to the closed contract objects `CalibratorList`, `LabelingRunList` and `LabelingDroppedList`. Calibrators are
the tenant's own rows plus every platform row (RLS `cal_read`). Dropped items show the stored redacted excerpt, which
is NULL whenever the data class or log policy forbade stored content. Only declared fields are emitted (the
labeling jobs keep extra bookkeeping keys inside `labeler_answers` and `ensemble`).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Final

from noema.core.principal import Principal
from noema.core.protocols.storage import Database, Page, Row
from noema.core.values import JsonValue
from noema.labeling.runs_open import ZERO_COUNTS, ZERO_USAGE

_BANDS: Final = frozenset({"high", "medium", "low"})
_CAL_STATUS: Final = frozenset({"calibrated", "provider_reported", "uncalibrated"})
_LABELER_STATUS: Final = frozenset({"ok", "refused", "parse_error", "error"})


def _unix(value: object) -> int | None:
    return int(value.timestamp()) if isinstance(value, datetime) else None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return None
    return float(value)


def _floats(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, JsonValue] = {}
    for k, v in value.items():
        number = _float(v)
        if number is not None:
            out[str(k)] = number
    return out


def _json_dict(value: object) -> dict[str, JsonValue]:
    return dict(value) if isinstance(value, dict) else {}


def _strs(value: object) -> list[JsonValue]:
    return [str(v) for v in value] if isinstance(value, list | tuple) else []


def calibrator_info(row: Row) -> dict[str, JsonValue]:
    """One `CalibratorInfo`."""
    holdout = row.get("holdout")
    return {
        "id": str(row["id"]),
        "object": "calibrator",
        "scope": str(row["scope"]),
        "question_name": str(row["question_name"]),
        "status": str(row["status"]),
        "method": str(row["method"]),
        "parameters": {
            "pooled": _floats(row.get("pooled")),
            "member_T": _floats(row.get("member_t")),
            "epsilon": _float(row.get("epsilon")) or 1e-4,
        },
        "binding": {
            "profile_id": str(row["profile_id"]),
            "profile_version": str(row["profile_version"]),
            "question_signature": str(row["question_signature"]),
            "input_view_signature": str(row["input_view_signature"]),
            "choice_method": str(row.get("choice_method") or "flat"),
        },
        "member_subset": _strs(row.get("member_subset")),
        "holdout": dict(holdout) if isinstance(holdout, dict) and holdout else None,
        "acceptance": _json_dict(row.get("acceptance")) or {"passed": False, "failed_rules": []},
        "reliability": _json_dict(row.get("reliability")) or {"bins": [], "ci": {}},
        "created_at": _unix(row.get("fitted_at")) or 0,
        "approved_by": _opt_str(row.get("approved_by")),
        "activated_by": _strs(row.get("activated_by")),
        "activated_at": _unix(row.get("activated_at")),
        "downgraded_at": _unix(row.get("downgraded_at")),
        "downgrade_reason": _opt_str(row.get("downgrade_reason")),
        "retired_at": _unix(row.get("retired_at")),
        "retired_reason": _opt_str(row.get("retired_reason")),
    }


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def labeling_run(row: Row) -> dict[str, JsonValue]:
    """One `LabelingRun`."""
    rate = _float(row.get("consensus_rate"))
    n_by_class = row.get("n_by_class")
    return {
        "id": str(row["id"]),
        "object": "labeling_run",
        "status": str(row["status"]),
        "profile_id": str(row["profile_id"]),
        "profile_version": str(row["profile_version"]),
        "question_name": str(row["question_name"]),
        "question_signature": str(row["question_signature"]),
        "labelers": _strs(row.get("labelers")),
        "rule": "unanimous",
        "counts": {**ZERO_COUNTS, **_json_dict(row.get("counts"))},
        "consensus_rate": rate,
        "consensus_rate_by_class": _floats(row.get("consensus_rate_by_class")),
        "n_by_class": {str(k): int(v) for k, v in n_by_class.items()} if isinstance(n_by_class, dict) else {},
        "usage": {**ZERO_USAGE, **_json_dict(row.get("usage"))},
        "started_at": _unix(row.get("started_at")) or 0,
        "completed_at": _unix(row.get("completed_at")),
    }


def _ensemble(value: object) -> dict[str, JsonValue]:
    doc = value if isinstance(value, dict) else {}
    raw = doc.get("value")
    band = doc.get("band")
    status = doc.get("calibration_status")
    probability = _float(doc.get("probability"))
    return {
        "value": raw if isinstance(raw, str | bool) else None,
        "probability": None if probability is None else min(max(probability, 0.0), 1.0),
        "band": band if band in _BANDS else None,
        "calibration_status": status if status in _CAL_STATUS else "uncalibrated",
    }


def _labeler_answers(value: object) -> list[JsonValue]:
    out: list[JsonValue] = []
    for a in value if isinstance(value, list) else []:
        if not isinstance(a, dict):
            continue
        status = a.get("status") if a.get("status") in _LABELER_STATUS else "error"
        raw = a.get("value")
        out.append(
            {
                "model": str(a.get("model")),
                "status": status,
                "value": raw if status == "ok" and isinstance(raw, str | bool) else None,
            }
        )
    return out


def dropped_item(row: Row, question_signature: str) -> dict[str, JsonValue]:
    """One `LabelingDroppedItem` (`question_signature` comes from its run)."""
    excerpt = row.get("content_excerpt")
    return {
        "id": str(row["id"]),
        "object": "labeling_dropped_item",
        "run_id": str(row["run_id"]),
        "decision_id": str(row["decision_id"]),
        "question_name": str(row["question_name"]),
        "question_signature": question_signature,
        "input_view_signature": str(row["input_view_signature"]),
        "reason": str(row["drop_reason"]),
        "sample_reason": str(row["sample_reason"]),
        "labeler_answers": _labeler_answers(row.get("labeler_answers")),
        "ensemble": _ensemble(row.get("ensemble")),
        "content": {"excerpt": excerpt if isinstance(excerpt, str) else None, "redacted": bool(row.get("redacted"))},
        "sampled_at": _unix(row.get("created_at")) or 0,
    }


def _page(page: Page[Row], data: list[JsonValue]) -> dict[str, JsonValue]:
    return {"data": data, "has_more": page.has_more, "next_cursor": page.next_cursor}


async def list_calibrators(
    db: Database,
    principal: Principal,
    *,
    profile_id: str | None,
    question_name: str | None,
    status: str | None,
    after: str | None,
    limit: int,
) -> dict[str, JsonValue]:
    """`CalibratorList`: the tenant's artifacts and the platform ones, newest first."""
    async with db.uow(principal) as uow:
        page = await uow.calibrators.list(
            profile_id=profile_id, question_name=question_name, status=status, after=after, limit=limit
        )
    return _page(page, [calibrator_info(r) for r in page.data])


async def list_runs(
    db: Database,
    principal: Principal,
    *,
    profile_id: str | None,
    question_name: str | None,
    status: str | None,
    after: str | None,
    limit: int,
) -> dict[str, JsonValue]:
    """`LabelingRunList`, newest first."""
    async with db.uow(principal) as uow:
        page = await uow.labels.list_runs(
            profile_id=profile_id, question_name=question_name, status=status, after=after, limit=limit
        )
    return _page(page, [labeling_run(r) for r in page.data])


async def list_dropped(
    db: Database,
    principal: Principal,
    *,
    run_id: str | None,
    question_name: str | None,
    reason: str | None,
    after: str | None,
    limit: int,
) -> dict[str, JsonValue]:
    """`LabelingDroppedList`, newest first; each item's question signature is read from its run."""
    async with db.uow(principal) as uow:
        page = await uow.labels.list_dropped(
            run_id=run_id, question_name=question_name, reason=reason, after=after, limit=limit
        )
        signatures: dict[str, str] = {}
        for rid in sorted({str(r["run_id"]) for r in page.data}):
            run = await uow.labels.update_run(rid)
            signatures[rid] = str(run["question_signature"])
    return _page(page, [dropped_item(r, signatures[str(r["run_id"])]) for r in page.data])


def params_of(params: Mapping[str, JsonValue], *names: str) -> dict[str, str | None]:
    """The optional string query parameters `names` (None when absent)."""
    return {n: (v if isinstance(v := params.get(n), str) else None) for n in names}
