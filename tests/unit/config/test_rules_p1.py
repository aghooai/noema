"""The vendored design rules and one negative test per Phase 1 rule (spec-05 §3-§4)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from noema.config.loader import ConfigError, load_config, validate_config
from noema.contracts import vendor

pytestmark = pytest.mark.unit


def _errors(cfg: dict[str, Any], env: str = "test") -> list[str]:
    return validate_config(cfg, env=env)[1]  # type: ignore[arg-type]


@pytest.mark.parametrize("name", sorted(vendor.config_negative_cases()))
def test_every_vendored_negative_config_is_rejected(example: dict[str, Any], name: str) -> None:
    entry = vendor.config_negative_cases()[name]
    mutate, expect = entry if isinstance(entry, tuple) else (entry, None)
    mutate(example)
    errors = _errors(example)
    assert errors, f"{name}: accepted"
    if expect is not None:
        assert any(expect in e for e in errors), f"{name}: {errors}"
    with pytest.raises(ConfigError):
        load_config(example, env="test")


def test_negative_case_count() -> None:
    assert len(vendor.config_negative_cases()) == 49


def test_the_design_positive_early_commit_mutation_is_rejected_by_p1_d1(example: dict[str, Any]) -> None:
    for mutate in vendor.constant("check_config", "POSITIVE").values():
        cfg = copy.deepcopy(example)
        mutate(cfg)
        assert vendor.config_semantic_errors(cfg) == []  # valid for the design checker
        assert any("(P1-D1)" in e for e in _errors(cfg))


def _rejects(cfg: dict[str, Any], rule: str, env: str = "test") -> None:
    errors = _errors(cfg, env)
    assert any(f"({rule})" in e for e in errors), errors


def test_p1_t1_fills_the_default_decision_profile(example: dict[str, Any]) -> None:
    del example["tenants"]["t_acme"]["defaults"]["decision_profile"]
    assert load_config(example, env="test").tenant("t_acme").defaults.decision_profile == "lightweight-model-decide"
    del example["tenants"]["t_acme"]["defaults"]
    assert load_config(example, env="test").tenant("t_acme").defaults.decision_profile == "lightweight-model-decide"


def test_p1_t1_rejects_an_unknown_default(example: dict[str, Any]) -> None:
    del example["tenants"]["t_acme"]["defaults"]["decision_profile"]
    del example["decision_profiles"]["lightweight-model-decide"]
    example["tenants"]["t_acme"]["request_overrides"]["decision_profiles"] = ["router-default"]
    _rejects(example, "P1-T1")


def test_p1_r1_default_profile_required(example: dict[str, Any]) -> None:
    del example["routing"]["default_profile"]
    _rejects(example, "P1-R1")


def test_p1_d2_batch_questions(example: dict[str, Any]) -> None:
    example["decision_profiles"]["router-plus"]["batch_questions"] = True
    _rejects(example, "P1-D2")


def test_p1_d3_elicitation(example: dict[str, Any]) -> None:
    example["templates"]["sampled-dist@1"] = {"elicitation": "sampling", "max_choices": 26}
    member = {
        "model": "deepseek-3.2",
        "elicitation": "sampling",
        "template": "sampled-dist@1",
        "weight": 1,
        "samples": 5,
    }
    example["decision_profiles"]["router-plus"]["members"][0] = member
    _rejects(example, "P1-D3")


def test_p1_d4_aggregation(example: dict[str, Any]) -> None:
    example["decision_profiles"]["router-plus"]["aggregation"] = "log_pool"
    _rejects(example, "P1-D4")


def test_p1_d5_template(example: dict[str, Any]) -> None:
    example["templates"]["verbalized-dist@2"] = {"elicitation": "verbalized", "max_choices": 26}
    example["decision_profiles"]["lightweight-model-decide"]["members"][0]["template"] = "verbalized-dist@2"
    _rejects(example, "P1-D5")


def test_p1_d5_template_elicitation(example: dict[str, Any]) -> None:
    """A config that redefines an implemented template's elicitation (consistently with its members) is rejected."""
    example["templates"]["verify-dist@1"]["elicitation"] = "token_logprobs"
    example["templates"]["verify-dist@1"]["max_choices"] = 16
    for m in example["decision_profiles"]["answer-verifier"]["members"]:
        m["elicitation"] = "token_logprobs"
        m["model"] = "deepseek-3.2"
        m.pop("effort", None)
    example["decision_profiles"]["answer-verifier"]["members"].pop()
    example["decision_profiles"]["answer-verifier"]["min_quorum"] = 1
    _rejects(example, "P1-D5")


