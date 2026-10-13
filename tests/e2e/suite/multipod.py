"""Helpers of the multi-pod e2e tests: keyed clients, minted keys, temporary platform-config changes, breakers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
import yaml
from tests.e2e.conftest import CheckedClient, Stack
from tests.e2e.suite.kit import Json, admin, metric_samples, tag, turn_body
from tools.bootstrap_dev import platform_config

from noema.core.principal import ALL_SCOPES

OPEN = 2.0  # aghoo_circuit_state: 0 closed, 1 half-open, 2 open
HEAL_TIMEOUT_S = 45.0  # open_s (15 s) + a probe per replica + slack
ROUTER_MEMBERS = frozenset({"deepseek.v3.2", "us.openai.gpt-6-luna", "us.anthropic.claude-haiku-5-5"})


def keyed(stack: Stack, replica: int, key: str) -> CheckedClient:
    client = stack.client(replica, role=None)
    client.http.headers["authorization"] = f"Bearer {key}"
    return client


async def mint_key(stack: Stack, tenant: str, project: str, role: str) -> Json:
    """A fresh `ak_test_` key for an existing bootstrap principal (noema-admin keys create)."""
    who = stack.principal(tenant, project, role)
    argv = ["keys", "create", "--tenant", tenant, "--project", stack.project_id(tenant, project)]
    argv += ["--principal", str(who["principal_id"]), "--scopes", ",".join(sorted(ALL_SCOPES)), "--env", "test"]
    return await admin(stack, argv)


async def active_version(stack: Stack) -> int:
    listed = (await admin(stack, ["config", "list"]))["data"]
    return int(next(v["version"] for v in listed if v["status"] == "active"))


async def activate(stack: Stack, path: Path) -> int:
    """Propose (operator A) and approve (operator B, the two-person rule) one config file."""
    proposed = await admin(stack, ["config", "propose", str(path)])
    version = int(proposed["version"])
    await admin(stack, ["config", "approve", str(version)], operator="noema-bootstrap-b")
    return version


@asynccontextmanager
async def patched_config(stack: Stack, tmp: Path, patch: Callable[[dict[str, Any]], None]) -> AsyncIterator[int]:
    """The bootstrap platform config with `patch` applied, active for the block; afterwards the previous version is
    re-proposed (`config rollback`) and approved again. Yields the patched version."""
    previous = await active_version(stack)
    listed = (await admin(stack, ["config", "list"]))["data"]
    doc = platform_config(
        1 + max(int(v["version"]) for v in listed), prices_ref=stack.state["config"].get("prices_ref")
    )
    patch(doc)
    path = tmp / "platform-e2e.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    version = await activate(stack, path)
    try:
        yield version
    finally:
        restored = await admin(stack, ["config", "rollback", str(previous)])
        await admin(stack, ["config", "approve", str(restored["version"])], operator="noema-bootstrap-b")


async def circuit(stack: Stack, replica: int, model: str) -> float | None:
    """aghoo_circuit_state of `model` on one replica (None before its first transition)."""
    for labels, value in await metric_samples(stack, replica, "aghoo_circuit_state"):
        if labels.get("model") == model:
            return value
    return None


async def wait_for(check: Callable[[], Any], *, timeout_s: float, every_s: float = 0.25) -> None:
    """Awaits `check()` until it is truthy."""
    with anyio.fail_after(timeout_s):
        while True:
            if await check():
                return
            await anyio.sleep(every_s)


async def tripped(stack: Stack) -> dict[str, dict[str, float]]:
    """Every service circuit that is not closed, per replica (`api-1`/`api-2` -> {model: state})."""
    out: dict[str, dict[str, float]] = {}
    for replica in (0, 1):
        states = {lb["model"]: v for lb, v in await metric_samples(stack, replica, "aghoo_circuit_state") if v != 0}
        if states:
            out[f"api-{replica + 1}"] = states
    return out


def probe_decision() -> Json:
    """A router-default decision of five predicates (every member asked; the default scenario answers them)."""
    marker = tag("heal")
    questions = [{"type": "predicate", "name": f"p{i}", "instructions": f"Fact {i} ({marker})."} for i in range(5)]
    return {"profile": "router-default", "escalation": "off", "input": f"Some text ({marker})", "questions": questions}


async def heal_circuits(stack: Stack, *, timeout_s: float = HEAL_TIMEOUT_S) -> None:
    """Wait until every service circuit is closed on both replicas (the breakers are shared through breaker_events
    and outlive a test). Every second, each replica with a tripped circuit gets a call that reaches the model: a
    router-default decision for the decision members, a `stream: false` turn (sol-fast) for the others. While open
    the call is refused (`circuit_open`, nothing sent); the first one after open_s turns the breaker half-open (the
    state moves only on a call, so the gauge stays 2 until then) and is its probe. Run with the default scenario
    loaded. Fails with the circuits still open after `timeout_s`."""
    with anyio.move_on_after(timeout_s):
        while left := await tripped(stack):
            for name, states in left.items():
                client = stack.client(int(name[-1]) - 1)
                if set(states) & ROUTER_MEMBERS:
                    await client.request("POST", "/v1/decisions", json=probe_decision(), expect=200)
                if set(states) - ROUTER_MEMBERS:
                    body = turn_body(f"What is the capital of France? ({tag('heal')})", stream=False)
                    await client.request("POST", "/v1/turns", json=body, expect=200)
            await anyio.sleep(1.0)
        return
    pytest.fail(f"service circuits still not closed after {timeout_s:.0f} s: {await tripped(stack)}")
