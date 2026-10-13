"""Sprint B F5: `tests.load.run --api-image` runs a pod of several api processes (WEB_CONCURRENCY) in the container, as
the venv mode does with `--api-procs N`: one listening socket shared by N `tests.load.serve` processes, each with its
own lag and GC files, and SIGTERM passed on to every one so they drain and flush. No docker, no stack:
`pytest tests/load/test_run_api_image.py` (tests/load is never collected by make check)."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import orjson
import pytest
from tests.load import run

pytestmark = [pytest.mark.unit]

# Stands in for tests.load.serve inside the pod launcher: records its argv and the inherited socket, then serves
# until SIGTERM (recorded too).
FAKE_SERVE = """
import json, os, signal, socket, sys, time
from pathlib import Path
args = sys.argv[1:]
sock = socket.socket(fileno=int(args[args.index("--fd") + 1]))
record = Path(os.environ["FAKE_SERVE_OUT"]) / f"{os.getpid()}.json"
stopped = []
signal.signal(signal.SIGTERM, lambda *_: stopped.append(1))
listening = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
record.write_text(json.dumps({"argv": args, "port": sock.getsockname()[1], "listening": listening}))
while not stopped:
    time.sleep(0.05)
record.with_suffix(".stopped").write_text("SIGTERM")
"""


def test_api_image_accepts_several_processes(tmp_path: Path) -> None:
    out = tmp_path / "run"
    out.mkdir()
    (out / "steps.json").write_text("[]")
    (out / "samples.jsonl").write_text("")
    argv = ["--state", str(tmp_path / "state.json"), "--out", str(out), "--analyze-only"]
    assert run.main([*argv, "--api-image", "noema-api:test", "--api-procs", "2"]) == 0
    assert orjson.loads((out / "results.json").read_bytes())["config"]["api_procs"] == "2"


def test_the_pod_launcher_shares_one_socket_and_forwards_sigterm(tmp_path: Path) -> None:
    fake = tmp_path / "pod" / "tests" / "load"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("")
    (fake / "serve.py").write_text(FAKE_SERVE)
    records = tmp_path / "records"
    records.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"} | {"FAKE_SERVE_OUT": str(records)}
    argv = [sys.executable, "-c", run.POD_LAUNCHER, "0", "2", "/out", "--dispatch-yields", "3"]
    pod = subprocess.Popen(argv, cwd=tmp_path / "pod", env=env)
    try:
        deadline = time.monotonic() + 20
        while len(list(records.glob("*.json"))) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        seen = [orjson.loads(p.read_bytes()) for p in sorted(records.glob("*.json"))]
        assert len(seen) == 2, seen
        assert len({s["port"] for s in seen}) == 1 and all(s["listening"] for s in seen)
        lag = sorted(s["argv"][s["argv"].index("--lag-out") + 1] for s in seen)
        gc = sorted(s["argv"][s["argv"].index("--gc-out") + 1] for s in seen)
        assert lag == ["/out/api-0.lag", "/out/api-1.lag"] and gc == ["/out/api-0.gc", "/out/api-1.gc"]
        assert all(s["argv"][-2:] == ["--dispatch-yields", "3"] for s in seen)
        pod.send_signal(signal.SIGTERM)
        assert pod.wait(timeout=20) == 0
        assert len(list(records.glob("*.stopped"))) == 2
    finally:
        if pod.poll() is None:
            pod.kill()


def test_the_container_pod_gets_one_sse_probe_file_per_process(tmp_path: Path) -> None:
    """C3 in the container (release gate): `--sse-probe` with `--api-image` reaches every pod process, each with its
    own probe file in the mounted out directory (the launcher fills in `{i}`), as the venv mode's `api-<i>`."""
    probe = argparse.Namespace(dispatch_yields=None, sse_probe=True)
    assert run.container_serve_args(probe, 1) == ["--sse-probe-out", "/out/api.sseprobe.json"]
    extra = run.container_serve_args(probe, 2)
    assert extra == ["--sse-probe-out", "/out/api-{i}.sseprobe.json"]
    assert run.container_serve_args(argparse.Namespace(dispatch_yields=3, sse_probe=False), 2) == [
        "--dispatch-yields",
        "3",
    ]
    fake = tmp_path / "pod" / "tests" / "load"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("")
    (fake / "serve.py").write_text(FAKE_SERVE)
    records = tmp_path / "records"
    records.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"} | {"FAKE_SERVE_OUT": str(records)}
    pod = subprocess.Popen(
        [sys.executable, "-c", run.POD_LAUNCHER, "0", "2", "/out", *extra], cwd=fake.parents[1], env=env
    )
    try:
        deadline = time.monotonic() + 20
        while len(list(records.glob("*.json"))) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        seen = [orjson.loads(p.read_bytes()) for p in sorted(records.glob("*.json"))]
        probes = sorted(s["argv"][s["argv"].index("--sse-probe-out") + 1] for s in seen)
        assert probes == ["/out/api-0.sseprobe.json", "/out/api-1.sseprobe.json"]
        pod.send_signal(signal.SIGTERM)
        assert pod.wait(timeout=20) == 0
    finally:
        if pod.poll() is None:
            pod.kill()
