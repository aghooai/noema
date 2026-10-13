"""Load runner (spec-10 §5): one api process against tools/mockbedrock with scripted latencies, a stepped plan of
concurrent streams, and the measurements of every step.

    python -m tests.load.run --state ~/.noema/stacks/noema-perf.json --out reports/perf/c2b/<run> \
        --plan 50:300,100:300,200:900

Processes and placement (spec-10 §5 roles and sizes; taskset; the defaults keep clear of the low cores other
agents' processes use most):
- the stack's Postgres and Valkey containers: `docker update --cpuset-cpus` to `--stack-cpus` (40-45);
- the mock: four `tests.load.mockserve` processes (32-33, 34-35, 36-37, 38-39), two Converse on `--mock-port` and
  two Mantle on the next port, sharing each port with SO_REUSEPORT (the kernel spreads the api's connections);
- the api: `tests.load.serve` (uvicorn + uvloop + httptools, the image CMD) on cores 46-47 = one 2 vCPU pod; with
  `--api-image` the same from the image (`docker run --cpuset-cpus`, the compose app settings, tests/load mounted;
  `--api-procs N` runs the N processes in the container on one socket, POD_LAUNCHER);
- drivers: `tests.load.driver`, 50 streams per process, on `--driver-cpus` (20-31); a step adds processes up to its
  target. The drivers are closed-loop (spec-10 §5: N streams that loop turns), so past the knee the latencies are
  queueing at that concurrency, not the response to an offered arrival rate.

`results.json` also carries `provenance` (spec-10 §5): HEAD plus a digest of the uncommitted code paths, the active
config version and digest, the scenario digest, the api's interpreter (or image id), the placement and the load.

`--plan streams:hold_s,...`: each step runs `--settle-s` (the first step `--warmup-s`, 60) and then its measured
window of hold_s seconds; turns are assigned to a window by their send time. Every 5 s the runner samples the
api's RSS/HWM, CPU, threads and fds, the mock and driver CPU and /proc/loadavg (`samples.jsonl`); `/metrics` is
scraped at the end of each step. Results: `results.json` in `--out` (the step windows analyzed by
tests/load/stats.py) and a printed summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final

import anyio
import orjson
from tests.load import stats

STREAMS_PER_DRIVER: Final = 50
SAMPLE_EVERY_S: Final = 5.0
REPO: Final = Path(__file__).resolve().parents[2]
CODE_PATHS: Final = ("src", "tests", "tools", "deploy", "pyproject.toml", "uv.lock")
STACK_PINNED: Final = ("postgres", "valkey")
API_CONTAINER_OUT: Final = "/out"
CLK_TCK: Final = os.sysconf("SC_CLK_TCK")
# `--api-image --api-procs N` (N > 1), run in the container as `python -c POD_LAUNCHER <port> <n> <out> [serve args]`:
# one listening socket shared by N `tests.load.serve` processes, as uvicorn's supervisor shares it with its
# WEB_CONCURRENCY workers; SIGTERM/SIGINT (from docker through the container's init) go to every process.
POD_LAUNCHER: Final = """
import signal, socket, subprocess, sys
port, n, out, *extra = sys.argv[1:]
sock = socket.create_server(("127.0.0.1", int(port)), backlog=2048)
fd = str(sock.fileno())
procs = [
    subprocess.Popen(
        [sys.executable, "-m", "tests.load.serve", "--port", port, "--fd", fd,
         "--lag-out", f"{out}/api-{i}.lag", "--gc-out", f"{out}/api-{i}.gc",
         *(a.replace("{i}", str(i)) for a in extra)],
        pass_fds=(sock.fileno(),),
    )
    for i in range(int(n))
]
sock.close()


def forward(sig, _frame):
    for proc in procs:
        proc.send_signal(sig)


for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, forward)
sys.exit(max(proc.wait() for proc in procs))
"""


@dataclass
class Step:
    streams: int
    hold_s: float
    t_start: int = 0
    t_end: int = 0


def parse_plan(text: str) -> list[Step]:
    steps = []
    for part in text.split(","):
        streams, hold = part.split(":")
        steps.append(Step(int(streams), float(hold)))
    return steps


def proc_cpu_s(pid: int) -> float:
    """utime + stime of a process (all threads), seconds."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / CLK_TCK


def proc_status(pid: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("VmRSS", "VmHWM", "Threads"):
            out[key] = int(value.split()[0])
    try:
        out["fds"] = len(os.listdir(f"/proc/{pid}/fd"))
    except PermissionError:  # the api container's uid 10001: its fd table is not ours to read
        out["fds"] = -1
    return out


def loadavg() -> list[float]:
    return [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]]


