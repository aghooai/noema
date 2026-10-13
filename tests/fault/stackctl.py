"""Control of the fault stack: toxiproxy (postgres, valkey, moto proxies), platform-config versions with the tenant
rate limits lifted, breaker states and process metrics (`/metrics` of both api replicas)."""

from __future__ import annotations

import tempfile
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

import anyio
import httpx
import yaml
from tests.e2e.conftest import Stack
from tests.e2e.suite.kit import admin, metric_samples, turn_body
from tests.e2e.suite.multipod import ROUTER_MEMBERS, activate, active_version, probe_decision, tripped
from tools.bootstrap_dev import platform_config

Json = dict[str, Any]
PROXIES: Final = ("postgres", "valkey", "moto")
# spec-05 §1.2: a load or fault run sets its own tenant limits (one turn reserves 28 calls; the per-key default of
# 6,000 rpm admits ~214 turns a minute, below what a 200-turn row sends)
FAULT_LIMITS: Final[Mapping[str, int]] = {
    "rpm_per_key": 50_000_000,
    "tpm_per_key": 100_000_000_000,
    "rpm_per_project": 50_000_000,
    "tpm_per_project": 100_000_000_000,
    "concurrent_streams_per_project": 10_000,
}


class Toxiproxy:
    """The stack's toxiproxy API (proxies `postgres`, `valkey`, `moto` in front of the app containers)."""

    def __init__(self, url: str) -> None:
        self.http = httpx.AsyncClient(base_url=url, timeout=10.0)

    async def enable(self, proxy: str, enabled: bool) -> float:
        """Enable or disable one proxy (disabled: every connection through it is closed, new ones refused);
        returns the monotonic time of the change."""
        response = await self.http.post(f"/proxies/{proxy}", json={"enabled": enabled})
        assert response.status_code == 200, response.text
        return time.monotonic()

    async def blackhole(self, proxy: str) -> float:
        """A `timeout` toxic with timeout 0 both ways: connections stay open and no byte passes (a hung peer)."""
        for stream in ("upstream", "downstream"):
            toxic = {"name": f"hang-{stream}", "type": "timeout", "stream": stream, "attributes": {"timeout": 0}}
            response = await self.http.post(f"/proxies/{proxy}/toxics", json=toxic)
            assert response.status_code == 200, response.text
        return time.monotonic()

    async def reset(self) -> None:
        """Every proxy enabled, every toxic removed."""
        response = await self.http.post("/reset")
        assert response.status_code == 204, response.text

    async def state(self) -> dict[str, bool]:
        proxies = (await self.http.get("/proxies")).json()
        return {name: bool(p["enabled"]) for name, p in proxies.items()}

    async def __aenter__(self) -> Toxiproxy:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.http.aclose()


# --- config versions ------------------------------------------------------------------------------------------
def fault_doc(version: int, prices_ref: str | None, patch: Callable[[Json], None] | None = None) -> Json:
    """The bootstrap platform config at `version` with FAULT_LIMITS on every tenant (and `patch` applied)."""
    doc = platform_config(version, prices_ref=prices_ref)
    for tenant in doc["tenants"].values():
        tenant["limits"] = dict(FAULT_LIMITS)
    if patch is not None:
        patch(doc)
    return doc


async def next_version(stack: Stack) -> int:
    listed = (await admin(stack, ["config", "list"]))["data"]
    return 1 + max(int(v["version"]) for v in listed)


async def activate_doc(stack: Stack, doc: Json) -> int:
    with tempfile.TemporaryDirectory(prefix="noema-fault-") as tmp:
        path = Path(tmp) / f"platform-v{doc['config_version']}.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False))
        return await activate(stack, path)


async def wait_rate_limit(stack: Stack, limit: int, *, timeout_s: float = 30.0) -> None:
    """Until both replicas answer a turn with `x-ratelimit-limit-requests: limit` (the version is loaded)."""
    with anyio.fail_after(timeout_s):
        for replica in (0, 1):
            client = stack.client(replica)
            while True:
                request = turn_body("Config probe: the capital of France?", stream=False)
                response = await client.request("POST", "/v1/turns", json=request)
                if response.headers.get("x-ratelimit-limit-requests") == str(limit):
                    break
                await anyio.sleep(0.5)


async def ensure_fault_config(stack: Stack) -> int:
    """A new version = the bootstrap config with FAULT_LIMITS, active on both replicas; its version."""
    prices_ref = stack.state["config"].get("prices_ref")
    version = await next_version(stack)
    await activate_doc(stack, fault_doc(version, prices_ref))
    await wait_rate_limit(stack, FAULT_LIMITS["rpm_per_key"])
    return await active_version(stack)


