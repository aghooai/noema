"""SandboxRunner conformance (ADR-002 §7): every job yields an `ExtractResult` (job failures are statuses, never
exceptions) with a known status and the extractor name."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

from noema.core.protocols.sandbox import SandboxJob, SandboxKind, SandboxLimits

from .kits import SandboxKit
from .registry import kit_fixture

kit = kit_fixture("sandbox_runner")
# mem_mib: detect's table value (ADR-002 §5); Magika + onnxruntime need that address space (media-pipeline.md 11)
LIMITS = SandboxLimits(wall_s=30, cpu_s=20, mem_mib=3072, fsize_mib=64, nproc=8, max_outputs=16)


async def test_every_kind_returns_a_result(kit: SandboxKit, tmp_path: Path) -> None:
    src = tmp_path / "in.bin"
    src.write_bytes(b"\x89PNG\r\n\x1a\n")
    for kind in get_args(SandboxKind):
        result = await kit.runner.run(SandboxJob(f"sbx_{kind}", kind, src, {}), LIMITS)
        assert result.status in ("ok", "rejected", "failed", "timeout", "violation")
        assert result.extractor and result.wall_ms >= 0
        assert (result.rejection_code is not None) == (result.status == "rejected")