@dataclass
class Procs:
    out: Path
    logs: list[IO[bytes]] = field(default_factory=list)
    mocks: list[subprocess.Popen[bytes]] = field(default_factory=list)
    api: subprocess.Popen[bytes] | None = None
    apis: list[subprocess.Popen[bytes]] = field(default_factory=list)  # --api-procs N: every api process
    drivers: list[subprocess.Popen[bytes]] = field(default_factory=list)
    sides: list[subprocess.Popen[bytes]] = field(default_factory=list)  # --side groups (flood drivers, C3 clients)
    streams: int = 0  # streams of the drivers started so far
    api_pid: int | None = None  # the api's python process (the container's, with --api-image)
    api_pids: list[int] = field(default_factory=list)  # every api process (one pod: summed by the sampler)
    container: str | None = None  # --api-image: the api container's name

    def spawn(self, name: str, cpus: str, argv: Sequence[str], env: Mapping[str, str]) -> subprocess.Popen[bytes]:
        return self.spawn_cmd(name, ["taskset", "-c", cpus, sys.executable, *argv], env)

    def spawn_cmd(
        self, name: str, cmd: Sequence[str], env: Mapping[str, str], pass_fds: Sequence[int] = ()
    ) -> subprocess.Popen[bytes]:
        log = (self.out / f"{name}.log").open("wb")
        self.logs.append(log)
        return subprocess.Popen(
            list(cmd), cwd=REPO, env=dict(env), stdout=log, stderr=subprocess.STDOUT, pass_fds=tuple(pass_fds)
        )

    def stop_all(self) -> None:
        for group in (self.drivers, self.sides, self.apis or ([self.api] if self.api else []), self.mocks):
            for proc in group:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
            for proc in group:
                try:
                    proc.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self.container is not None:  # gone already after a clean stop (--rm); a killed client leaves it
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, check=False)
        for log in self.logs:
            log.close()


def wait_ready(url: str, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} not ready after {timeout_s:.0f} s")


def scrape(url: str, path: Path) -> None:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            path.write_bytes(response.read())
    except OSError as exc:
        path.write_text(f"scrape failed: {exc}\n")


def api_env(state: Mapping[str, Any], args: argparse.Namespace) -> dict[str, str]:
    """The stack's host env (tools/bootstrap_dev.py) with the bedrock endpoints on our mocks."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NOEMA_", "AWS_"))}
    env.update({k: str(v) for k, v in state["host_env"].items()})
    overrides = json.loads(env["NOEMA_AWS_ENDPOINT_OVERRIDES"])
    overrides["bedrock-runtime"] = f"http://127.0.0.1:{args.mock_port}"
    overrides["mantle"] = f"http://127.0.0.1:{args.mock_port + 1}/v1"
    env["NOEMA_AWS_ENDPOINT_OVERRIDES"] = json.dumps(overrides)
    env["NOEMA_METRICS_PORT"] = str(args.api_port + 1)
    env["NOEMA_POD_ID"] = "perf-api-1"
    env.update(item.split("=", 1) for item in args.api_env)  # --api-env K=V (e.g. NOEMA_MAX_TURNS_PER_PROCESS=0)
    return env


def git(*argv: str) -> bytes:
    return subprocess.run(["git", *argv], cwd=REPO, capture_output=True, check=True).stdout


def code_version() -> dict[str, Any]:
    """spec-10 §5 "commit": HEAD plus a digest of the uncommitted state of the code paths (tracked diff and
    untracked files), so a run on a dirty working tree still names the exact code it measured."""
    head = git("rev-parse", "HEAD").decode().strip()
    status = git("status", "--porcelain", "--untracked-files=all", "--", *CODE_PATHS).decode().splitlines()
    digest = hashlib.sha256(git("diff", "--binary", "HEAD", "--", *CODE_PATHS))
    for line in sorted(status):
        path = REPO / line[3:]
        if line.startswith("??") and path.is_file():
            digest.update(line[3:].encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return {
        "git_head": head,
        "paths": list(CODE_PATHS),
        "changed_paths": len(status),
        "worktree_digest": f"sha256:{digest.hexdigest()}",
    }


async def active_config(state: Mapping[str, Any]) -> dict[str, Any]:
    """spec-10 §5 "config hash": the stack's active config version and digest (noema-admin config list)."""
    from tests.load.stack import target_of
    from tools.bootstrap_dev import admin

    key = state["operators"]["noema-bootstrap-a"]["key"]
    listed = (await admin(target_of(state), ["config", "list"], key)).get("data", [])
    active = [v for v in listed if v.get("status") == "active"]
    return {"version": active[0]["version"], "digest": active[0]["digest"]} if active else {}


