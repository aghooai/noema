"""Several api processes per pod (E sprint B, uvicorn `--workers` = `WEB_CONCURRENCY`): each worker is its own pod in
the leases, owners and consumer names, the local fallback shares divide by pods x workers, and each worker serves
its metrics on the first free port of its pod's range."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import pytest

from noema.composition import StartupError, api_workers, process_settings, serve_metrics

pytestmark = [pytest.mark.unit]


def test_one_worker_keeps_the_pod_id_and_the_expected_pods() -> None:
    s = process_settings("dev", {"NOEMA_POD_ID": "api-1", "NOEMA_EXPECTED_PODS": "4"})
    assert (s.pod_id, s.expected_pods) == ("api-1", 4)
    assert api_workers({}) == 1


def test_each_worker_of_a_pod_is_its_own_process_identity() -> None:
    s = process_settings("dev", {"NOEMA_POD_ID": "api-1", "NOEMA_EXPECTED_PODS": "4", "WEB_CONCURRENCY": "2"})
    assert s.pod_id == f"api-1-{os.getpid()}"
    assert s.expected_pods == 8  # 4 pods x 2 processes share the Redis-down fallback budgets
    assert process_settings("dev", {"WEB_CONCURRENCY": "2"}).expected_pods == 2


@pytest.mark.parametrize("value", ["0", "65", "two"])
def test_web_concurrency_is_validated(value: str) -> None:
    with pytest.raises(StartupError, match="WEB_CONCURRENCY"):
        process_settings("dev", {"WEB_CONCURRENCY": value})


class _Setup:
    """`TelemetrySetup.serve_prometheus` over a set of ports already taken."""

    def __init__(self, taken: set[int]) -> None:
        self.taken = taken
        self.stopped: list[int] = []

    def serve_prometheus(self, port: int) -> tuple[int, Callable[[], None]]:
        if port in self.taken:
            raise OSError(98, "Address already in use")
        self.taken.add(port)
        return port, lambda: self.stopped.append(port)


def test_workers_take_the_first_free_metrics_port_of_the_pod() -> None:
    taken: set[int] = set()
    first, second = _Setup(taken), _Setup(taken)
    stop_first = serve_metrics(first, 9464, 2)  # type: ignore[arg-type]
    stop_second = serve_metrics(second, 9464, 2)  # type: ignore[arg-type]
    assert taken == {9464, 9465}
    stop_first()
    stop_second()
    assert (first.stopped, second.stopped) == ([9464], [9465])
    with pytest.raises(OSError, match="in use"):
        serve_metrics(_Setup(taken), 9464, 2)  # type: ignore[arg-type]
    one: Any = _Setup(set())
    serve_metrics(one, 9464, 1)
    assert one.taken == {9464}
