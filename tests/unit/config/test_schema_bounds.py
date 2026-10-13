"""One negative test per rev-6 schema bound (spec-05 §1, §4): platform, tenant limits, acceptance, context_window."""

from __future__ import annotations

from typing import Any

import pytest

from noema.config.loader import validate_config

pytestmark = pytest.mark.unit

PLATFORM_BOUNDS = [
    ("limits", "max_json_body_bytes", 1_048_575),
    ("limits", "max_json_body_bytes", 104_857_601),
    ("limits", "rpm_per_key", 0),
    ("limits", "tpm_per_key", 0),
    ("limits", "rpm_per_project", 0),
    ("limits", "tpm_per_project", 0),
    ("limits", "concurrent_streams_per_project", 0),
    ("limits", "admission_queue_timeout_ms", -1),
    ("limits", "admission_queue_timeout_ms", 30_001),
    ("idempotency", "ttl_s", 3599),
    ("idempotency", "ttl_s", 604_801),
    ("events", "resume_window_s", 29),
    ("events", "resume_window_s", 3601),
    ("sessions", "queue_timeout_ms", -1),
    ("sessions", "queue_timeout_ms", 30_001),
    ("decision_cache", "ttl_s", -1),
    ("decision_cache", "ttl_s", 86_401),
    ("cost", "cancel_overrun_tokens", -1),
    ("cost", "cancel_overrun_tokens", 4097),
    ("cost", "prices_ref", "db:0"),
    ("cost", "prices_ref", "s3://bucket/prices.yaml"),
    ("cost", "prices_ref", "file:"),
    ("media", "inline_wait_ms", 99),
    ("media", "inline_wait_ms", 30_001),
    ("credentials", "mantle_auth", "long_term_key"),
    ("limits", "unknown_key", 1),
]
DATA_CLASS_BOUNDS = [("store_content", "plaintext"), ("retain_days", 0), ("retain_days", 3651), ("l2_cache", "yes")]
ACCEPTANCE_BOUNDS = [
    ("ece_max", 0.051),
    ("ece_max", -0.01),
    ("n_min", 299),
    ("class_n_min", 29),
    ("consensus_min", 0.59),
    ("consensus_min", 1.01),
    ("drift_ece_max", 1.01),
    ("subset_min_share", -0.01),
    ("subset_min_share", 1.01),
]
TENANT_LIMIT_BOUNDS: list[dict[str, Any]] = [
    {"rpm_per_key": 0},
    {"tpm_per_key": 0},
    {"rpm_per_project": 0},
    {"tpm_per_project": 0},
    {"concurrent_streams_per_project": 0},
    {"model_quotas": {"sol-6.1": {"rpm": 1}}},
    {"model_quotas": {"sol-6.1": {"rpm": 0, "tpm": 1}}},
    {"model_quotas": {"sol-6.1": {"rpm": 1, "tpm": 1, "rph": 1}}},
    {"burst": 1},
]


def _schema_rejected(cfg: dict[str, Any]) -> None:
    errors = validate_config(cfg, env="test")[1]
    assert errors and all(e.startswith("schema:") for e in errors), errors


@pytest.mark.parametrize(("section", "key", "value"), PLATFORM_BOUNDS)
def test_platform_bounds(example: dict[str, Any], section: str, key: str, value: object) -> None:
    example["platform"] = {section: {key: value}}
    _schema_rejected(example)


@pytest.mark.parametrize(("key", "value"), DATA_CLASS_BOUNDS)
def test_data_class_bounds(example: dict[str, Any], key: str, value: object) -> None:
    example["platform"] = {"data_classes": {"internal": {key: value}}}
    _schema_rejected(example)


def test_unknown_data_class(example: dict[str, Any]) -> None:
    example["platform"] = {"data_classes": {"secret": {"l2_cache": False}}}
    _schema_rejected(example)


@pytest.mark.parametrize(("key", "value"), ACCEPTANCE_BOUNDS)
def test_acceptance_bounds(example: dict[str, Any], key: str, value: float) -> None:
    example["labeling"]["acceptance"] = {key: value}
    _schema_rejected(example)


def test_family_overlap_lift_bound(example: dict[str, Any]) -> None:
    example["labeling"]["family_overlap_lift_max"] = 0.11
    _schema_rejected(example)


@pytest.mark.parametrize("limits", TENANT_LIMIT_BOUNDS)
def test_tenant_limit_bounds(example: dict[str, Any], limits: dict[str, Any]) -> None:
    example["tenants"]["t_acme"]["limits"] = limits
    _schema_rejected(example)


def test_context_window_bound(example: dict[str, Any]) -> None:
    example["models"]["sol-6.1"]["context_window"] = 1023
    _schema_rejected(example)
    example["models"]["sol-6.1"]["context_window"] = 1024
    assert validate_config(example, env="test")[1] == []


def test_the_boundary_values_themselves_are_valid(example: dict[str, Any]) -> None:
    example["platform"] = {
        "limits": {"max_json_body_bytes": 1_048_576, "admission_queue_timeout_ms": 30_000},
        "idempotency": {"ttl_s": 604_800},
        "events": {"resume_window_s": 30},
        "cost": {"cancel_overrun_tokens": 4096},
        "media": {"inline_wait_ms": 100},
        "credentials": {"mantle_auth": "sigv4"},
        "data_classes": {"restricted": {"retain_days": 3650, "l2_cache": False}},
    }
    example["labeling"]["acceptance"] = {"ece_max": 0.05, "n_min": 300, "consensus_min": 1, "drift_ece_max": 0.05}
    assert validate_config(example, env="test")[1] == []
