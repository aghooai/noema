"""noema-admin without a database: environment rules, local validation, argument refusals, ExternalId format and
the calibrator retirement selection."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from noema.contracts.bundle import VENDOR_DIR
from noema.core.settings import Env
from noema.identity.cli.context import CliError, endpoint_overrides, parse_env
from noema.identity.cli.noema_admin import amain, build_parser
from noema.identity.cli.platform_cmds import retire_left_calibrators
from noema.identity.cli.tenancy import new_external_id

EXAMPLE = VENDOR_DIR / "examples" / "platform.example.yaml"


async def run(argv: list[str], environ: dict[str, str]) -> tuple[int, Any, Any]:
    out, err = io.StringIO(), io.StringIO()
    code = await amain(argv, environ=environ, stdout=out, stderr=err)
    return code, json.loads(out.getvalue() or "null"), json.loads(err.getvalue() or "null")


def test_environment_rules() -> None:
    assert parse_env({}) == "prod"
    assert parse_env({"NOEMA_ENV": "dev"}) == "dev"
    with pytest.raises(CliError):
        parse_env({"NOEMA_ENV": "qa"})
    overrides = {"NOEMA_AWS_ENDPOINT_OVERRIDES": '{"secretsmanager": "http://127.0.0.1:25000"}'}
    assert endpoint_overrides(overrides, "dev") == {"secretsmanager": "http://127.0.0.1:25000"}
    refused: tuple[Env, ...] = ("staging", "prod")
    for env in refused:
        with pytest.raises(CliError) as err:
            endpoint_overrides(overrides, env)
        assert err.value.reason == "forbidden"
    for raw in ('{"iam": "http://x:1"}', '{"sts": "ftp://x"}', "not json", "[]"):
        with pytest.raises(CliError):
            endpoint_overrides({"NOEMA_AWS_ENDPOINT_OVERRIDES": raw}, "dev")
    assert endpoint_overrides({"NOEMA_AWS_ENDPOINT_OVERRIDES": " "}, "prod") == {}


async def test_local_validation(tmp_path: Path) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(EXAMPLE.read_text())
    code, out, _ = await run(["config", "validate", str(good)], {"NOEMA_ENV": "test"})
    assert code == 0 and out["valid"] is True and out["config_version"] == 1
    body = yaml.safe_load(EXAMPLE.read_text())
    body["decision_profiles"]["router-default"]["batch_questions"] = True  # P1-D2
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(body))
    code, _, err = await run(["config", "validate", str(bad)], {"NOEMA_ENV": "test"})
    assert code == 1 and err["error"] == "invalid_config" and "P1-D2" in err["message"]
    code, _, err = await run(["config", "validate", str(tmp_path / "missing.yaml")], {"NOEMA_ENV": "test"})
    assert code == 1 and err["error"] == "invalid_argument"


async def test_refusals_before_any_connection() -> None:
    prod = {"NOEMA_ENV": "prod"}
    argv = ["keys", "create", "--tenant", "t", "--project", "p", "--principal", "prn", "--scopes", "turns"]
    code, _, err = await run([*argv, "--env", "test"], prod)
    assert code == 1 and "ak_test_" in err["message"]
    code, _, err = await run([*argv, "--expires", "yesterday"], prod)
    assert code == 1 and err["error"] == "invalid_argument"
    code, _, err = await run([*argv, "--ip-allow", "10.0.0.0/33"], prod)
    assert code == 1 and err["error"] == "invalid_argument"
    code, _, err = await run(["keys", "list", "--tenant", "t"], prod)
    assert code == 1 and err == {"error": "invalid_argument", "message": "NOEMA_CLI_DSN is not set"}
    with pytest.raises(SystemExit):
        build_parser().parse_args(["calibrators", "retire", "cal_1", "--reason", "drift_ece"])


def test_external_id_format() -> None:
    values = {new_external_id("t_acme") for _ in range(20)}
    assert len(values) == 20
    assert all(re.fullmatch(r"aghoo-t_acme-[A-Za-z0-9]{32}", v) for v in values)


class FakeAdmin:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.retired: list[tuple[str, str]] = []

    async def list_calibrators(self, op: str) -> list[dict[str, Any]]:
        return self.rows

    async def calibrator_retire(self, op: str, calibrator_id: str, *, reason: str) -> None:
        self.retired.append((calibrator_id, reason))


async def test_retirement_selection() -> None:
    rows = [
        {"id": "cal_1", "status": "active", "profile_id": "p", "profile_version": "2"},
        {"id": "cal_2", "status": "active", "profile_id": "p", "profile_version": "1"},
        {"id": "cal_3", "status": "accepted", "profile_id": "gone", "profile_version": "1"},
        {"id": "cal_4", "status": "fitted", "profile_id": "gone", "profile_version": "1"},
        {"id": "cal_5", "status": "rejected", "profile_id": "gone", "profile_version": "1"},
        {"id": "cal_6", "status": "downgraded", "profile_id": "gone", "profile_version": "1"},
        {"id": "cal_7", "status": "retired", "profile_id": "gone", "profile_version": "1"},
    ]
    admin = FakeAdmin(rows)
    retired = await retire_left_calibrators(admin, "op_1", {"p": {"version": 2}})  # type: ignore[arg-type]
    assert retired == ["cal_2", "cal_3", "cal_4"]
    assert admin.retired == [(c, "profile_version_retired") for c in ("cal_2", "cal_3", "cal_4")]
