"""SE15 (supporting; spec-09 §10 SEC-CFG-01 and SEC-EDGE-01) and the config side of SE4 on the stack: `noema-admin
config approve` by the proposer is refused (two-person rule) and the active version does not move; a config whose
tenant role ARN is in another tenant's account is refused at propose; a JSON body of 50 MiB
+ 1 B is 413 `request_too_large` (contract-validated) on both replicas."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.e2e.conftest import Stack
from tests.e2e.suite.kit import AdminRefused, admin
from tests.e2e.suite.multipod import active_version
from tools.bootstrap_dev import platform_config

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(180)]
LIMIT = 52_428_800  # platform.limits.max_json_body_bytes (IMPL-064)


async def config_file(stack: Stack, tmp_path: Path, name: str, **beta: Any) -> Path:
    listed = (await admin(stack, ["config", "list"]))["data"]
    doc = platform_config(
        1 + max(int(v["version"]) for v in listed), prices_ref=stack.state["config"].get("prices_ref")
    )
    doc["tenants"]["t_beta"].update(beta)
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


async def test_the_proposer_cannot_approve(stack: Stack, tmp_path: Path) -> None:
    before = await active_version(stack)
    proposed = await admin(stack, ["config", "propose", str(await config_file(stack, tmp_path, "same"))])
    with pytest.raises(AdminRefused) as refused:
        await admin(stack, ["config", "approve", str(proposed["version"])])  # operator A again
    assert refused.value.code == 1 and "two_person" in refused.value.stderr
    assert await active_version(stack) == before


async def test_a_role_arn_in_another_account_is_refused_at_propose(stack: Stack, tmp_path: Path) -> None:
    """t_beta (444455556666) pointed at t_acme's account; the broker's own check (role `Admin` in the right account
    -> role_not_allowlisted) is tests/security/test_sts.py."""
    before = await active_version(stack)
    arn = "arn:aws:iam::111122223333:role/AghooBedrockInvoke"
    path = await config_file(stack, tmp_path, "arn", invoke_role_arn=arn)
    with pytest.raises(AdminRefused) as refused:
        await admin(stack, ["config", "propose", str(path)])
    assert refused.value.code == 1 and "invoke_role_arn" in refused.value.stderr, refused.value.stderr
    assert await active_version(stack) == before


@pytest.mark.parametrize("replica", [0, 1])
async def test_a_json_body_over_the_cap_is_413_before_parsing(stack: Stack, replica: int) -> None:
    head, tail = b'{"input": "', b'"}'
    body = head + b"x" * (LIMIT + 1 - len(head) - len(tail)) + tail  # LIMIT + 1 bytes, valid JSON
    assert len(body) == LIMIT + 1
    response = await stack.client(replica).request(
        "POST", "/v1/decisions", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 413 and response.json()["error"]["code"] == "request_too_large"