def container_cpuset(name: str) -> str:
    return subprocess.run(
        ["docker", "inspect", "-f", "{{.HostConfig.CpusetCpus}}", name], capture_output=True, text=True, check=True
    ).stdout.strip()


def pin_stack(project: str, cpus: str) -> dict[str, str]:
    """spec-10 §5 placement: the stack's Postgres and Valkey containers on `cpus` (docker update)."""
    names = [f"{project}-{service}-1" for service in STACK_PINNED]
    if cpus:
        subprocess.run(["docker", "update", "--cpuset-cpus", cpus, *names], capture_output=True, check=True)
    return {name: container_cpuset(name) for name in names}


def image_python(image: str) -> str:
    env = subprocess.run(
        ["docker", "image", "inspect", "-f", "{{json .Config.Env}}", image], capture_output=True, check=True
    ).stdout
    found = [e.split("=", 1)[1] for e in orjson.loads(env) if e.startswith("PYTHON_VERSION=")]
    return found[0] if found else "unknown"


def image_id(image: str) -> str:
    return subprocess.run(
        ["docker", "image", "inspect", "-f", "{{.Id}}", image], capture_output=True, text=True, check=True
    ).stdout.strip()


class Sampler:
    """One line per SAMPLE_EVERY_S: api memory/CPU/fds, mock and driver CPU, host load."""

    def __init__(self, procs: Procs, path: Path) -> None:
        self.procs = procs
        self.out = path.open("wb")
        self.next_at = 0.0

    def tick(self) -> None:
        now = time.monotonic()
        if now < self.next_at:
            return
        self.next_at = now + SAMPLE_EVERY_S
        row: dict[str, Any] = {"t_ns": time.monotonic_ns(), "loadavg": loadavg()}
        api, pid = self.procs.api, self.procs.api_pid
        pids = self.procs.api_pids or ([pid] if pid is not None else [])
        if api is not None and api.poll() is None and pids:
            rows = [{"cpu_s": proc_cpu_s(p), **proc_status(p)} for p in pids]
            row["api"] = {k: sum(r[k] for r in rows) for k in rows[0]}  # one pod: CPU, RSS, fds summed
            row["api_procs"] = len(rows)
        row["mock_cpu_s"] = [proc_cpu_s(p.pid) for p in self.procs.mocks if p.poll() is None]
        row["driver_cpu_s"] = sum(proc_cpu_s(p.pid) for p in self.procs.drivers if p.poll() is None)
        row["drivers"] = sum(1 for p in self.procs.drivers if p.poll() is None)
        self.out.write(orjson.dumps(row) + b"\n")
        self.out.flush()

    def sleep_until(self, t_ns: int) -> None:
        while time.monotonic_ns() < t_ns:
            self.tick()
            time.sleep(min(0.5, max(0.0, (t_ns - time.monotonic_ns()) / 1e9)))


def start_drivers(procs: Procs, args: argparse.Namespace, target: int, end_ns: int, key_file: Path) -> None:
    running = procs.streams
    while running < target:
        n = min(STREAMS_PER_DRIVER, target - running)
        i = len(procs.drivers)
        argv = ["-m", "tests.load.driver", "--base-url", f"http://127.0.0.1:{args.api_port}"]
        argv += ["--key-file", str(key_file), "--streams", str(n), "--stream-base", str(running)]
        argv += ["--duration-s", f"{(end_ns - time.monotonic_ns()) / 1e9:.1f}", "--stagger-s", "5"]
        argv += ["--turns-per-session", str(args.turns_per_session)]
        argv += ["--out", str(args.out / f"driver-{i}.jsonl"), "--lag-out", str(args.out / f"driver-{i}.lag")]
        procs.drivers.append(procs.spawn(f"driver-{i}", args.driver_cpus, argv, os.environ))
        running += n
    procs.streams = running


@dataclass(frozen=True)
class Side:
    """`--side`: a client group that runs during one step only (settle + window), next to the main drivers.
    `flood:<tenant>:<streams>:<step>`: driver streams with that tenant's developer key, group "flood" (C5);
    `slow:<clients>:<step>[:<rate_bps>]` / `fast:<clients>:<step>`: tests/load/slow.py heavy-stream clients (C3)."""

    kind: str
    count: int
    step: int
    tenant: str = "t_acme"
    rate_bps: float = 0.0


