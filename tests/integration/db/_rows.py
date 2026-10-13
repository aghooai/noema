"""One valid row per RLS table (spec-03 §2) for the RLS and FORCE tests, plus the parent rows they reference.

`seed_parents(admin, tenant)` creates, as superuser, the tenant with projects prj_a/prj_b, principal prn_<p>,
session ses_<p>, turn turn_<p>, file file_<p> per project and the labeling run lrun_x. `row_sql(table, tenant,
project, suffix)` is an INSERT of one row whose own key is derived from `suffix`.
"""

from __future__ import annotations

import asyncpg

type Conn = asyncpg.Connection[asyncpg.Record]
type Statement = tuple[str, tuple[object, ...]]

PROJECTS = ("prj_a", "prj_b")

# table -> (column that holds the tenant, INSERT template). Placeholders: {t} tenant, {p} project, {s} suffix ($n)
_ROWS: dict[str, tuple[str, str]] = {
    "noema.tenant": ("id", "INSERT INTO noema.tenant (id, aws_account_id) VALUES ({t}, '111122223333')"),
    "noema.project": ("tenant_id", "INSERT INTO noema.project (tenant_id, id, name) VALUES ({t}, {s}, {s})"),
    "noema.principal": (
        "tenant_id",
        "INSERT INTO noema.principal (tenant_id, project_id, id, subject, roles)"
        " VALUES ({t}, {p}, {s}, {s}, '{{developer}}')",
    ),
    "auth.api_key": (
        "tenant_id",
        "INSERT INTO auth.api_key (key_id, tenant_id, project_id, principal_id, key_hash, pepper_version, env, scopes)"
        " VALUES ({s}, {t}, {p}, 'prn_' || {p}, 'hmac-sha256:v1:00', 1, 'live', '{{turns}}')",
    ),
    "noema.session": (
        "tenant_id",
        "INSERT INTO noema.session (tenant_id, project_id, id, owner_principal_id, data_class)"
        " VALUES ({t}, {p}, {s}, 'prn_' || {p}, 'internal')",
    ),
    "noema.turn": (
        "tenant_id",
        "INSERT INTO noema.turn (tenant_id, project_id, id, principal_id, status, content_policy, routing, usage,"
        " timings, created_at, completed_at)"
        " VALUES ({t}, {p}, {s}, 'prn_' || {p}, 'completed', 'full', '{{}}', '{{}}', '{{}}', now(), now())",
    ),
    "noema.turn_file": (
        "tenant_id",
        "INSERT INTO noema.turn_file (tenant_id, turn_id, part_index, file_id)"
        " VALUES ({t}, 'turn_' || {p}, abs(hashtext({s})), 'file_' || {p})",
    ),
    "noema.attempt": (
        "tenant_id",
        "INSERT INTO noema.attempt (tenant_id, id, turn_id, route_id, model, spec_signature, outcome, usage, cost_usd,"
        " cost_estimated, started_at, ended_at)"
        " VALUES ({t}, {s}, 'turn_' || {p}, 'r1', 'm', 'sig', 'committed', '{{}}', 0, false, now(), now())",
    ),
    "noema.decision": (
        "tenant_id",
        "INSERT INTO noema.decision (tenant_id, project_id, id, created_at, principal_id, purpose, origin,"
        " output_format, profile_id, profile_version, question_signature, input_view_signature, input_digest,"
        " status, cache_hit, questions, result)"
        " VALUES ({t}, {p}, {s}, now(), 'prn_' || {p}, 'decision', 'native', 'native', 'lmd', '1', 'qs', 'vs',"
        " 'dg', 'completed', false, '[]', '{{}}')",
    ),
    "noema.file": (
        "tenant_id",
        "INSERT INTO noema.file (tenant_id, project_id, id, principal_id, origin, declared_type, bytes, sha256, status,"
        " quarantine_key) VALUES ({t}, {p}, {s}, 'prn_' || {p}, 'upload', 'image/png', 1, '00', 'ready', 'q')",
    ),
    "noema.derivative": (
        "tenant_id",
        "INSERT INTO noema.derivative (tenant_id, id, file_id, kind, status, extractor_version)"
        " VALUES ({t}, {s}, 'file_' || {p}, (ARRAY['raster', 'raster_1568', 'thumbnail', 'ocr_text', 'pdf_sanitized',"
        " 'markdown', 'page_images', 'text'])[1 + abs(hashtext({s})) % 8], 'ready', 'x1')",
    ),
    "noema.usage_line": (
        "tenant_id",
        "INSERT INTO noema.usage_line (tenant_id, project_id, id, line, input_tokens, output_tokens,"
        " cached_input_tokens, cache_write_input_tokens, reasoning_tokens, tokens_estimated, cost_usd, cost_estimated,"
        " at) VALUES ({t}, {p}, {s}, 'cache_savings', 0, 0, 0, 0, 0, false, 0, false, now())",
    ),
    "noema.idempotency_record": (
        "tenant_id",
        "INSERT INTO noema.idempotency_record (tenant_id, project_id, id, scope_hash, key, fingerprint, state, owner,"
        " lease_expires_at, expires_at)"
        " VALUES ({t}, {p}, {s}, 'sh', {s}, 'fp', 'in_flight', 'o', now(), now() + interval '1 day')",
    ),
    "noema.audit_event": (
        "tenant_id",
        "INSERT INTO noema.audit_event (tenant_id, id, actor_type, actor_id, action, resource_kind)"
        " VALUES ({t}, {s}, 'system', 'test', 'test.action', 'test')",
    ),
    "jobs.job": (
        "tenant_id",
        "INSERT INTO jobs.job (id, tenant_id, kind, payload, state) VALUES ({s}, {t}, 'test.kind', '{{}}', 'queued')",
    ),
    "noema.labeling_run": (
        "tenant_id",
        "INSERT INTO noema.labeling_run (tenant_id, id, profile_id, profile_version, question_name,"
        " question_signature, status, labelers, rule, counts, usage, started_at)"
        " VALUES ({t}, {s}, 'p', '1', 'q', 'qs', 'completed', '{{a,b}}', 'unanimous', '{{}}', '{{}}', now())",
    ),
    "noema.label_item": (
        "tenant_id",
        "INSERT INTO noema.label_item (tenant_id, id, run_id, decision_id, question_name, sample_reason,"
        " labeler_answers, kept, value, ensemble, redacted, input_view_signature, member_subset)"
        " VALUES ({t}, {s}, 'lrun_x', {s}, 'q', 'uniform', '[]', true, '1', '{{}}', false, 'vs', '{{m1}}')",
    ),
    "noema.calibrator": (
        "tenant_id",
        "INSERT INTO noema.calibrator (id, scope, tenant_id, profile_id, profile_version, question_name,"
        " question_signature, member_subset, input_view_signature, method, member_t, pooled, holdout, acceptance,"
        " reliability, status, fitted_at) VALUES ({s}, 'tenant', {t}, 'p', '1', 'q', 'qs', '{{m1}}', 'vs',"
        " 'temperature', '{{}}', '{{}}', '{{}}', '{{}}', '{{}}', 'fitted', now())",
    ),
    "noema.calibrator_epoch": ("scope_key", "INSERT INTO noema.calibrator_epoch (scope_key, n) VALUES ({t}, 0)"),
}
RLS_TABLES = tuple(_ROWS)
WRITE_PRIVILEGES = {  # what noema_app may do (spec-03 §1); others get 42501 before RLS
    "noema.calibrator": {"INSERT", "UPDATE"},
    "noema.audit_event": {"INSERT"},
    "jobs.job": {"INSERT"},
    **{
        f"noema.{t}": {"INSERT", "UPDATE", "DELETE"}
        for t in ("session", "turn", "turn_file", "attempt", "decision", "file", "derivative", "idempotency_record",
                  "labeling_run", "label_item")
    },
}  # fmt: skip
APP_CANNOT_SELECT = {"auth.api_key"}


