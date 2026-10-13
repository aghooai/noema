"""Toxiproxy: the shared host instance (profile toxiproxy) and the fault-layer instance (profile fault)."""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from tests.integration.infra.helpers import Compose

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("docker")]


def valkey_ping(port: int, timeout: float = 2.0) -> bool:
    """One RESP PING to 127.0.0.1:port; False on any connection error or a non-PONG answer."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as conn:
            conn.sendall(b"PING\r\n")
            return conn.recv(64).startswith(b"+PONG")
    except OSError:
        return False


def listen_port(api: httpx.Client, proxy: str) -> int:
    listen: str = api.get(f"/proxies/{proxy}").raise_for_status().json()["listen"]
    return int(listen.rsplit(":", 1)[1])


@pytest.fixture
def host_toxiproxy() -> Iterator[httpx.Client]:
    port = os.environ.get("NOEMA_TOXIPROXY_PORT", "28474")
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0)
    try:
        client.get("/version").raise_for_status()
    except httpx.HTTPError:
        client.close()
        pytest.skip("the host toxiproxy is not running: make up")
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def run_proxy(host_toxiproxy: httpx.Client, noema_run_id: str, noema_services: Any) -> Iterator[str]:
    """A proxy owned by this session in front of the shared valkey, listening on an ephemeral loopback port."""
    upstream = urlsplit(noema_services.redis_url)
    name = f"{noema_run_id}-valkey"
    body = {"name": name, "listen": "127.0.0.1:0", "upstream": f"{upstream.hostname}:{upstream.port}", "enabled": True}
    host_toxiproxy.post("/proxies", json=body).raise_for_status()
    try:
        yield name
    finally:
        host_toxiproxy.delete(f"/proxies/{name}")


def test_host_toxiproxy_proxy_can_be_cut_and_restored(host_toxiproxy: httpx.Client, run_proxy: str) -> None:
    port = listen_port(host_toxiproxy, run_proxy)
    assert valkey_ping(port)

    host_toxiproxy.post(f"/proxies/{run_proxy}", json={"enabled": False}).raise_for_status()
    assert not valkey_ping(port)

    host_toxiproxy.post(f"/proxies/{run_proxy}", json={"enabled": True}).raise_for_status()
    assert valkey_ping(listen_port(host_toxiproxy, run_proxy))


def test_host_toxiproxy_latency_toxic_delays_replies(host_toxiproxy: httpx.Client, run_proxy: str) -> None:
    toxic = {"name": "slow", "type": "latency", "stream": "downstream", "attributes": {"latency": 300}}
    host_toxiproxy.post(f"/proxies/{run_proxy}/toxics", json=toxic).raise_for_status()
    port = listen_port(host_toxiproxy, run_proxy)
    started = time.monotonic()
    assert valkey_ping(port, timeout=5.0)
    assert time.monotonic() - started >= 0.25  # a lower bound only: latency can only add


@pytest.mark.slow
@pytest.mark.timeout(240)
def test_fault_toxiproxy_fronts_the_project_services(isolated_compose: Compose) -> None:
    up = isolated_compose("up", "-d", "--wait", "toxiproxy", profiles=["fault"], timeout=200)
    assert up.returncode == 0, up.stderr
    with httpx.Client(base_url=f"http://127.0.0.1:{isolated_compose.port('toxiproxy', 8474)}", timeout=5.0) as api:
        assert set(api.get("/proxies").raise_for_status().json()) == {"postgres", "valkey", "moto"}
        port = isolated_compose.port("toxiproxy", 16379)
        assert valkey_ping(port)
        api.post("/proxies/valkey", json={"enabled": False}).raise_for_status()
        assert not valkey_ping(port)
        api.post("/proxies/valkey", json={"enabled": True}).raise_for_status()
        assert valkey_ping(port)
