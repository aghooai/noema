"""Shared fixtures of the media-pipeline unit tests: the InprocRlimitRunner (NOEMA_ENV=test only) over a temporary
spool, and `extract()` to run one sandbox kind over bytes."""

from __future__ import annotations

import itertools
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

import pytest

from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxKind, SandboxLimits
from noema.core.values import JsonValue
from noema.media.sandbox.inproc import InprocRlimitRunner
from noema.media.sandbox.limits import KIND_LIMITS

Extract = Callable[..., Awaitable[ExtractResult]]
_ids = itertools.count(1)


@pytest.fixture
def runner(tmp_path: Path) -> InprocRlimitRunner:
    return InprocRlimitRunner("test", spool_root=tmp_path / "spool")


@pytest.fixture
def extract(runner: InprocRlimitRunner, tmp_path: Path) -> Extract:
    async def run(
        kind: SandboxKind,
        data: bytes,
        params: Mapping[str, JsonValue] | None = None,
        limits: SandboxLimits | None = None,
    ) -> ExtractResult:
        n = next(_ids)
        src = tmp_path / f"src-{n}.bin"
        src.write_bytes(data)
        return await runner.run(SandboxJob(f"sbx_test_{n}", kind, src, dict(params or {})), limits or KIND_LIMITS[kind])

    return run