def parse_side(text: str) -> Side:
    parts = text.split(":")
    if parts[0] == "flood" and len(parts) == 4:
        return Side("flood", int(parts[2]), int(parts[3]), tenant=parts[1])
    if parts[0] in ("slow", "fast") and len(parts) in (3, 4):
        rate = float(parts[3]) if len(parts) == 4 else (12_600.0 if parts[0] == "slow" else 0.0)
        return Side(parts[0], int(parts[1]), int(parts[2]), rate_bps=rate if parts[0] == "slow" else 0.0)
    raise ValueError(f"--side {text!r}: flood:<tenant>:<streams>:<step> | slow|fast:<clients>:<step>[:<rate_bps>]")


def side_key(args: argparse.Namespace, state: Mapping[str, Any], tenant: str) -> Path:
    path = Path(args.out) / f"key-{tenant}"
    path.write_text(state["tenants"][tenant]["projects"]["main"]["principals"]["developer"]["key"])
    path.chmod(0o600)
    return path


def start_sides(procs: Procs, args: argparse.Namespace, state: Mapping[str, Any], step: int, end_ns: int) -> None:
    duration = f"{(end_ns - time.monotonic_ns()) / 1e9:.1f}"
    for i, side in enumerate(s for s in args.sides if s.step == step):
        key = side_key(args, state, side.tenant)
        name = f"side{step}-{side.kind}-{i}"
        if side.kind == "flood":
            for k in range(0, side.count, STREAMS_PER_DRIVER):
                argv = ["-m", "tests.load.driver", "--base-url", f"http://127.0.0.1:{args.api_port}"]
                argv += ["--key-file", str(key), "--streams", str(min(STREAMS_PER_DRIVER, side.count - k))]
                argv += ["--stream-base", str(100_000 * (step + 1) + k), "--duration-s", duration, "--group", "flood"]
                argv += ["--turns-per-session", str(args.turns_per_session), "--stagger-s", "5"]
                argv += ["--out", str(args.out / f"{name}-{k}.jsonl"), "--lag-out", str(args.out / f"{name}-{k}.lag")]
                procs.sides.append(procs.spawn(f"{name}-{k}", args.driver_cpus, argv, os.environ))
            continue
        argv = ["-m", "tests.load.slow", "--port", str(args.api_port), "--key-file", str(key)]
        argv += ["--clients", str(side.count), "--rate-bps", str(side.rate_bps), "--kind", side.kind]
        argv += ["--duration-s", duration, "--out", str(args.out / f"{name}.jsonl")]
        procs.sides.append(procs.spawn(name, args.driver_cpus, argv, os.environ))


def start_stack(procs: Procs, args: argparse.Namespace, state: Mapping[str, Any]) -> None:
    out = args.out
    scenario = str(args.scenario)
    mock_cpus = args.mock_cpus.split(",")
    for i, cpus in enumerate(mock_cpus):  # even: Converse on --mock-port, odd: Mantle on the next port
        argv = ["-m", "tests.load.mockserve", "--port", str(args.mock_port + i % 2), "--scenario", scenario]
        argv += ["--out", str(out / f"mock-{i}.jsonl"), "--lag-out", str(out / f"mock-{i}.lag")]
        argv += ["--reuse-port"] if len(mock_cpus) > 2 else []
        argv += ["--quotas", args.mock_quotas] if args.mock_quotas else []
        procs.mocks.append(procs.spawn(f"mock-{i}", cpus, argv, os.environ))
    for i, proc in enumerate(procs.mocks):  # with --reuse-port a request cannot pick its process: read the logs
        wait_logged(out / f"mock-{i}.log", b"mockbedrock listening", proc)
    if args.api_image:
        start_api_container(procs, args, state)
        return
    n = args.api_procs  # > 1: one pod of n processes on one inherited socket, as `uvicorn --workers n`
    sock = socket.create_server(("127.0.0.1", args.api_port), backlog=2048) if n > 1 else None
    env = api_env(state, args) | ({"WEB_CONCURRENCY": str(n)} if n > 1 else {})
    for i in range(n):
        tag = "" if n == 1 else f"-{i}"
        argv = ["-m", "tests.load.serve", "--port", str(args.api_port), "--lag-out", str(out / f"api{tag}.lag")]
        argv += ["--gc-out", str(out / f"api{tag}.gc"), "--census-out", str(out / f"census{tag}.txt")]
        if args.profile:
            argv += ["--profile-out", str(out / f"profile-{args.profile}{tag}.txt"), "--profiler", args.profile]
        if args.trace:
            argv += ["--trace-out", str(out / f"trace{tag}.jsonl")]
        if args.gc_threshold:
            argv += ["--gc-threshold", args.gc_threshold]
        if args.dispatch_yields is not None:
            argv += ["--dispatch-yields", str(args.dispatch_yields)]
        if args.sse_probe:
            argv += ["--sse-probe-out", str(out / f"api{tag}.sseprobe.json")]
        fds = (sock.fileno(),) if sock is not None else ()
        argv += ["--fd", str(fds[0])] if fds else []
        cmd = ["taskset", "-c", args.api_cpus, sys.executable, *argv]
        procs.apis.append(procs.spawn_cmd(f"api{tag}", cmd, env, pass_fds=fds))
    if sock is not None:
        sock.close()  # the api processes hold their own copies
    procs.api, procs.api_pids = procs.apis[0], [p.pid for p in procs.apis]
    procs.api_pid = procs.api.pid
    for i in range(n):  # each process serves its metrics on the first free port after the api port
        wait_ready(f"http://127.0.0.1:{args.api_port + 1 + i}/metrics", timeout_s=90)
    wait_ready(f"http://127.0.0.1:{args.api_port}/readyz", timeout_s=90)


