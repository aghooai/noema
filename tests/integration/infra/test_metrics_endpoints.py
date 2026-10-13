"""Defect 24: every api worker's metrics endpoint (WEB_CONCURRENCY processes per pod, docs/modules/integration.md §5.1).

The bootstrap records NOEMA_METRICS_PORT .. + WEB_CONCURRENCY - 1 of each api replica (from the running container's
environment, through a fake docker CLI here), and the e2e/fault/security metric helpers read every one of them, so a
counter that moved in a replica's second worker is counted (the counter-delta gates F2, F10, SEC-ISO-02, SE12).
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.e2e.conftest import Stack
from tests.e2e.suite.kit import metric_samples
from tests.fault.stackctl import metric_total
from tools import bootstrap_dev

pytestmark = pytest.mark.integration

COUNTER = "aghoo_security_violations_total"
BASE_PORTS = {("postgres", 5432): 30032, ("moto", 5000): 30050, ("api-1", 8000): 30001, ("api-2", 8000): 30002}


def fake_docker(published: Mapping[tuple[str, int], int], workers: Mapping[str, int]) -> Callable[..., Any]:
    """`subprocess.run` for the bootstrap's docker calls: `compose port`, `compose ps -q` and `inspect` of a service."""

    def run(argv: Sequence[str], **_: Any) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        if argv[:2] == ["docker", "compose"] and "port" in argv:
            found = published.get((argv[-2], int(argv[-1])))
            return subprocess.CompletedProcess(argv, 0 if found else 1, f"0.0.0.0:{found}\n" if found else "", "")
        if argv[:2] == ["docker", "compose"] and "ps" in argv:
            return subprocess.CompletedProcess(argv, 0, f"cid-{argv[-1]}\n" if argv[-1] in workers else "", "")
        if argv[:2] == ["docker", "inspect"]:
            service = argv[-1].removeprefix("cid-")
            env = ["PATH=/usr/bin", "NOEMA_METRICS_PORT=9464", f"WEB_CONCURRENCY={workers[service]}"]
            return subprocess.CompletedProcess(argv, 0, json.dumps(env) + "\n", "")
        raise AssertionError(f"unexpected command {argv}")

    return run


def discover(monkeypatch: pytest.MonkeyPatch, published: Mapping[tuple[str, int], int], **workers: int) -> Any:
    monkeypatch.setattr(subprocess, "run", fake_docker({**BASE_PORTS, **published}, workers))
    return bootstrap_dev.discover("noema-test", env="dev", stack_db_host="postgres:5432")


def worker_ports(n: int) -> dict[tuple[str, int], int]:
    return {(f"api-{r}", 9464 + w): 31000 + 10 * r + w for r in (1, 2) for w in range(n)}


def test_the_bootstrap_records_every_worker_port_of_each_replica(monkeypatch: pytest.MonkeyPatch) -> None:
    target = discover(monkeypatch, worker_ports(2), **{"api-1": 2, "api-2": 2})
    assert target.metrics == (
        ("http://127.0.0.1:31010", "http://127.0.0.1:31011"),
        ("http://127.0.0.1:31020", "http://127.0.0.1:31021"),
    )


def test_one_worker_per_pod_records_one_endpoint_per_replica(monkeypatch: pytest.MonkeyPatch) -> None:
    target = discover(monkeypatch, worker_ports(2), **{"api-1": 1, "api-2": 1})
    assert target.metrics == (("http://127.0.0.1:31010",), ("http://127.0.0.1:31020",))


def test_a_worker_without_a_published_metrics_port_is_a_bootstrap_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(bootstrap_dev.BootstrapError, match="api-1 runs 3 api workers"):
        discover(monkeypatch, worker_ports(2), **{"api-1": 3, "api-2": 2})


class _Metrics(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = cast(Any, self.server).text.encode()
        self.send_response(200)
        self.send_header("content-type", "text/plain; version=0.0.4")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        pass


@pytest.fixture
def worker_servers() -> Iterator[dict[tuple[str, int], int]]:
    """One /metrics server per (replica, worker): api-1 counts 1 and 2 mismatches, api-2 counts 4 and 8."""
    servers: dict[tuple[str, int], ThreadingHTTPServer] = {}
    for (replica, worker), value in {("api-1", 0): 1, ("api-1", 1): 2, ("api-2", 0): 4, ("api-2", 1): 8}.items():
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Metrics)
        cast(Any, server).text = (
            f'# TYPE {COUNTER} counter\n{COUNTER}{{kind="pubsub_tenant_mismatch"}} {value}.0\n'
            f'{COUNTER}{{kind="other"}} 100.0\n'
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers[(replica, 9464 + worker)] = server
    try:
        yield {key: server.server_address[1] for key, server in servers.items()}
    finally:
        for server in servers.values():
            server.shutdown()
            server.server_close()


async def test_counter_deltas_sum_every_worker_of_both_replicas(
    monkeypatch: pytest.MonkeyPatch, worker_servers: dict[tuple[str, int], int]
) -> None:
    target = discover(monkeypatch, worker_servers, **{"api-1": 2, "api-2": 2})
    monkeypatch.undo()  # the helpers below talk to the servers, not to docker
    stack = cast(Stack, SimpleNamespace(state={"endpoints": bootstrap_dev.endpoints(target)}))
    first = await metric_samples(stack, 0, COUNTER)
    assert [v for labels, v in first if labels == {"kind": "pubsub_tenant_mismatch"}] == [1.0, 2.0]
    assert await metric_total(stack, COUNTER, kind="pubsub_tenant_mismatch") == 15.0