@asynccontextmanager
async def patched(stack: Stack, base: int, patch: Callable[[Json], None]) -> AsyncIterator[int]:
    """The fault config with `patch` active for the block; then `base` re-activated (rollback + approve)."""
    version = await next_version(stack)
    patched_version = await activate_doc(stack, fault_doc(version, stack.state["config"].get("prices_ref"), patch))
    try:
        yield patched_version
    finally:
        restored = await admin(stack, ["config", "rollback", str(base)])
        await admin(stack, ["config", "approve", str(restored["version"])], operator="noema-bootstrap-b")


# --- metrics --------------------------------------------------------------------------------------------------
async def circuit_states(stack: Stack, model: str) -> tuple[float | None, float | None]:
    """`aghoo_circuit_state` of `model`'s service breaker on (api-1, api-2): 0 closed, 1 half-open, 2 open."""
    out: list[float | None] = []
    for replica in (0, 1):
        found = [v for lb, v in await metric_samples(stack, replica, "aghoo_circuit_state") if lb.get("model") == model]
        out.append(found[0] if found else None)
    return out[0], out[1]


async def metric_total(stack: Stack, name: str, **labels: str) -> float:
    """The sum over both replicas of every sample of `name` whose labels include `labels`."""
    total = 0.0
    for replica in (0, 1):
        for got, value in await metric_samples(stack, replica, name):
            if all(got.get(k) == v for k, v in labels.items()):
                total += value
    return total


class CircuitMonitor:
    """Polls `aghoo_circuit_state` of one model on both replicas every `every_s` (run it in a task group); the
    samples are (monotonic s, api-1 state, api-2 state) with None before the gauge's first transition."""

    def __init__(self, stack: Stack, model: str, every_s: float = 0.25) -> None:
        self.stack, self.model, self.every_s = stack, model, every_s
        self.samples: list[tuple[float, float | None, float | None]] = []

    async def run(self) -> None:
        while True:
            one, two = await circuit_states(self.stack, self.model)
            self.samples.append((time.monotonic(), one, two))
            await anyio.sleep(self.every_s)

    def first(self, after: float, pred: Callable[[float | None, float | None], bool]) -> float | None:
        """The first sample time >= `after` whose states satisfy `pred`, else None."""
        return next((t for t, one, two in self.samples if t >= after and pred(one, two)), None)

    def transitions(self, t0: float) -> list[tuple[float, float | None, float | None]]:
        """(s after t0, api-1, api-2) at every change of the pair of states."""
        out: list[tuple[float, float | None, float | None]] = []
        for t, one, two in self.samples:
            if not out or out[-1][1:] != (one, two):
                out.append((round(t - t0, 2), one, two))
        return out


def any_open(one: float | None, two: float | None) -> bool:
    return 2.0 in (one, two)


def both_closed(one: float | None, two: float | None) -> bool:
    return one in (None, 0.0) and two in (None, 0.0)


# --- healing the breakers between rows ------------------------------------------------------------------------
PROBE_PROFILE: Final[Mapping[str, str]] = {
    "us.openai.gpt-6.1-sol": "sol-fast",
    "us.anthropic.claude-opus-5-5": "opus-deep",
    "us.zai.glm-5.3": "glm-reason",
}


async def heal(stack: Stack, *, timeout_s: float = 60.0) -> None:
    """Until every service circuit is closed on both replicas (the default fault scenario loaded): every second,
    each replica with a tripped circuit gets a call that reaches that model once admitted (a router-default decision
    for the members, a `stream: false` turn pinned to a profile whose first member is the model otherwise). The
    state moves only on a call (lazy half-open, then a 1-in-20 probe), as `tests.e2e.suite.multipod.heal_circuits`."""
    clients = [stack.client(0), stack.client(1)]
    with anyio.move_on_after(timeout_s):
        while left := await tripped(stack):
            for name, states in left.items():
                client = clients[int(name[-1]) - 1]
                if set(states) & ROUTER_MEMBERS:
                    await client.request("POST", "/v1/decisions", json=probe_decision())
                for model in sorted(set(states) - ROUTER_MEMBERS):
                    profile = PROBE_PROFILE.get(model, "sol-fast")
                    probe = turn_body("Heal probe: the capital of France?", stream=False)
                    probe["generation"] = {"profile": profile}
                    await client.request("POST", "/v1/turns", json=probe)
            await anyio.sleep(1.0)
        return
    raise AssertionError(f"service circuits still not closed after {timeout_s:.0f} s: {await tripped(stack)}")


async def sql(stack: Stack, query: str) -> list[list[str]]:
    """One read-only query as the stack's Postgres superuser (docker exec psql, dev stack only); rows of strings."""
    container = f"{stack.state['project']}-postgres-1"
    argv = ["docker", "exec", container, "psql", "-U", "postgres", "-d", "noema", "-tA", "-F", "\t", "-c", query]
    done = await anyio.run_process(argv, check=True)
    return [line.split("\t") for line in done.stdout.decode().splitlines() if line]
