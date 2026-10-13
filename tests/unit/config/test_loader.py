"""The config loader and the frozen snapshot (spec-05 §3-§4, spec-02 §2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from noema.config.loader import (
    ConfigError,
    digest_of,
    fill_defaults,
    load_config,
    load_config_file,
    parse_source,
    referenced_price_version,
    validate_config,
)
from noema.config.models import (
    DECISION_PROMPT_OVERHEAD,
    DEFAULT_CONTEXT_WINDOW,
    EFFORT_RESERVE,
    GEN_DEFAULT_MAX_TOKENS,
    ROUTE_TIMEOUTS_V1_DEFAULT,
)
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import TenantRef
from noema.core.protocols.adapters import StreamTimeouts
from noema.core.values import Effort

from .conftest import EXAMPLE_PATH

pytestmark = pytest.mark.unit


def test_the_example_config_loads_with_defaults_filled() -> None:
    snap = load_config_file(EXAMPLE_PATH, env="test")
    assert snap.version == 1
    assert snap.digest.startswith("sha256:") and len(snap.digest) == 71
    limits = snap.platform.limits
    assert (limits.max_json_body_bytes, limits.rpm_per_key, limits.admission_queue_timeout_ms) == (
        52_428_800,
        6000,
        2000,
    )
    assert snap.platform.idempotency.ttl_s == 86400
    assert snap.platform.events.resume_window_s == 300
    assert snap.platform.sessions.queue_timeout_ms == 2000
    assert snap.platform.decision_cache.ttl_s == 600
    assert (snap.platform.cost.cancel_overrun_tokens, snap.platform.cost.prices_ref) == (64, None)
    assert snap.platform.media.inline_wait_ms == 3000
    assert snap.platform.credentials.mantle_auth == "bearer_short_term"
    classes = snap.platform.data_classes
    assert (classes.public.store_content, classes.public.retain_days, classes.public.l2_cache) == ("full", 90, True)
    assert classes.for_class("confidential").store_content == "redacted"
    assert (classes.restricted.store_content, classes.restricted.retain_days) == ("metadata_only", 7)
    acc = snap.labeling.acceptance
    assert (acc.ece_max, acc.n_min, acc.class_n_min, acc.consensus_min, acc.drift_ece_max, acc.subset_min_share) == (
        0.05, 300, 30, 0.6, 0.08, 0.05,
    )  # fmt: skip
    assert snap.routing.speculation.commit_on_quorum is False
    assert snap.routing.bindings[0].when.source == "platform"
    assert snap.decision_profile("router-plus").modality_policy == "derive_for_text_members"
    assert snap.decision_profile("router-plus").batch_questions is False
    assert snap.prices.entries == ()


def test_snapshot_lookups() -> None:
    snap = load_config_file(EXAMPLE_PATH, env="test")
    assert snap.tenant("t_acme").id == "t_acme"
    assert snap.tenant("t_acme").defaults.decision_profile == "lightweight-model-decide"
    assert snap.tenant_ref("t_acme") == TenantRef("t_acme", "111122223333", "us-east-1")
    model = snap.model("deepseek-3.2")
    assert (model.alias, model.id, model.adapter) == ("deepseek-3.2", "deepseek.v3.2", "bedrock-mantle-chat")
    assert model.controls.top_logprobs_max == 20 and model.controls.effort == ()
    assert model.context_window == DEFAULT_CONTEXT_WINDOW and model.reasoning_reserve_tokens == 0
    assert snap.template("logprob-codes@2").max_choices == 16
    router = snap.decision_profile("router-default")
    assert router.id == "router-default" and router.commit_critical == ("intent", "complexity")
    assert router.members[0].backup_after_ms == 400 and router.members[0].weight == 1.0
    assert router.escalation is not None and router.escalation.questions == ("intent",)
    assert router.questions[0].choices is not None and router.questions[0].choices[0].value == "chitchat"
    assert snap.generation_profile("glm-reason").members[0].max_output_tokens == 4096
    assert snap.routing.intent_routes["coding"] == "opus-deep"
    assert snap.routing.bindings[1].when.confidence_band == "low"
    for lookup in (
        snap.tenant,
        snap.tenant_ref,
        snap.model,
        snap.template,
        snap.decision_profile,
        snap.generation_profile,
    ):
        with pytest.raises(NoemaError) as info:
            lookup("missing")
        assert info.value.code is ErrorCode.NOT_FOUND
    assert GEN_DEFAULT_MAX_TOKENS == 4096


def test_shared_template_constants_are_the_spec_values() -> None:
    # spec-06 §5.5 (decision templates, labeler@1, admission) and spec-11 §1.1 (admission's per-call overhead)
    assert dict(EFFORT_RESERVE) == {"none": 0, "low": 512, "medium": 1024, "high": 2048, "xhigh": 4096, "max": 4096}
    assert DECISION_PROMPT_OVERHEAD == 600


def test_route_timeouts_v1() -> None:
    snap = load_config_file(EXAMPLE_PATH, env="test")
    fast = StreamTimeouts(3, 5, 8, 30)
    efforts: tuple[Effort | None, ...] = (None, "none", "low")
    for effort in efforts:
        assert snap.route_timeouts("sol-6.1", effort) == fast
    slow: tuple[Effort, ...] = ("medium", "high", "max")
    for effort in slow:
        assert snap.route_timeouts("sol-6.1", effort) == ROUTE_TIMEOUTS_V1_DEFAULT == StreamTimeouts(3, 30, 30, 30)
    glm_efforts: tuple[Effort | None, ...] = (None, "none", "medium", "max")
    for effort in glm_efforts:
        assert snap.route_timeouts("glm-5.3", effort) == StreamTimeouts(3, 10, 30, 30)
    assert snap.route_timeouts("opus-5.5", "high") == ROUTE_TIMEOUTS_V1_DEFAULT


def test_digest_is_stable_and_canonical(example: dict[str, Any]) -> None:
    a = load_config(example, env="test")
    reordered = dict(reversed(list(example.items())))
    b = load_config(yaml.safe_dump(reordered), env="test")
    assert a.digest == b.digest == digest_of(fill_defaults(example))
    assert a.body == fill_defaults(example)
    example["routing"]["speculation"]["holdback_ms"] = 1500
    assert load_config(example, env="test").digest != a.digest


def test_filling_an_explicit_default_does_not_change_the_digest(example: dict[str, Any]) -> None:
    explicit = parse_source(example)
    explicit["platform"] = {"limits": {"rpm_per_key": 6000}, "data_classes": {"public": {"retain_days": 90}}}
    assert load_config(explicit, env="test").digest == load_config(example, env="test").digest


def test_data_class_overrides_merge_per_field(example: dict[str, Any]) -> None:
    example["platform"] = {"data_classes": {"confidential": {"retain_days": 60}}}
    conf = load_config(example, env="test").platform.data_classes.confidential
    assert (conf.store_content, conf.retain_days, conf.l2_cache) == ("redacted", 60, False)


def test_source_errors(tmp_path: Path) -> None:
    for bad in ("a: [", "- 1\n- 2\n"):
        with pytest.raises(ConfigError, match="yaml"):
            load_config(bad, env="test")
    with pytest.raises(ConfigError, match="cannot read"):
        load_config_file(tmp_path / "missing.yaml", env="test")


def test_schema_errors_stop_before_the_rules(example: dict[str, Any]) -> None:
    del example["routing"]
    _, errors = validate_config(example, env="test")
    assert errors and all(e.startswith("schema:") for e in errors)
    with pytest.raises(ConfigError) as info:
        load_config(example, env="test")
    assert info.value.errors == tuple(errors)


def test_referenced_price_version(example: dict[str, Any]) -> None:
    assert referenced_price_version(example) is None
    example["platform"] = {"cost": {"prices_ref": "db:7"}}
    assert referenced_price_version(example) == 7
    example["platform"] = {"cost": {"prices_ref": "file:prices.yaml"}}
    assert referenced_price_version(example) is None