def tenant_column(table: str) -> str:
    return _ROWS[table][0]


def row_sql(table: str, tenant: str, project: str, suffix: str) -> Statement:
    """A numbered-parameter INSERT of one row of `table` and its arguments."""
    template = _ROWS[table][1]
    args: list[object] = []
    slots: dict[str, str] = {}
    for key, value in (("t", tenant), ("p", project), ("s", suffix)):
        if "{" + key + "}" in template:
            args.append(value)
            slots[key] = f"${len(args)}::text"
    return template.format(**{k: slots.get(k, "") for k in ("t", "p", "s")}), tuple(args)


async def seed_parents(admin: Conn, tenant: str) -> None:
    """The parent rows of every child table for `tenant` (superuser: past RLS)."""
    for table, suffix_of in (
        ("noema.tenant", None), ("noema.project", "{p}"), ("noema.principal", "prn_{p}"), ("noema.session", "ses_{p}"),
        ("noema.turn", "turn_{p}"), ("noema.file", "file_{p}"),
    ):  # fmt: skip
        for project in PROJECTS if suffix_of else (PROJECTS[0],):
            sql, args = row_sql(table, tenant, project, (suffix_of or "").format(p=project))
            await admin.execute(sql, *args)
    sql, args = row_sql("noema.labeling_run", tenant, PROJECTS[0], "lrun_x")
    await admin.execute(sql, *args)