def test_p1_d6_router_question_above_the_flat_limit(example: dict[str, Any]) -> None:
    choices = [{"value": f"v{i}"} for i in range(17)]
    question = {"type": "choice", "name": "topic", "instructions": "Topic?", "choices": choices}
    example["decision_profiles"]["router-default"]["questions"].append(question)
    _rejects(example, "P1-D6")


def test_p1_d6_verification_question_above_the_flat_limit(example: dict[str, Any]) -> None:
    choices = [{"value": f"v{i}"} for i in range(27)]
    question = {"type": "choice", "name": "category", "instructions": "Category?", "choices": choices}
    example["decision_profiles"]["answer-verifier"]["questions"].append(question)
    assert not any("P1-D6" in e for e in _errors(example))  # not asked: routing.verification.questions selects others
    example["routing"]["verification"]["questions"].append("category")
    _rejects(example, "P1-D6")


@pytest.mark.parametrize(
    "where",
    ["intent_route", "binding", "default_profile", "uncertain_profile", "tenant_default"],
)
def test_p1_g1_no_route_to_best_of_n(example: dict[str, Any], where: str) -> None:
    target = "best-of-sol-opus"
    if where == "intent_route":
        example["routing"]["intent_routes"]["chitchat"] = target
    elif where == "binding":
        example["routing"]["bindings"][1]["then"]["generation_profile"] = target
    elif where in ("default_profile", "uncertain_profile"):
        example["routing"][where] = target
    else:
        example["tenants"]["t_acme"]["defaults"]["generation_profile"] = target
    _rejects(example, "P1-G1")


def test_p1_g1_allows_best_of_n_in_the_request_allowlist(example: dict[str, Any]) -> None:
    assert "best-of-sol-opus" in example["tenants"]["t_acme"]["request_overrides"]["generation_profiles"]
    assert _errors(example) == []  # requests for it get 422 feature_not_available (IMPL-036)


def test_p1_l1_model_quotas(example: dict[str, Any]) -> None:
    example["tenants"]["t_acme"]["limits"] = {"model_quotas": {"sol-6.1": {"rpm": 10, "tpm": 1000}}}
    assert _errors(example) == []
    example["tenants"]["t_acme"]["limits"]["model_quotas"]["kimi-k3"] = {"rpm": 1, "tpm": 1}
    _rejects(example, "P1-L1")


def test_p1_l1_model_quota_outside_allowed_models(example: dict[str, Any]) -> None:
    example["tenants"]["t_acme"]["allowed_models"] = list(example["models"])
    example["tenants"]["t_acme"]["limits"] = {"model_quotas": {"glm-5.3": {"rpm": 1, "tpm": 1}}}
    assert _errors(example) == []
    example["tenants"]["t_acme"]["allowed_models"].remove("glm-5.3")
    _rejects(example, "P1-L1")


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_p1_p1_file_prices_only_in_dev_and_test(example: dict[str, Any], env: str) -> None:
    example["platform"] = {"cost": {"prices_ref": "file:prices.yaml"}}
    _rejects(example, "P1-P1", env=env)
    assert not any("P1-P1" in e for e in _errors(example, "dev"))
