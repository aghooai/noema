"""Minimal valid spec-03 rows for the repository tests, principals and superuser helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import asyncpg

from noema.core.clock import SystemClock
from noema.core.ids import UlidIdGen
from noema.core.principal import ALL_SCOPES, Principal, Role

type Conn = asyncpg.Connection[asyncpg.Record]

_IDS = UlidIdGen(SystemClock())  # monotonic: ids sort in creation order (id DESC = newest first)
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{_IDS.ulid()}"


def principal(tenant_id: str, project_id: str, *roles: Role, principal_id: str = "prn_test") -> Principal:
    return Principal(
        tenant_id=tenant_id,
        project_id=project_id,
        principal_id=principal_id,
        key_id="K",
        roles=frozenset(roles or ("developer",)),
        scopes=ALL_SCOPES,
        request_id="req_test",
    )


def session_row(owner: str = "prn_test", **extra: Any) -> dict[str, Any]:
    return {"id": new_id("ses"), "owner_principal_id": owner, "data_class": "internal", **extra}


def turn_row(**extra: Any) -> dict[str, Any]:
    return {
        "id": new_id("turn"),
        "principal_id": "prn_test",
        "status": "completed",
        "content_policy": "full",
        "input_parts": [{"type": "text", "text": "hi"}],
        "output_text": "hello",
        "routing": {"profile": "fast"},
        "usage": {"input_tokens": 3, "output_tokens": 2},
        "timings": {"e2e_ms": 10},
        "created_at": NOW,
        "completed_at": NOW,
        **extra,
    }


def attempt_row(started_at: datetime = NOW, **extra: Any) -> dict[str, Any]:
    return {
        "id": new_id("att"),
        "route_id": "r1",
        "model": "amazon.nova-lite-v1:0",
        "spec_signature": "sig",
        "outcome": "committed",
        "usage": {"input_tokens": 3},
        "cost_usd": Decimal("0.000123"),
        "cost_estimated": False,
        "started_at": started_at,
        "ended_at": started_at,
        **extra,
    }


def decision_row(**extra: Any) -> dict[str, Any]:
    return {
        "id": new_id("dec"),
        "created_at": NOW,
        "principal_id": "prn_test",
        "purpose": "decision",
        "origin": "native",
        "output_format": "native",
        "profile_id": "lmd",
        "profile_version": "1",
        "question_signature": "qs",
        "input_view_signature": "vs",
        "input_digest": "dg",
        "status": "completed",
        "cache_hit": False,
        "questions": [{"name": "q1"}],
        "input": {"messages": []},
        "result": {"answers": []},
        **extra,
    }


def file_row(**extra: Any) -> dict[str, Any]:
    return {
        "id": new_id("file"),
        "principal_id": "prn_test",
        "origin": "upload",
        "declared_type": "image/png",
        "bytes": 10,
        "sha256": "00" * 32,
        "status": "quarantined",
        "quarantine_key": "q/key",
        **extra,
    }


def usage_row(tenant_id: str, project_id: str, line: str, at: datetime, **extra: Any) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "id": new_id("use"),
        "line": line,
        "model_id": None if line == "cache_savings" else "amazon.nova-lite-v1:0",
        "region": None if line == "cache_savings" else "us-east-1",
        "input_tokens": 10,
        "output_tokens": 5,
        "cached_input_tokens": 1,
        "cache_write_input_tokens": 0,
        "reasoning_tokens": 0,
        "tokens_estimated": False,
        "cost_usd": Decimal("0.001000"),
        "cost_estimated": False,
        "at": at,
        **extra,
    }


def run_row(**extra: Any) -> dict[str, Any]:
    return {
        "id": new_id("lrun"),
        "profile_id": "lmd",
        "profile_version": "1",
        "question_name": "q1",
        "question_signature": "qs",
        "status": "running",
        "labelers": ["m1", "m2"],
        "rule": "unanimous",
        "counts": {"kept": 0},
        "usage": {},
        "started_at": NOW,
        **extra,
    }


def item_row(run_id: str, *, kept: bool = True, **extra: Any) -> dict[str, Any]:
    return {
        "id": new_id("litem"),
        "run_id": run_id,
        "decision_id": new_id("dec"),
        "question_name": "q1",
        "sample_reason": "uniform",
        "labeler_answers": [
            {"model": "m1", "status": "ok", "value": "a"},
            {"model": "m2", "status": "ok", "value": "a"},
        ],
        "kept": kept,
        "value": "a" if kept else None,
        "drop_reason": None if kept else "disagreed",
        "ensemble": {"value": "a", "probability": 0.9},
        "content_excerpt": "some text",
        "redacted": False,
        "input_view_signature": "vs",
        "member_subset": ["m1"],
        **extra,
    }


def calibrator_row(**extra: Any) -> dict[str, Any]:
    return {
        "id": new_id("cal"),
        "profile_id": "lmd",
        "profile_version": "1",
        "question_name": "q1",
        "question_signature": "qs",
        "member_subset": ["m1"],
        "input_view_signature": "vs",
        "method": "temperature",
        "member_t": {"m1": 1.2},
        "pooled": {},
        "holdout": {},
        "acceptance": {},
        "reliability": {},
        **extra,
    }


async def admin_insert(admin: Conn, table: str, row: Mapping[str, Any]) -> None:
    """INSERT past RLS (superuser); `table` is schema-qualified, keys are trusted test column names."""
    columns = ", ".join(f'"{c}"' for c in row)
    values = ", ".join(f"${i}" for i in range(1, len(row) + 1))
    await admin.execute(f"INSERT INTO {table} ({columns}) VALUES ({values})", *row.values())
