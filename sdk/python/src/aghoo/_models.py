"""Tolerant response models for sessions, files, profiles, usage and the read-only admin views (W4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from ._decision_types import CalibrationBinding, HoldoutMetrics, Response, Usage


class SessionRetention(Response):
    policy: Literal["default", "ephemeral", "custom"] | str
    expires_at: int | None


class Session(Response):
    id: str
    created_at: int
    last_seq: int  # seq of the latest committed turn (0 = none); the value to send as expected_seq
    owner: str | None = None
    data_class: Literal["public", "internal", "confidential", "restricted"] | str
    retention: SessionRetention
    metadata: dict[str, str]


class Redaction(Response):
    start: int  # offsets in Unicode code points (Python str indices); end is exclusive
    end: int
    reason: str


class SessionTurn(Response):
    """One history message; a committed turn yields its user message, then its assistant message."""

    turn_id: str
    seq: int
    role: Literal["user", "assistant"] | str
    text: str
    created_at: int
    redactions: list[Redaction]


class SessionDeleted(Response):
    id: str
    deleted: Literal[True]
    deletion_job_id: str


class Derivative(Response):
    kind: str
    id: str
    status: Literal["pending", "ready", "failed"] | str
    pages: int | None = None


class FileRejection(Response):
    code: str
    message: str


class FileFailure(Response):
    code: str
    message: str
    retryable: bool


class FileObject(Response):
    id: str
    filename: str  # display only; never sent to models
    media_type: str
    bytes: int
    sha256: str
    status: Literal["quarantined", "processing", "ready", "partial", "rejected", "failed", "deleted"] | str
    derivatives: list[Derivative]
    rejection: FileRejection | None = None
    failure: FileFailure | None = None


class FileDeleted(Response):
    id: str
    deleted: Literal[True]


@dataclass(frozen=True, slots=True)
class DerivativeContent:
    """Derivative bytes and their content type (e.g. text/markdown, image/png)."""

    content: bytes
    media_type: str


class ProfileInfo(Response):
    id: str
    version: Annotated[str, Field(pattern=r"^[0-9]+$")]
    kind: Literal["decision", "generation"] | str
    description: str
    models: list[str]
    mode: str | None = None
    modalities: list[str]
    streaming: str | None = None


class UsageRow(Response):
    key: str | None  # project, session, Bedrock model id, UTC day or line; null when the dimension does not apply
    line: Literal["decision", "generation", "labeling", "verification", "waste", "cache_savings"] | str
    usage: Usage


class CancelResult(Response):
    turn_id: str
    status: Literal["cancelling", "cancelled", "completed"] | str


class CalibratorParameters(Response):
    pooled: dict[str, float]
    member_T: dict[str, float]  # noqa: N815 - wire name
    epsilon: float


class CalibratorAcceptance(Response):
    passed: bool
    failed_rules: list[str]


class ReliabilityBin(Response):
    lower: float
    upper: float
    n: int
    mean_predicted: float | None
    observed: float | None


class CalibratorReliability(Response):
    bins: list[ReliabilityBin]
    ci: dict[str, list[float]]


class CalibratorInfo(Response):
    id: str
    object: Literal["calibrator"]
    scope: Literal["platform", "tenant"] | str
    question_name: str
    status: Literal["fitted", "rejected", "accepted", "active", "downgraded", "retired"] | str
    method: str
    parameters: CalibratorParameters
    binding: CalibrationBinding
    member_subset: list[str]
    holdout: HoldoutMetrics | None
    acceptance: CalibratorAcceptance
    reliability: CalibratorReliability
    created_at: int
    approved_by: str | None
    activated_by: list[str]
    activated_at: int | None
    downgraded_at: int | None
    downgrade_reason: str | None
    retired_at: int | None
    retired_reason: str | None


class LabelingCounts(Response):
    sampled: int
    sampled_uniform: int
    sampled_uncertainty: int
    kept: int
    dropped_disagreed: int
    dropped_refused: int
    dropped_parse_error: int
    dropped_labeler_error: int


class LabelingRun(Response):
    id: str
    object: Literal["labeling_run"]
    status: Literal["running", "completed", "failed", "cancelled"] | str
    profile_id: str
    profile_version: str
    question_name: str
    question_signature: str
    labelers: list[str]
    rule: str
    counts: LabelingCounts
    consensus_rate: float | None
    consensus_rate_by_class: dict[str, float]
    n_by_class: dict[str, int]
    usage: Usage
    started_at: int
    completed_at: int | None


class LabelerAnswer(Response):
    model: str
    status: str
    value: str | bool | None


class EnsembleView(Response):
    value: str | bool | None
    probability: float | None
    band: str | None
    calibration_status: str


class DroppedContent(Response):
    excerpt: str | None
    redacted: bool


class LabelingDroppedItem(Response):
    id: str
    object: Literal["labeling_dropped_item"]
    run_id: str
    decision_id: str
    question_name: str
    question_signature: str
    input_view_signature: str
    reason: Literal["disagreed", "refused", "parse_error", "labeler_error"] | str
    sample_reason: str
    labeler_answers: list[LabelerAnswer]
    ensemble: EnsembleView
    content: DroppedContent
    sampled_at: int
