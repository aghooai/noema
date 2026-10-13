"""The writable columns of every table the repositories insert into (spec-03 §2; generated columns excluded)."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from noema.db.repos._base import Table


def _cols(text: str) -> frozenset[str]:
    return frozenset(text.split())


SESSION: Final = Table(
    "noema.session",
    _cols(
        "tenant_id project_id id owner_principal_id data_class retention expires_at metadata last_seq sticky_spec"
        " active_turn_id lease_expires_at created_at deleted_at"
    ),
    project_scoped=True,
)
TURN: Final = Table(
    "noema.turn",
    _cols(
        "tenant_id project_id id session_id seq principal_id status content_policy input_parts output_text redactions"
        " generation_profile routing usage timings degradations decision_id verification_decision_id retry_of"
        " created_at completed_at expires_at"
    ),
    project_scoped=True,
)
TURN_FILE: Final = Table("noema.turn_file", _cols("tenant_id turn_id part_index file_id"))
ATTEMPT: Final = Table(
    "noema.attempt",
    _cols(
        "tenant_id id turn_id route_id model spec_signature outcome cancel_reason finish_reason error_code usage"
        " cost_usd cost_estimated started_at ended_at"
    ),
)
DECISION: Final = Table(
    "noema.decision",
    _cols(
        "tenant_id project_id id created_at principal_id turn_id session_id purpose origin output_format profile_id"
        " profile_version question_signature input_view_signature input_digest status cache_hit safety_identifier"
        " questions input result expires_at"
    ),
    project_scoped=True,
)
FILE: Final = Table(
    "noema.file",
    _cols(
        "tenant_id project_id id principal_id session_id origin filename_display declared_type media_type bytes sha256"
        " status rejection failure quarantine_key pages created_at deleted_at expires_at"
    ),
    project_scoped=True,
)
DERIVATIVE: Final = Table(
    "noema.derivative",
    _cols(
        "tenant_id id file_id kind status extractor_version sha256 bytes pages width height text_layer_pages"
        " facts storage_key error_code created_at completed_at"
    ),
)
IDEMPOTENCY_RECORD: Final = Table(
    "noema.idempotency_record",
    _cols(
        "tenant_id project_id id scope_hash key fingerprint state owner lease_expires_at resource_id response_status"
        " response_headers response_body sse_turn_id response_body_key created_at expires_at"
    ),
    project_scoped=True,
)
AUDIT_EVENT: Final = Table(
    "noema.audit_event",
    _cols("tenant_id id actor_type actor_id action resource_kind resource_id details request_id at"),
)
JOB: Final = Table(
    "jobs.job",
    _cols("id tenant_id kind priority payload state attempts max_attempts run_after dedupe_key created_at updated_at"),
)
LABELING_RUN: Final = Table(
    "noema.labeling_run",
    _cols(
        "tenant_id id profile_id profile_version question_name question_signature status labelers rule counts"
        " consensus_rate consensus_rate_by_class n_by_class usage started_at completed_at"
    ),
)
LABEL_ITEM: Final = Table(
    "noema.label_item",
    _cols(
        "tenant_id id run_id decision_id question_name sample_reason labeler_answers kept value drop_reason ensemble"
        " content_excerpt redacted input_view_signature member_subset created_at"
    ),
)
CALIBRATOR: Final = Table(
    "noema.calibrator",
    _cols(
        "id scope tenant_id profile_id profile_version question_name question_signature member_subset"
        " input_view_signature choice_method method member_t pooled epsilon holdout acceptance reliability status"
        " fitted_at accepted_at approved_by activated_by activated_at downgraded_at downgrade_reason retired_at"
        " retired_reason"
    ),
    sql_defaults=MappingProxyType({"fitted_at": "now()"}),
)