def container_serve_args(args: argparse.Namespace, n: int) -> list[str]:
    """`tests.load.serve` arguments for the container's api processes; `{i}` is each pod process's index (C3's
    `--sse-probe`: one probe file per process in the mounted out directory)."""
    extra = ["--dispatch-yields", str(args.dispatch_yields)] if args.dispatch_yields is not None else []
    if args.sse_probe:
        extra += ["--sse-probe-out", f"{API_CONTAINER_OUT}/api{'-{i}' if n > 1 else ''}.sseprobe.json"]
    return extra


def start_api_container(procs: Procs, args: argparse.Namespace, state: Mapping[str, Any]) -> None:
    """The api from the image (its interpreter and venv) with the compose app settings (init, uid 10001, read-only
    root, no capabilities, /tmp tmpfs) on --api-cpus, host network (the stack and the mocks listen on 127.0.0.1);
    tests/load is mounted read-only for serve.py's lag sampler and GC log (`out/api/`). `--api-procs N` > 1: the pod
    launcher runs N processes on one socket with WEB_CONCURRENCY = N (a metrics port each, as in the venv mode)."""
    n = args.api_procs
    api_out = args.out / "api"
    api_out.mkdir(exist_ok=True)
    api_out.chmod(0o777)  # the container's uid 10001 writes the lag and GC files
    env_file = args.out / "api.env"
    env = {k: v for k, v in api_env(state, args).items() if k.startswith(("NOEMA_", "AWS_"))}
    env |= {"WEB_CONCURRENCY": str(n)} if n > 1 else {}
    env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    env_file.chmod(0o600)
    name = f"{state['project']}-api-load"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    cmd = ["docker", "run", "--rm", "--name", name, "--network", "host", "--cpuset-cpus", args.api_cpus, "--init"]
    cmd += ["--user", "10001:10002", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true"]
    cmd += ["--tmpfs", "/tmp:size=256m", "--env-file", str(env_file), "-w", "/opt/perf"]
    cmd += ["-v", f"{REPO / 'tests' / 'load'}:/opt/perf/tests/load:ro", "-v", f"{api_out}:{API_CONTAINER_OUT}"]
    extra = container_serve_args(args, n)
    if n > 1:
        cmd += [args.api_image, "python", "-c", POD_LAUNCHER, str(args.api_port), str(n), API_CONTAINER_OUT, *extra]
    else:
        cmd += [args.api_image, "python", "-m", "tests.load.serve", "--port", str(args.api_port)]
        cmd += ["--lag-out", f"{API_CONTAINER_OUT}/api.lag", "--gc-out", f"{API_CONTAINER_OUT}/api.gc", *extra]
    procs.container = name
    try:
        procs.api = procs.spawn_cmd("api", cmd, os.environ)
        for i in range(n):  # each process serves its metrics on the first free port after the api port
            wait_ready(f"http://127.0.0.1:{args.api_port + 1 + i}/metrics", timeout_s=90)
        wait_ready(f"http://127.0.0.1:{args.api_port}/readyz", timeout_s=90)
    finally:
        env_file.unlink(missing_ok=True)  # read by `docker run` before the container started
    init = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Pid}}", name], capture_output=True, text=True, check=True
    ).stdout
    procs.api_pid = child_pid(int(init))
    if n > 1:  # the launcher's children are the pod's processes (summed by the sampler)
        procs.api_pids = child_pids(procs.api_pid)
        if len(procs.api_pids) != n:
            raise RuntimeError(f"the pod launcher runs {len(procs.api_pids)} api processes, not {n}")
        procs.api_pid = procs.api_pids[0]


