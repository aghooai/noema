"""Sandbox building blocks (ADR-002 §5-§7): the out/ ownership walk (SEC-SBX-04 planted entries), the backend/env
matrix (SEC-SBX-07), the IPC schema, per-kind params, facts and limit clamping."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from noema.core.errors import ForbiddenInEnvironment
from noema.core.protocols.sandbox import SandboxLimits
from noema.media.sandbox import spool
from noema.media.sandbox.backends import BACKEND_ENVS, assert_backend_allowed
from noema.media.sandbox.base import local_job_id, raw_status, signal_of
from noema.media.sandbox.child import ChildOutcome
from noema.media.sandbox.ipc import (
    BadRequest,
    JobResponse,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)
from noema.media.sandbox.limits import KIND_LIMITS, BadParams, check_facts, check_params, clamp

pytestmark = pytest.mark.unit
JOB = "sbx_01HZY3M4N5P6Q7R8S9T0V1W2X3"


def test_reclaim_out_deletes_planted_entries_and_never_follows_links(tmp_path: Path) -> None:
    jd = spool.prepare(tmp_path, JOB, b"input")
    out = jd / "out"
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    os.chmod(victim, 0o600)
    (out / "x.png").symlink_to(victim)
    os.link(victim, out / "hard.png")
    os.mkfifo(out / "fifo.png")
    (out / "sub").mkdir()
    (out / "sub" / "inner.png").write_bytes(b"x")
    (out / "ok.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    kept = spool.reclaim_out(out, uid=None, gid=None)
    assert kept == ["ok.png"]
    assert sorted(p.name for p in out.iterdir()) == ["ok.png"]
    assert stat.S_IMODE(victim.stat().st_mode) == 0o600 and victim.read_text() == "keep"  # target untouched
    assert stat.S_IMODE((out / "ok.png").stat().st_mode) == 0o640
    assert stat.S_IMODE(out.stat().st_mode) == 0o750


def test_spool_layout_and_orphan_sweep(tmp_path: Path) -> None:
    jd = spool.prepare(tmp_path, JOB, b"abc")
    assert stat.S_IMODE(jd.stat().st_mode) == 0o711
    assert stat.S_IMODE((jd / "in").stat().st_mode) == 0o755
    assert stat.S_IMODE((jd / "in" / "input.bin").stat().st_mode) == 0o644
    assert stat.S_IMODE((jd / "out").stat().st_mode) == 0o700
    (tmp_path / "not-a-job").mkdir()
    assert spool.sweep_orphans(tmp_path, older_than_s=3600, now=jd.stat().st_mtime + 10) == 0
    assert spool.sweep_orphans(tmp_path, older_than_s=3600, now=jd.stat().st_mtime + 4000) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["not-a-job"]
    with pytest.raises(ValueError, match="invalid sandbox job id"):
        spool.job_dir(tmp_path, "../etc")


@pytest.mark.parametrize("env", ["test", "dev", "staging", "prod"])
@pytest.mark.parametrize("backend", ["remote", "nsjail", "bwrap", "inproc_rlimit"])
def test_backend_env_matrix(backend: str, env: str) -> None:
    allowed = env in BACKEND_ENVS[backend]  # type: ignore[index]
    if allowed:
        assert_backend_allowed(backend, env)  # type: ignore[arg-type]
    else:
        with pytest.raises(ForbiddenInEnvironment):
            assert_backend_allowed(backend, env)  # type: ignore[arg-type]
    expected = {"remote": True, "nsjail": True, "bwrap": env in ("test", "dev"), "inproc_rlimit": env == "test"}
    assert allowed is expected[backend]
    worker_ok = allowed and (backend == "remote" or env in ("test", "dev"))
    if worker_ok:
        assert_backend_allowed(backend, env, process="media-worker")  # type: ignore[arg-type]
    else:
        with pytest.raises(ForbiddenInEnvironment):
            assert_backend_allowed(backend, env, process="media-worker")  # type: ignore[arg-type]


def test_runners_refuse_disallowed_environments(tmp_path: Path) -> None:
    from noema.media.sandbox.bwrap import BwrapRunner
    from noema.media.sandbox.inproc import InprocRlimitRunner

    for env in ("dev", "staging", "prod"):
        with pytest.raises(ForbiddenInEnvironment):
            InprocRlimitRunner(env, spool_root=tmp_path)
    for env in ("staging", "prod"):
        with pytest.raises(ForbiddenInEnvironment):
            BwrapRunner(env, spool_root=tmp_path)


def test_ipc_round_trip_and_schema() -> None:
    limits = clamp("pdf.render")
    raw = encode_request(JOB, "pdf.render", {"long_edge_px": 1568}, limits)
    req = decode_request(raw.rstrip(b"\n"))
    assert req.kind == "pdf.render" and req.limits == limits and req.params == {"long_edge_px": 1568}
    doc = json.loads(raw)
    for mutate in (
        {"job_id": "sbx_../../x"},
        {"kind": "shell"},
        {"input": "../input.bin"},
        {"params": {"long_edge_px": 1568, "exec": 1}},
        {"extra": 1},
        {"limits": {**doc["limits"], "nproc": -1}},
    ):
        with pytest.raises(BadRequest):
            decode_request(json.dumps({**doc, **mutate}).encode())
    with pytest.raises(BadRequest):
        decode_request(b"x" * (64 * 1024 + 1))
    for deep in (b"[" * 60_000, b'{"a":' * 12_000):  # under 64 KiB, deeper than the parser's stack
        with pytest.raises(BadRequest):
            decode_request(deep)
        with pytest.raises(BadRequest):
            decode_response(deep, job_id=JOB)
    resp = JobResponse(JOB, "ok", 0, None, 12, "out/manifest.json", "e" * 5000)
    back = decode_response(encode_response(resp).rstrip(b"\n"), job_id=JOB)
    assert back.status == "ok" and len(back.stderr_tail.encode()) <= 2048
    with pytest.raises(BadRequest):
        decode_response(encode_response(resp), job_id="sbx_01HZY3M4N5P6Q7R8S9T0V1W2X4")


def test_params_are_closed_per_kind() -> None:
    assert check_params("ocr", {}) == {"pages": None}
    assert check_params("ocr", {"pages": [1, 3]}) == {"pages": [1, 3]}
    assert check_params("pdf.render", {}) == {"long_edge_px": 1568}
    for kind, params in (
        ("detect", {"x": 1}),
        ("image.normalize", {}),
        ("image.normalize", {"format": "BMP"}),
        ("ocr", {"pages": [3, 1]}),
        ("ocr", {"pages": [0]}),
        ("pdf.render", {"long_edge_px": True}),
    ):
        with pytest.raises(BadParams):
            check_params(kind, params)  # type: ignore[arg-type]


def test_limits_are_clamped_to_the_table() -> None:
    huge = SandboxLimits(wall_s=1e6, cpu_s=10**6, mem_mib=10**6, fsize_mib=10**6, nproc=10**6, max_outputs=10**6)
    for kind, top in KIND_LIMITS.items():
        clamped = clamp(kind, huge)
        assert clamped == (top if top.mem_mib is not None else replace(top, mem_mib=huge.mem_mib))
    low = SandboxLimits(wall_s=1, cpu_s=1, mem_mib=64, fsize_mib=1, nproc=1, max_outputs=0)
    assert clamp("detect", low) == low
    assert clamp("pdf.markdown", huge).mem_mib == 10**6  # unbounded kind: the container mem_limit bounds it


def test_facts_schema() -> None:
    assert check_facts("pdf.sanitize", {"pages": 2, "encrypted": False, "active_content": ["javascript"]}) is None
    assert check_facts("pdf.sanitize", {"pages": 2, "encrypted": True, "active_content": []}) is not None
    assert check_facts("pdf.render", {"pages": 2, "text_layer_pages": [2, 1]}) is not None
    assert check_facts("pdf.render", {"pages": 2, "text_layer_pages": [3]}) is not None
    assert check_facts("ocr", {"x": 1}) is not None
    assert check_facts("image.normalize", {}) is None


def test_exit_status_mapping() -> None:
    lim = clamp("detect")

    def status(rc: int, *, wall_ms: int = 10, timed_out: bool = False, wrapped: bool = True) -> str:
        return raw_status(ChildOutcome(rc, timed_out, wall_ms, ""), lim, wrapped=wrapped)

    assert status(0) == "ok"
    assert status(159) == "violation" and status(-31, wrapped=False) == "violation"
    assert status(137, wall_ms=15_000) == "timeout" and status(137, wall_ms=100) == "failed"  # time limit vs OOM
    assert status(152) == "timeout"  # SIGXCPU
    assert status(1) == "failed" and status(2) == "bad_request"
    assert status(0, timed_out=True) == "timeout"
    assert signal_of(-9, wrapped=False) == 9 and signal_of(1, wrapped=True) is None


def test_local_job_ids_are_valid_and_stable() -> None:
    assert local_job_id(JOB) == JOB
    derived = local_job_id("sbx_detect")
    assert spool.JOB_ID_RE.fullmatch(derived) and derived == local_job_id("sbx_detect") != local_job_id("other")
