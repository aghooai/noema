"""Wave E3 process settings and their wiring (spec-05 §5): NOEMA_REDIS_MAX_CONNECTIONS sizes the process's ONE Redis
`BlockingConnectionPool` (default 256, a 0.5 s wait for a free connection) and NOEMA_TOKEN_ESTIMATOR_TABLE replaces
the TokenEstimator's table with a refit (`estimator_fit`'s JSON; admission-estimator report, not_fixed item 2).
Nothing here opens a connection."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import orjson
import pytest
import redis.asyncio

from noema import composition
from noema.composition import REDIS_POOL_TIMEOUT_S, StartupError, build, process_settings
from noema.core.values import TextBlock

pytestmark = [pytest.mark.unit]
ENVIRON: Final = {"NOEMA_REDIS_URL": "redis://127.0.0.1:1/0"}  # never connected


def test_the_redis_pool_is_bounded_and_blocks_briefly() -> None:
    assert process_settings("test", {}).redis_max_connections == 256
    services = build("test", environ={**ENVIRON, "NOEMA_REDIS_MAX_CONNECTIONS": "32"})
    pool = services.redis.connection_pool
    assert isinstance(pool, redis.asyncio.BlockingConnectionPool)
    assert (pool.max_connections, pool.timeout, REDIS_POOL_TIMEOUT_S) == (32, 0.5, 0.5)
    assert services.redis.auto_close_connection_pool  # stop() closes the pool with the client
    assert pool.connection_kwargs["socket_keepalive"] and pool.connection_kwargs["decode_responses"] is False


@pytest.mark.parametrize("value", ["7", "10001", "many"])
def test_an_invalid_pool_size_is_refused(value: str) -> None:
    with pytest.raises(StartupError):
        process_settings("test", {"NOEMA_REDIS_MAX_CONNECTIONS": value})


def test_the_estimator_table_override_replaces_the_shipped_fit(tmp_path: Path) -> None:
    table = tmp_path / "fit.json"
    table.write_bytes(orjson.dumps({"models": {"sol-6.1": {"chars_per_token": 5.0, "request_overhead_tokens": 3}}}))
    text = (TextBlock("x" * 100),)
    shipped = build("test", environ=ENVIRON).estimator
    refit = build("test", environ={**ENVIRON, "NOEMA_TOKEN_ESTIMATOR_TABLE": str(table)}).estimator
    assert shipped.estimate("sol-6.1", (), text) == 11 + 22  # TOKEN_ESTIMATOR_V1: 4.61 chars per token, 11 overhead
    assert refit.estimate("sol-6.1", (), text) == 3 + 20
    assert refit.estimate("opus-5.5", (), text) == 25  # not in the refit: chars / 4, no overhead


@pytest.mark.parametrize("content", [None, b"{}", b"not json", b'{"models": {"sol-6.1": {"chars_per_token": 0}}}'])
def test_a_missing_or_invalid_table_stops_the_process(tmp_path: Path, content: bytes | None) -> None:
    table = tmp_path / "fit.json"
    if content is not None:
        table.write_bytes(content)
    with pytest.raises(StartupError, match="NOEMA_TOKEN_ESTIMATOR_TABLE"):
        build("test", environ={**ENVIRON, "NOEMA_TOKEN_ESTIMATOR_TABLE": str(table)})


def test_the_table_path_must_be_absolute() -> None:
    assert process_settings("test", {}).token_estimator_table is None
    with pytest.raises(StartupError, match="absolute"):
        process_settings("test", {"NOEMA_TOKEN_ESTIMATOR_TABLE": "fit.json"})


def test_one_datastore_operation_in_sixteen_is_timed() -> None:
    draws = 160_000
    timed = sum(composition._sampled() for _ in range(draws))
    assert composition.DATASTORE_SAMPLE_RATE == 1 / 16
    assert abs(timed / draws - 1 / 16) < 0.003  # about 5 standard deviations of the binomial share
