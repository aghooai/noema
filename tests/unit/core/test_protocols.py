"""The frozen core surface (spec-02 and the impl-spec README table): names, dataclass shape, Protocol rules."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import inspect
from datetime import UTC, datetime
from typing import Protocol

import orjson
import pytest

from noema.core.errors import ErrorKind, TenantAccessMisconfigured
from noema.core.protocols.adapters import AdapterError
from noema.core.protocols.credentials import AwsCredentials, MantleAuth, TenantCredentials, assert_creds_for
from noema.core.protocols.generation import AttemptBudget, AttemptSpec
from noema.core.protocols.media import CONVERSE_CAPS_V1, VIEW_BUDGET_V1
from noema.core.secret import SecretStr

pytestmark = pytest.mark.unit

FROZEN_SURFACE: dict[str, tuple[str, ...]] = {
    "noema.core.ids": ("IdPrefix", "IdGen"),
    "noema.core.clock": ("Clock", "Deadline"),
    "noema.core.errors": (
        "ErrorType", "ErrorCode", "ErrorSpec", "ERROR_TABLE", "NoemaError", "ErrorKind", "TenantAccessCause",
        "ClassifiedError", "TenantAccessMisconfigured",
    ),
    "noema.core.values": (
        "JsonValue", "Effort", "DataClass", "ContentPolicy", "CallKind", "UsageLineName", "Modality", "UsageRecord",
        "Cost", "Degradation", "TextBlock", "ImageBlock", "DocumentBlock", "ContentBlock", "Message",
    ),
    "noema.core.principal": ("Role", "Scope", "Principal", "system_principal", "TenantRef"),
    "noema.core.text": ("clean_text", "RESERVED_TAGS", "escape_reserved_tags", "datamark"),
    "noema.core.secret": ("SecretStr",),
    "noema.core.redis_keys": ("RedisKeys",),
    "noema.core.protocols.config": ("ConfigSnapshot", "ConfigRegistry"),
    "noema.core.protocols.credentials": (
        "AwsCredentials", "MantleAuth", "TenantCredentials", "CredentialBroker", "assert_creds_for", "SecretsProvider",
    ),
    "noema.core.protocols.adapters": (
        "ModelRequest", "TokenLogprob", "StopReason", "ModelResponse", "MessageStart", "ReasoningDelta", "TextDelta",
        "UsageEvent", "Stop", "ErrorEvent", "StreamEvent", "StreamTimeouts", "ModelCapabilities", "Adapter",
        "AdapterError", "AdapterRegistry",
    ),
    "noema.core.protocols.decision": (
        "Purpose", "Origin", "OutputFormat", "ProfileRef", "DecisionContext", "DecisionRequest", "AnswerSummary",
        "DecisionSnapshot", "DecisionRun", "DecisionEngine", "QuestionCompiler", "DecisionCache", "OptionSpec",
        "CompiledQuestion", "GroupSpec", "CompiledQuestionSet", "Msg", "StandardInput", "VerificationInput",
        "DecisionInput",
    ),
    "noema.core.protocols.media": (
        "ViewKind", "ContentPart", "DecisionViewBudget", "VIEW_BUDGET_V1", "ViewPart", "InputView", "InputViewResolver",
        "DerivativeKind", "DerivativeRef", "FileStatus", "FileRef", "FileService", "MediaCaps", "CONVERSE_CAPS_V1",
        "PlannedPart", "MediaPlan", "MediaNegotiator", "MediaBudgetPlanner",
    ),
    "noema.core.protocols.sandbox": (
        "SandboxKind", "SandboxLimits", "SandboxJob", "SandboxOutput", "ExtractResult", "SandboxRunner",
    ),
    "noema.core.protocols.storage": (
        "Row", "UnitOfWork", "Database", "Page", "SessionLease", "Busy", "LeaseLost", "SessionRepo", "TurnRepo",
        "AttemptRepo", "DecisionRepo", "FileRepo", "DerivativeRepo", "UsageRepo", "LabelRepo", "CalibratorRepo",
        "IdempotencyRepo", "AuditRepo", "ProjectRepo", "JobRepo", "SystemCalls", "JobQueue", "Job", "PermanentJobError",
        "Worker", "StoredResponse", "IdemToken", "New", "InFlight", "Replay", "Conflict", "IdempotencyStore",
        "OutboxKind", "OutboxRecord", "Outbox",
    ),
    "noema.core.protocols.events": ("SseFrame", "TurnEventLog", "SseWriter"),
    "noema.core.protocols.ops": (
        "HealthRegistry", "RetryBudget", "DecisionWork", "GenerationWork", "WorkGraph", "WorkGraphPlanner",
        "Reservation", "Admission", "TokenEstimator", "UsageLine", "CostMeter",
    ),
    "noema.core.protocols.labeling": (
        "CalibratorBindingKey", "Transform", "Calibrator", "CalibratorRegistry", "ContentRef", "LabelingSampler",
    ),
    "noema.core.protocols.generation": (
        "AttemptSpec", "Conditioning", "AssembledPrompt", "PromptBuilder", "AttemptBudget", "TurnContext",
        "AttemptRecord", "GenEvent", "ProfileRun", "GenerationEngine",
    ),
    "noema.core.protocols.auth": ("Authenticator", "Action", "ResourceRef", "ListScope", "Authorizer"),
    "noema.core.protocols.telemetry": ("SpanName", "Span", "Instruments", "Telemetry"),
}  # fmt: skip


@pytest.mark.parametrize("module_name", sorted(FROZEN_SURFACE))
def test_frozen_names_exist(module_name: str) -> None:
    module = importlib.import_module(module_name)
    missing = [n for n in FROZEN_SURFACE[module_name] if not hasattr(module, n)]
    assert not missing


@pytest.mark.parametrize("module_name", sorted(FROZEN_SURFACE))
def test_dataclasses_are_frozen_and_slotted_and_protocols_not_runtime_checkable(module_name: str) -> None:
    module = importlib.import_module(module_name)
    for name, obj in vars(module).items():
        if not inspect.isclass(obj) or obj.__module__ != module_name:
            continue
        if dataclasses.is_dataclass(obj):
            params = obj.__dataclass_params__  # type: ignore[attr-defined]
            assert params.frozen, name
            assert "__slots__" in vars(obj), name
        if issubclass(obj, Protocol) and obj is not Protocol and getattr(obj, "_is_protocol", False):
            assert not getattr(obj, "_is_runtime_protocol", False), name


def test_attempt_spec_signature_and_equality() -> None:
    spec = AttemptSpec("sol-fast", "low", 600, "none")
    expected = "sha256:" + hashlib.sha256(orjson.dumps(["sol-fast", "low", 600, "none"])).hexdigest()
    assert spec.signature() == expected
    assert AttemptSpec("sol-fast", "low", 600, "none") == spec
    assert AttemptSpec("sol-fast", None, None, "none").signature() != spec.signature()


def test_attempt_budget() -> None:
    budget = AttemptBudget()
    assert budget.remaining == 2
    assert budget.try_take()
    assert budget.try_take()
    assert not budget.try_take()
    assert budget.remaining == 0
    assert not AttemptBudget(0).try_take()
    with pytest.raises(ValueError, match=">= 0"):
        AttemptBudget(-1)


def _creds(tenant: str, account: str) -> TenantCredentials:
    aws = AwsCredentials("ASIAEXAMPLE", SecretStr("secret-access-key"), SecretStr("token-value-1234"), None)
    return TenantCredentials(
        tenant_id=tenant,
        account_id=account,
        region="us-east-1",
        aws=aws,
        mantle=MantleAuth("sigv4", None, None),
        expires_at=datetime(2026, 10, 10, tzinfo=UTC),
        source="sts",
    )


def test_assert_creds_for() -> None:
    assert_creds_for(_creds("t_a", "111111111111"), "t_a", "111111111111")
    for creds in (_creds("t_b", "111111111111"), _creds("t_a", "222222222222")):
        with pytest.raises(TenantAccessMisconfigured) as info:
            assert_creds_for(creds, "t_a", "111111111111")
        assert info.value.cause == "account_mismatch"
    assert "secret-access-key" not in repr(_creds("t_a", "1"))


def test_adapter_error_carries_the_classified_error() -> None:
    from noema.core.errors import ClassifiedError

    err = AdapterError(ClassifiedError(ErrorKind.RETRYABLE, "timeout", provider_code="timeout:deadline"))
    assert err.error.provider_code == "timeout:deadline"
    assert str(err) == "timeout"


def test_code_constants() -> None:
    assert (VIEW_BUDGET_V1.max_text_tokens, VIEW_BUDGET_V1.max_images, VIEW_BUDGET_V1.max_documents) == (6000, 20, 5)
    assert VIEW_BUDGET_V1.image_long_edge == 1568
    assert CONVERSE_CAPS_V1.max_images == 20
    assert CONVERSE_CAPS_V1.max_image_bytes == 3_750_000
    assert CONVERSE_CAPS_V1.max_payload_bytes == 20_000_000
