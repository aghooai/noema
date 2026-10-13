"""The load runs' stack: a compose project of its own (base services only: postgres, valkey, moto), seeded by
`tools.bootstrap_dev`, plus a load config that lifts the tenant rate limits (spec-05 §1.2: "the Phase 1 load test sets
its own"; one turn reserves 28 calls, so the 6,000 rpm per-key default admits about 214 turns a minute).

    E2E_PROJECT=noema-perf E2E_PORT_BASE=28700 ... docker compose up -d --wait postgres valkey moto
    python -m tools.bootstrap_dev --compose-project noema-perf --state ~/.noema/stacks/noema-perf.json
    python -m tests.load.stack activate --state ~/.noema/stacks/noema-perf.json

`activate` proposes (operator A) and approves (operator B) the next config version = the bootstrap config with
LOAD_LIMITS on every tenant; a running api loads it within 5 s. Dev/test stacks only (moto, never AWS).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import anyio
import orjson
import yaml
from tools.bootstrap_dev import Target, admin, platform_config

LOAD_LIMITS: Final[Mapping[str, int]] = {
    "rpm_per_key": 50_000_000,
    "tpm_per_key": 100_000_000_000,
    "rpm_per_project": 50_000_000,
    "tpm_per_project": 100_000_000_000,
    "concurrent_streams_per_project": 10_000,
}


def load_config(version: int, limits: Sequence[str] = (), quotas: Sequence[str] = ()) -> dict[str, Any]:
    """The bootstrap's platform config at `version` with LOAD_LIMITS on every tenant, then the overrides:
    `limits` = `<tenant>:<key>=<int>` (C5: the flooder's `concurrent_streams_per_project`), `quotas` =
    `<tenant>:<model alias>:<rpm>:<tpm>` (C1: `limits.model_quotas`, the tenant account's Bedrock quota)."""
    doc = platform_config(version)
    for tenant in doc["tenants"].values():
        tenant["limits"] = dict(LOAD_LIMITS)
    for item in limits:
        tenant_id, _, setting = item.partition(":")
        key, _, value = setting.partition("=")
        doc["tenants"][tenant_id]["limits"][key] = int(value)
    for item in quotas:
        tenant_id, alias, rpm, tpm = item.split(":")
        model_quotas = doc["tenants"][tenant_id]["limits"].setdefault("model_quotas", {})
        model_quotas[alias] = {"rpm": int(rpm), "tpm": int(tpm)}
    return doc


def target_of(state: Mapping[str, Any]) -> Target:
    endpoints = state["endpoints"]
    return Target(
        project=str(state["project"]),
        env=str(state["env"]),
        pg_host=str(endpoints["postgres"]),
        moto_url=str(endpoints["moto"]),
        redis_url=str(endpoints.get("redis", "")),
    )


async def activate(state: Mapping[str, Any], limits: Sequence[str] = (), quotas: Sequence[str] = ()) -> int:
    target = target_of(state)
    op_a = state["operators"]["noema-bootstrap-a"]["key"]
    op_b = state["operators"]["noema-bootstrap-b"]["key"]
    listed = (await admin(target, ["config", "list"], op_a)).get("data", [])
    version = 1 + max((int(v["version"]) for v in listed), default=0)
    with tempfile.TemporaryDirectory(prefix="noema-load-") as tmp:
        path = Path(tmp) / f"platform-v{version}.yaml"
        path.write_text(yaml.safe_dump(load_config(version, limits, quotas), sort_keys=False))
        proposed = await admin(target, ["config", "propose", str(path)], op_a)
    await admin(target, ["config", "approve", str(version)], op_b)
    print(f"load config v{version} active ({proposed['digest']})")
    return version


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.load.stack", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("activate",))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--limit", action="append", default=[], help="<tenant>:<limit key>=<int> (repeatable)")
    parser.add_argument("--quota", action="append", default=[], help="<tenant>:<model alias>:<rpm>:<tpm> (repeatable)")
    args = parser.parse_args(argv)
    anyio.run(activate, orjson.loads(args.state.read_bytes()), args.limit, args.quota)
    return 0


if __name__ == "__main__":
    sys.exit(main())
