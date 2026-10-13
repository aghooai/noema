"""The shared spool (ADR-002 §1, §6; the 4 GiB media-spool tmpfs): every job's outputs fit an aggregate budget
(enforced by the extractor's writer and again by the trusted validator), each job dir is removed as soon as its
outputs are stored, later jobs read hard links of the few kept inputs, and file hashing and reads run off the
event loop."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from tests.contract_internal.kits import principal

from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxLimits
from noema.media.extractors import pdf_render
from noema.media.extractors.common import OutputBudgetExceeded, Outputs
from noema.media.sandbox import base, limits
from noema.media.sandbox.manifest import ManifestInvalid
from noema.media.sandbox.manifest import validate as real_validate

from . import media_fixtures as fx
from .conftest import Extract
from .support import MediaEnv, md_script, media_env
from .test_manifest import check, entry, write

pytestmark = pytest.mark.unit
MIB = 1024 * 1024


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


def test_the_writer_stops_before_the_budget(tmp_path: Path) -> None:
    out = Outputs("pdf.render", tmp_path, "x", budget_bytes=10)
    out.add("page-0001.png", b"123456", kind="page_images", media_type="image/png", page=1)
    with pytest.raises(OutputBudgetExceeded):
        out.add("page-0002.png", b"123456", kind="page_images", media_type="image/png", page=2)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["page-0001.png"] and out.written == 6
    assert Outputs("pdf.render", tmp_path, "x").budget_bytes == limits.OUTPUT_BUDGET_BYTES["pdf.render"] == 512 * MIB


def test_a_shared_image_pdf_render_stops_at_the_budget(tmp_path: Path) -> None:
    src = tmp_path / "in.pdf"
    src.write_bytes(fx.shared_noise_pdf(3))  # a small upload: one ~5 MiB page render per page
    (tmp_path / "out").mkdir()
    out = Outputs("pdf.render", tmp_path / "out", "x", budget_bytes=8 * MIB)
    with pytest.raises(OutputBudgetExceeded):
        pdf_render.run(src, out, {"long_edge_px": 1568})
    written = sum(p.stat().st_size for p in (tmp_path / "out").iterdir())
    assert src.stat().st_size < 1 * MIB and 0 < written <= 8 * MIB and len(out.outputs) < 3


def test_the_validator_refuses_outputs_over_the_budget(tmp_path: Path) -> None:
    png = fx.png(8, 8)
    outputs = []
    for name, kind in (("raster.png", "raster"), ("raster_1568.png", "raster_1568"), ("thumbnail.png", "thumbnail")):
        (tmp_path / name).write_bytes(png)
        outputs.append(entry(name, png, kind=kind, width=8, height=8))
    write(tmp_path, outputs)
    assert check(tmp_path).status == "ok"
    write(tmp_path, outputs)
    with pytest.raises(ManifestInvalid, match="output budget"):
        real_validate(
            "image.normalize", tmp_path, max_outputs=3, output_cap_bytes=MIB, wall_ms=1, max_total_bytes=len(png)
        )
    assert sorted(p.name for p in tmp_path.iterdir()) == ["manifest.json"]  # nothing of an invalid job survives


async def test_a_render_over_budget_fails_page_images_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(limits.OUTPUT_BUDGET_BYTES, "pdf.render", 8 * MIB)
    env = media_env(tmp_path)
    env.runner.script["pdf.markdown"] = md_script(["a", "b", "c"])
    ref = await env.files.upload(
        principal(), chunks(fx.shared_noise_pdf(3)), declared_type="application/pdf", filename="a.pdf", session_id=None
    )
    await env.drain()
    done = await env.files.get(principal(), ref.file_id)
    states = {d.kind: d.status for d in done.derivatives}
    assert done.status == "partial" and states["page_images"] == "failed"
    assert states["markdown"] == states["pdf_sanitized"] == "ready" and "ocr" not in env.runner.kinds()
    assert not [k for k in env.objects() if "/page_images/" in k]
    assert list((tmp_path / "spool").iterdir()) == []


def spool_watch(env: MediaEnv, spool_root: Path) -> list[tuple[str, list[str], list[str], int]]:
    """Per sandbox job: (kind, the spool dirs, the keep dir's files, the input's link count) at its start."""
    seen: list[tuple[str, list[str], list[str], int]] = []
    real_run = env.runner.run

    def snapshot(job: SandboxJob) -> None:
        dirs = sorted(p.name for p in spool_root.iterdir())
        keep = [d for d in dirs if d != job.job_id]
        kept = sorted(p.name for p in (spool_root / keep[0]).iterdir()) if len(keep) == 1 else []
        seen.append((job.kind, dirs, kept, job.input_path.stat().st_nlink))

    async def run(job: SandboxJob, limits: SandboxLimits) -> ExtractResult:
        snapshot(job)
        return await real_run(job, limits)

    env.runner.run = run  # type: ignore[method-assign]
    return seen


async def test_each_job_dir_goes_once_stored_and_inputs_are_linked(tmp_path: Path) -> None:
    env = media_env(tmp_path)
    spool_root = tmp_path / "spool"
    seen = spool_watch(env, spool_root)
    await env.files.upload(principal(), chunks(fx.png()), declared_type="image/png", filename="a.png", session_id=None)
    await env.drain()
    assert [(kind, len(dirs), kept, links) for kind, dirs, kept, links in seen] == [
        ("detect", 2, ["original"], 2),  # the keep dir and this job's dir only
        ("image.normalize", 2, ["original"], 2),
        ("ocr", 2, ["raster"], 2),  # the original was released after its last reader
    ]
    assert list(spool_root.iterdir()) == []


async def test_validation_and_output_reads_run_in_worker_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extract: Extract
) -> None:
    threads: list[bool] = []
    real_read = Path.read_bytes

    def validate(*args: Any, **kwargs: Any) -> ExtractResult:
        threads.append(threading.current_thread() is threading.main_thread())
        return real_validate(*args, **kwargs)

    def read_bytes(self: Path) -> bytes:
        if self.parent.name == "out":
            threads.append(threading.current_thread() is threading.main_thread())
        return real_read(self)

    monkeypatch.setattr(base, "validate", validate)
    assert (await extract("markdown.sanitize", b"# Title\n\nbody\n")).status == "ok"
    monkeypatch.setattr(base, "validate", real_validate)  # the test double below validates in-process
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    env = media_env(tmp_path)
    await env.files.upload(principal(), chunks(fx.png()), declared_type="image/png", filename="a.png", session_id=None)
    await env.drain()
    assert len(threads) >= 4 and not any(threads)  # 1 validation + the raster, raster_1568, thumbnail, ocr reads