def child_pids(parent: int) -> list[int]:
    """Every child of `parent`, in pid order."""
    found: list[int] = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                stat = Path(f"/proc/{entry}/stat").read_text()
            except OSError:
                continue
            if int(stat.rsplit(")", 1)[1].split()[1]) == parent:
                found.append(int(entry))
    return sorted(found)


def child_pid(parent: int) -> int:
    """The (single) child of `parent`: the api's python (or the pod launcher) under the container's init."""
    found = child_pids(parent)
    if not found:
        raise RuntimeError(f"no child of pid {parent}")
    return found[0]


def wait_logged(log: Path, line: bytes, proc: subprocess.Popen[bytes], timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{log.stem} exited ({proc.returncode})")
        if log.exists() and line in log.read_bytes():
            return
        time.sleep(0.2)
    raise RuntimeError(f"{log.stem} not ready after {timeout_s:.0f} s")


def resource_summary(samples: Sequence[Mapping[str, Any]], t_from: int, t_to: int) -> dict[str, Any]:
    """api CPU utilization (cores), RSS (MiB) first/last/max, fds, threads and the host load inside a window."""
    rows = [s for s in samples if t_from <= s["t_ns"] < t_to and "api" in s]
    if len(rows) < 2:
        return {"samples": len(rows)}
    first, last = rows[0], rows[-1]
    dt = (last["t_ns"] - first["t_ns"]) / 1e9
    loads = [s["loadavg"][0] for s in rows]
    return {
        "samples": len(rows),
        "api_cpu_cores": round((last["api"]["cpu_s"] - first["api"]["cpu_s"]) / dt, 3),
        "driver_cpu_cores": round((last["driver_cpu_s"] - first["driver_cpu_s"]) / dt, 3),
        "mock_cpu_cores": [
            round((b - a) / dt, 3) for a, b in zip(first["mock_cpu_s"], last["mock_cpu_s"], strict=False)
        ],
        "rss_mib": {
            "first": round(first["api"]["VmRSS"] / 1024, 1),
            "last": round(last["api"]["VmRSS"] / 1024, 1),
            "max": round(max(s["api"]["VmRSS"] for s in rows) / 1024, 1),
        },
        "fds_max": max(s["api"]["fds"] for s in rows),
        "threads_max": max(s["api"]["Threads"] for s in rows),
        "loadavg_1m": {"min": min(loads), "max": max(loads), "mean": round(sum(loads) / len(loads), 2)},
    }


def side_summary(out: Path, step: int) -> dict[str, Any]:
    """Per --side group of `step`: flood drivers' turns by status/error (C5), heavy clients' turns (C3)."""
    summary: dict[str, Any] = {}
    for path in sorted(out.glob(f"side{step}-*.jsonl")):
        name = path.stem.split("-")[1]  # side<step>-<kind>-<i>[-<k>]
        rows = stats.read_jsonl([path])
        entry = summary.setdefault(name, {"turns": 0, "status": {}, "errors": {}, "violations": 0})
        entry["turns"] += len(rows)
        for row in rows:
            status, error = str(row.get("status")), row.get("error") or ",".join(map(str, row.get("errors") or []))
            entry["status"][status] = entry["status"].get(status, 0) + 1
            if error:
                entry["errors"][str(error)] = entry["errors"].get(str(error), 0) + 1
            entry["violations"] += int(bool(row.get("violation")))
        if name in ("slow", "fast"):
            heavy = [r for r in rows if r.get("status") == 200]
            entry["coalesced_turns"] = sum(1 for r in heavy if (r.get("coalesced") or 0) > 0)
            entry["coalesced_frames"] = sum(int(r.get("coalesced") or 0) for r in heavy)
            entry["eof_after_s"] = sorted(round(float(r["eof_after_s"]), 1) for r in heavy if r.get("eof_after_s"))
            entry["turn_ids"] = [r["turn_id"] for r in heavy if r.get("turn_id")]
            rates = sorted(r["bytes"] * 1e9 / (r["t_end"] - r["t_send"]) for r in heavy if r["t_end"] > r["t_send"])
            entry["read_bps_p50"] = round(rates[len(rates) // 2]) if rates else None
    return summary


def analyze_run(out: Path, steps: Sequence[Step], settle_s: float, warmup_s: float) -> dict[str, Any]:
    turns = [t for t in stats.read_jsonl(sorted(out.glob("driver-*.jsonl"))) if t.get("group", "main") == "main"]
    mock = stats.read_jsonl(sorted(out.glob("mock-*.jsonl")))
    samples = stats.read_jsonl([out / "samples.jsonl"])
    lag_files = {
        "api": sorted(out.glob("api.lag"))
        or sorted(out.glob("api-*.lag"))
        or sorted(out.glob("api/api.lag"))
        or sorted(out.glob("api/api-*.lag")),
        "mock": sorted(out.glob("mock-*.lag")),
        "drivers": sorted(out.glob("driver-*.lag")),
    }
    results = []
    for i, step in enumerate(steps):
        t_from = step.t_start + int((warmup_s if i == 0 else settle_s) * 1e9)
        entry: dict[str, Any] = {"streams": step.streams, "hold_s": step.hold_s, "t_from": t_from, "t_to": step.t_end}
        entry |= stats.analyze(turns, mock, t_from, step.t_end, lag_files=lag_files)
        entry["resources"] = resource_summary(samples, t_from, step.t_end)
        entry["errors"] = stats.errors_by_kind(stats.window(turns, t_from, step.t_end))
        entry["side"] = side_summary(out, i)
        results.append(entry)
    return {"steps": results, "memory": [s for s in samples if "api" in s][:: max(1, int(60 / SAMPLE_EVERY_S))]}


def print_summary(result: Mapping[str, Any]) -> None:
    for s in result["steps"]:
        t, e, lag = s["ttft_overhead_ms"], s["e2e_overhead_ms"], s["lag_ms"]["api"]
        res = s["resources"]
        print(
            f"streams={s['streams']:4d} n={t.get('n', 0):6d} tps={s['turns_per_s']} "
            f"ttft_ovh p50={t.get('p50')} p95={t.get('p95')} p99={t.get('p99')} | "
            f"e2e_ovh p50={e.get('p50')} p95={e.get('p95')} | api lag p99={lag.get('p99')} max={lag.get('max')} | "
            f"cpu={res.get('api_cpu_cores')} rss={res.get('rss_mib')} load={res.get('loadavg_1m')} "
            f"errors={s['errors']}"
        )


def run(args: argparse.Namespace) -> dict[str, Any]:
    state = orjson.loads(args.state.read_bytes())
    steps = parse_plan(args.plan)
    args.out.mkdir(parents=True, exist_ok=True)
    key_file = args.out / "key"
    key_file.write_text(state["tenants"]["t_acme"]["projects"]["main"]["principals"]["developer"]["key"])
    key_file.chmod(0o600)
    procs = Procs(args.out)
    provenance = {
        "code": code_version(),
        "config": anyio.run(active_config, state),
        "scenario_sha256": hashlib.sha256(args.scenario.read_bytes()).hexdigest(),
        "api": {"image": args.api_image, "image_id": image_id(args.api_image), "python": image_python(args.api_image)}
        if args.api_image
        else {"image": None, "python": sys.version.split()[0], "executable": sys.executable},
        "placement": {
            "api": args.api_cpus,
            "api_procs": args.api_procs,
            "mocks": args.mock_cpus.split(","),
            "drivers": args.driver_cpus,
            "stack": pin_stack(str(state["project"]), args.stack_cpus),
        },
        "dispatch_yields": args.dispatch_yields,
        "loadavg_at_start": loadavg(),
    }
    (args.out / "provenance.json").write_text(json.dumps(provenance, indent=1))
    try:
        start_stack(procs, args, state)
        sampler = Sampler(procs, args.out / "samples.jsonl")
        settle = [args.warmup_s if i == 0 else args.settle_s for i in range(len(steps))]
        t = time.monotonic_ns()
        end_ns = t + int((sum(settle) + sum(s.hold_s for s in steps)) * 1e9)
        for i, step in enumerate(steps):
            step.t_start = t
            step.t_end = t + int((settle[i] + step.hold_s) * 1e9)
            start_drivers(procs, args, step.streams, end_ns, key_file)
            start_sides(procs, args, state, i, step.t_end)
            window_from = step.t_start + int(settle[i] * 1e9)
            if args.profile and args.profile_step == i:
                sampler.sleep_until(window_from)
                for api in procs.apis:
                    api.send_signal(signal.SIGUSR1)
            sampler.sleep_until(step.t_end)
            if args.profile and args.profile_step == i:
                for api in procs.apis:
                    api.send_signal(signal.SIGUSR2)
            for k in range(args.api_procs):
                tag = "" if k == 0 else f"-{k}"
                scrape(f"http://127.0.0.1:{args.api_port + 1 + k}/metrics", args.out / f"metrics-step{i}{tag}.txt")
            if args.census:
                for api in procs.apis:
                    api.send_signal(signal.SIGRTMIN)
            t = step.t_end
        for proc in procs.drivers + procs.sides:  # no new turn starts after end_ns; the last ones finish soon after
            while proc.poll() is None:
                sampler.tick()
                time.sleep(0.5)
    finally:
        procs.stop_all()
        key_file.unlink(missing_ok=True)
        for side_key_file in args.out.glob("key-*"):
            side_key_file.unlink()
    (args.out / "steps.json").write_text(json.dumps([s.__dict__ for s in steps]))
    return analyze_run(args.out, steps, args.settle_s, args.warmup_s)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.load.run", description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, required=True, help="the stack's bootstrap state file")
    parser.add_argument("--out", type=Path, required=True, help="raw data directory (outside the repo: large)")
    parser.add_argument("--results", type=Path, help="write the analyzed results JSON here too")
    parser.add_argument("--plan", default="50:300,100:300,200:900", help="streams:hold_s,...")
    parser.add_argument("--warmup-s", type=float, default=60.0)
    parser.add_argument("--settle-s", type=float, default=30.0)
    parser.add_argument("--turns-per-session", type=int, default=10)
    parser.add_argument("--scenario", type=Path, default=REPO / "tests" / "load" / "scenarios" / "overhead.yaml")
    parser.add_argument("--api-port", type=int, default=28790, help="the api; its /metrics on the next port")
    parser.add_argument("--mock-port", type=int, default=28781, help="Converse mock; Mantle on the next port")
    parser.add_argument("--api-cpus", default="46-47")
    parser.add_argument("--api-procs", type=int, default=1, help="api processes of the pod (WEB_CONCURRENCY)")
    parser.add_argument("--api-env", action="append", default=[], help="K=V for the api processes (repeatable)")
    parser.add_argument("--stack-cpus", default="40-45", help="docker update the stack's postgres and valkey ('' = no)")
    parser.add_argument(
        "--mock-cpus",
        default="32-33,34-35,36-37,38-39",
        help="one taskset list per mock process: even = Converse, odd = Mantle; more than 2 share the ports",
    )
    parser.add_argument("--driver-cpus", default="20-31")
    parser.add_argument("--api-image", help="run the api from this image (e.g. noema-api:dev) instead of the venv")
    parser.add_argument("--dispatch-yields", type=int, help="override DISPATCH_YIELDS in the api (A/B runs)")
    parser.add_argument("--profile", choices=("cpu", "sample", "cprofile"), help="profile the api in one step")
    parser.add_argument("--profile-step", type=int, default=0)
    parser.add_argument("--census", action="store_true", help="a GC heap census of the api at each step end")
    parser.add_argument("--trace", action="store_true", help="createTurn timelines from the api (diagnosis only)")
    parser.add_argument("--gc-threshold", help="a,b,c for gc.set_threshold in the api (experiments)")
    parser.add_argument("--side", dest="sides", type=parse_side, action="append", default=[], help=Side.__doc__)
    parser.add_argument("--mock-quotas", help="JSON quota list for every mock process (C1; tools/mockbedrock)")
    parser.add_argument("--sse-probe", action="store_true", help="SSE writer probe in the api (C3; sseprobe.py)")
    parser.add_argument("--analyze-only", action="store_true", help="re-analyze an existing --out directory")
    args = parser.parse_args(argv)
    if args.api_image and (args.profile or args.trace or args.census):
        parser.error("--profile, --trace and --census need the venv api (no --api-image)")
    if args.analyze_only:
        steps = [Step(**s) for s in json.loads((args.out / "steps.json").read_text())]
        result = analyze_run(args.out, steps, args.settle_s, args.warmup_s)
    else:
        result = run(args)
    provenance = args.out / "provenance.json"
    if provenance.exists():
        result["provenance"] = json.loads(provenance.read_text())
    result["config"] = {k: str(v) for k, v in vars(args).items() if k not in ("state",)}
    (args.out / "results.json").write_text(json.dumps(result, indent=1))
    if args.results:
        args.results.parent.mkdir(parents=True, exist_ok=True)
        args.results.write_text(json.dumps(result, indent=1))
    print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
