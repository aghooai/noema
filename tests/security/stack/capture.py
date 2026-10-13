"""Captures the security stack's telemetry and stores for the SE12 scan: every container's JSON logs (`docker
compose logs`), a Valkey dump (every key and value), a Postgres data dump (`pg_dump` in the postgres container), the
wire mock's request log, and the Prometheus text of every worker of the api replicas; plus the known secret values
(bootstrap state, moto Secrets Manager: peppers, DSN passwords, ExternalIds)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import anyio
import httpx
import orjson
import redis.asyncio as aioredis
from aiobotocore.session import get_session
from tests.e2e.conftest import REPO, Stack
from tests.e2e.suite.kit import metrics_urls

COMPOSE_FILE = REPO / "deploy" / "compose" / "docker-compose.yml"


def compose(project: str, *args: str) -> list[str]:
    return ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE), "--profile", "*", *args]


async def run_to(path: Path, argv: list[str]) -> None:
    done = await anyio.run_process(argv, check=False)
    assert done.returncode == 0, done.stderr.decode(errors="replace")[-500:]
    await anyio.Path(path).write_bytes(done.stdout)


async def redis_dump(url: str, path: Path) -> int:
    client: Any = aioredis.from_url(url)
    lines: list[bytes] = []
    try:
        async for key in client.scan_iter(count=1000):
            kind = (await client.type(key)).decode()
            if kind == "string":
                value: Any = await client.get(key)
            elif kind == "hash":
                value = await client.hgetall(key)
            elif kind == "list":
                value = await client.lrange(key, 0, -1)
            elif kind in ("set", "zset"):
                value = await (client.smembers(key) if kind == "set" else client.zrange(key, 0, -1))
            elif kind == "stream":
                value = await client.xrange(key)
            else:
                value = b""
            lines.append(key + b"\t" + kind.encode() + b"\t" + repr(value).encode())
    finally:
        await client.aclose()
    await anyio.Path(path).write_bytes(b"\n".join(lines))
    return len(lines)


async def mock_dump(url: str, path: Path) -> int:
    rows: list[bytes] = []
    after = 0
    async with httpx.AsyncClient(base_url=url, timeout=60) as http:
        while True:
            page = (await http.get("/_mock/requests", params={"after": after, "limit": 500})).json()["requests"]
            rows += [orjson.dumps(r) for r in page]
            if len(page) < 500:
                break
            after = int(page[-1]["seq"])
    await anyio.Path(path).write_bytes(b"\n".join(rows))
    return len(rows)


async def capture(stack: Stack, out: Path) -> dict[str, Path]:
    project = os.environ.get("NOEMA_E2E_PROJECT") or str(stack.state.get("project"))
    await anyio.Path(out).mkdir(parents=True, exist_ok=True)
    files = {name: out / name for name in ("compose.log", "redis.txt", "db.sql", "mock-requests.jsonl")}
    await run_to(files["compose.log"], compose(project, "logs", "--no-color", "--timestamps"))
    dump = ["exec", "-T", "postgres", "pg_dump", "-U", "postgres", "--data-only", "noema"]
    await run_to(files["db.sql"], compose(project, *dump))
    await redis_dump(str(stack.state["endpoints"]["redis"]), files["redis.txt"])
    await mock_dump(stack.mock_url, files["mock-requests.jsonl"])
    async with httpx.AsyncClient(timeout=10) as http:  # every worker of every api replica (defect 24)
        for replica in range(len(stack.state["endpoints"]["metrics"])):
            for worker, url in enumerate(metrics_urls(stack, replica)):
                name = f"metrics-{replica}-{worker}.txt"
                files[name] = out / name
                await anyio.Path(files[name]).write_text((await http.get(f"{url}/metrics")).text)
    return files


async def moto_secrets(stack: Stack) -> dict[str, str]:
    """Every Secrets Manager value of the stack's moto (fake credentials)."""
    out: dict[str, str] = {}
    session = get_session()
    fake = "noema-compose-fake"  # the stack's documented fake AWS credentials (moto accepts any)
    kwargs: Any = {"region_name": "us-east-1", "endpoint_url": stack.state["endpoints"]["moto"]}
    kwargs |= {"aws_access_key_id": fake, "aws_secret_access_key": fake}
    async with session.create_client("secretsmanager", **kwargs) as sm:
        names = [s["Name"] for s in (await sm.list_secrets(MaxResults=100))["SecretList"]]
        for name in names:
            out[name] = (await sm.get_secret_value(SecretId=name))["SecretString"]
    return out


def secret_values(stack: Stack, moto: dict[str, str], presented: dict[str, str]) -> dict[str, str]:
    """label -> exact secret string (keys and their secret halves, peppers, DSN passwords, ExternalIds)."""
    values: dict[str, str] = {}
    for name, op in stack.state["operators"].items():
        values[f"operator:{name}"] = str(op["key"])
        values[f"operator-secret:{name}"] = str(op["key"]).split(".", 1)[1]
    for tenant, t in stack.state["tenants"].items():
        values[f"external_id:{tenant}"] = str(t["external_id"])
        for project, p in t["projects"].items():
            for role, who in p["principals"].items():
                values[f"key:{tenant}/{project}/{role}"] = str(who["key"])
                values[f"key-secret:{tenant}/{project}/{role}"] = str(who["key"]).split(".", 1)[1]
    for reason, key in presented.items():
        if "." in key and len(key.split(".", 1)[1]) >= 8:
            values[f"presented-secret:{reason}"] = key.split(".", 1)[1]
    for name, raw in moto.items():
        if name.endswith("pepper"):
            parsed = orjson.loads(raw) if raw.startswith("{") else {"pepper": raw}
            values[f"pepper:{name}"] = str(parsed.get("pepper", raw))
        elif "dsn" in name:
            values[f"dsn-password:{name}"] = raw.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
        else:
            values[f"secret:{name}"] = raw
    return values
