"""spec-03 row builders for the database suites (complete NOT NULL columns, so the real `db/repos` accept them)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .kits import DatabaseKit

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
USAGE_JSON = {
    "input_tokens": 10,
    "output_tokens": 5,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "reasoning_tokens": 0,
    "cost_usd": 0.0,
    "cost_estimated": True,
    "tokens_estimated": False,
}


async def seed_tenancy(kit: DatabaseKit, tenant_id: str = "t_acme", projects: tuple[str, ...] = ("p1", "p2")) -> None:
    """A tenant, its projects and one principal per project (the FKs of session and api_key rows)."""
    await kit.seed("tenant", {"id": tenant_id, "aws_account_id": "111122223333", "created_at": T0, "deleted_at": None})
    for p in projects:
        await kit.seed(
            "project",
            {
                "tenant_id": tenant_id,
                "id": p,
                "name": p,
                "default_data_class": "internal",
                "created_at": T0,
                "deleted_at": None,
            },
        )
        await kit.seed(
            "principal",
            {
                "tenant_id": tenant_id,
                "project_id": p,
                "id": f"prn_{p}",
                "subject": f"s_{p}",
                "roles": ["developer"],
                "created_at": T0,
                "disabled_at": None,
            },
        )


def session(sid: str, *, owner: str = "prn_dev", project_id: str = "p1", tenant_id: str = "t_acme") -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "id": sid,
        "owner_principal_id": owner,
        "data_class": "internal",
        "retention": "default",
        "expires_at": None,
        "metadata": {},
    }


def turn(tid: str, *, project_id: str = "p1", status: str = "completed") -> dict[str, Any]:
    return {
        "id": tid,
        "project_id": project_id,
        "principal_id": "prn_dev",
        "status": status,
        "content_policy": "full",
        "input_parts": [],
        "output_text": "answer",
        "redactions": {"user": [], "assistant": []},
        "generation_profile": "sol-fast",
        "routing": {},
        "usage": USAGE_JSON,
        "timings": {},
        "degradations": [],
        "decision_id": None,
        "verification_decision_id": None,
        "retry_of": None,
        "created_at": T0,
        "completed_at": T0,
        "expires_at": None,
    }


def attempt(aid: str, *, started_at: datetime = T0) -> dict[str, Any]:
    return {
        "id": aid,
        "route_id": "sol-fast",
        "model": "us.openai.gpt-6.1-sol",
        "spec_signature": "sha256:" + "0" * 64,
        "outcome": "committed",
        "cancel_reason": None,
        "finish_reason": "stop",
        "error_code": None,
        "usage": USAGE_JSON,
        "cost_usd": Decimal("0.000010"),
        "cost_estimated": True,
        "started_at": started_at,
        "ended_at": started_at,
    }


def decision(
    did: str,
    *,
    project_id: str = "p1",
    session_id: str | None = None,
    turn_id: str | None = None,
    principal_id: str = "prn_dev",
) -> dict[str, Any]:
    return {
        "id": did,
        "project_id": project_id,
        "created_at": T0,
        "principal_id": principal_id,
        "turn_id": turn_id,
        "session_id": session_id,
        "purpose": "decision",
        "origin": "native",
        "output_format": "native",
        "profile_id": "lightweight-model-decide",
        "profile_version": "1",
        "question_signature": "sha256:" + "1" * 64,
        "input_view_signature": "sha256:" + "2" * 64,
        "input_digest": "hmac-sha256:" + "3" * 64,
        "status": "completed",
        "cache_hit": False,
        "safety_identifier": None,
        "questions": [],
        "input": None,
        "result": {"id": did},
        "expires_at": None,
    }


def file(fid: str, *, project_id: str = "p1", session_id: str | None = None) -> dict[str, Any]:
    return {
        "id": fid,
        "project_id": project_id,
        "principal_id": "prn_dev",
        "session_id": session_id,
        "origin": "upload",
        "filename_display": "a.png",
        "declared_type": "image/png",
        "media_type": None,
        "bytes": 10,
        "sha256": "4" * 64,
        "status": "processing",
        "rejection": None,
        "failure": None,
        "quarantine_key": f"t_acme/quarantine/{fid}",
        "pages": None,
        "expires_at": None,
    }


def usage_line(
    lid: str,
    *,
    project_id: str = "p1",
    line: str = "decision",
    model_id: str = "deepseek.v3.2",
    at: datetime = T0,
    session_id: str | None = None,
) -> dict[str, Any]:
    return {
        "tenant_id": "t_acme",
        "project_id": project_id,
        "id": lid,
        "line": line,
        "model_id": model_id,
        "region": "us-east-1",
        "session_id": session_id,
        "turn_id": None,
        "decision_id": None,
        "attempt_id": None,
        "labeling_run_id": None,
        "input_tokens": 10,
        "output_tokens": 2,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "reasoning_tokens": 0,
        "tokens_estimated": False,
        "cost_usd": Decimal("0.000010"),
        "cost_estimated": True,
        "at": at,
    }
