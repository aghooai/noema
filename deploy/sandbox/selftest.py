"""ADR-002 §2 sandbox selftest: checks 1-10 inside real nsjail jails (SEC-SBX-01).

Run inside the media-sandbox container (as container root, with the compose privileges):

    python -I /etc/noema-sandbox/selftest.py [--json] [--policy PATH] [--nsjail PATH] [--spool DIR]

Each check spawns one jail built like ADR-002 §3 (namespaces incl. a network namespace without interfaces other
than a down `lo`, uid 65534 mapped to outside uid 100000, rlimits, read-only binds, the kafel deny-list) and runs
this file's `probe` sub-command inside it. Exit status 0 iff every check passes; 2 when the jail itself cannot
be started (missing privileges: see PRIVILEGES.md). Standard library only: it runs before any noema code.
"""

# A CLI (T201: prints are its output) that runs nsjail by design (S603); /tmp paths are jail-internal (S108) and the
# spool modes are the ADR-002 §6 layout (S103).
# ruff: noqa: T201, S603, S108, S103

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
JAIL_PYTHON = "/opt/noema-sandbox/venv/bin/python"
JAIL_SELFTEST_DIR = "/selftest"  # where HERE is bind-mounted (read-only) inside the jail
SLOT_UID = 100000  # outside uid of jail slot 0 (ADR-002 §3)
SIGSYS_EXIT = 128 + 31  # nsjail's status for a child killed by SIGSYS (seccomp KILL_PROCESS)
SIGKILL_EXIT = 128 + 9

# x86_64 syscall numbers and flags used by the probes.
NR = {"ptrace": 101, "clone": 56, "unshare": 272, "bpf": 321, "io_uring_setup": 425, "fsopen": 430, "clone3": 435}
X32_SYSCALL_BIT = 0x40000000
NR_X32_PTRACE = 521  # ptrace in the x32 syscall table
CLONE_NEWUSER = 0x10000000
SIGCHLD = 17
SIOCGIFFLAGS = 0x8913
IFF_UP = 0x1


# ----------------------------------------------------------------------------------------------------------------------
# In-jail probes: each prints one JSON line {"ok": bool, "detail": str}; kill-expected probes never return.
# ----------------------------------------------------------------------------------------------------------------------
def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _raw_syscall(nr: int, *args: int) -> tuple[int, int]:
    """Call syscall(nr, *args) and return (result, errno)."""
    libc = _libc()
    fn = libc.syscall
    fn.restype = ctypes.c_long
    res = fn(ctypes.c_long(nr), *(ctypes.c_long(a) for a in args))
    return int(res), ctypes.get_errno()


def _socket_errno(family: int) -> int:
    try:
        socket.socket(family, socket.SOCK_DGRAM).close()
    except OSError as exc:
        return exc.errno or -1
    return 0


def probe_socket_inet() -> tuple[bool, str]:
    err = _socket_errno(socket.AF_INET)
    return err == errno.EAFNOSUPPORT, f"socket(AF_INET) errno={err}"


def probe_socket_families() -> tuple[bool, str]:
    got = {name: _socket_errno(fam) for name, fam in (("AF_INET6", socket.AF_INET6), ("AF_NETLINK", socket.AF_NETLINK))}
    unix = _socket_errno(socket.AF_UNIX)
    ok = all(v == errno.EAFNOSUPPORT for v in got.values()) and unix == 0
    return ok, f"errno {got}, AF_UNIX errno={unix}"


def probe_net_ifaces() -> tuple[bool, str]:
    """Run without the policy: only `lo` exists, it is down, and loopback connects fail."""
    names = [name for _, name in socket.if_nameindex()]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        req = struct.pack("16sH14x", b"lo", 0)
        flags = struct.unpack("16sH14x", fcntl.ioctl(s.fileno(), SIOCGIFFLAGS, req))[1]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.connect(("127.0.0.1", 9))
            conn = 0
        except OSError as exc:
            conn = exc.errno or -1
    ok = names == ["lo"] and not flags & IFF_UP and conn == errno.ENETUNREACH
    return ok, f"interfaces={names} lo_up={bool(flags & IFF_UP)} connect_errno={conn}"


def probe_spool_isolation() -> tuple[bool, str]:
    work = sorted(os.listdir("/work"))
    data = Path("/work/in/input.bin").read_bytes()
    ok = work == ["in", "out"] and not os.path.exists("/spool") and data == b"selftest-input"
    return ok, f"/work={work} /spool_exists={os.path.exists('/spool')} input_ok={data == b'selftest-input'}"


def probe_proc_environ() -> tuple[bool, str]:
    try:
        Path("/proc/1/environ").read_bytes()
    except OSError as exc:
        return True, f"unreadable ({errno.errorcode.get(exc.errno or 0, exc.errno)})"
    return False, "/proc/1/environ is readable"


def probe_write_outside() -> tuple[bool, str]:
    denied: dict[str, str] = {}
    for target in ("/x", "/usr/x", "/etc/x", "/work/x", "/work/in/x", "/opt/noema-sandbox/x", "/selftest/x"):
        try:
            Path(target).write_bytes(b"x")
            denied[target] = "WRITTEN"
        except OSError as exc:
            denied[target] = errno.errorcode.get(exc.errno or 0, str(exc.errno))
    allowed: dict[str, str] = {}
    for target in ("/work/out/ok.txt", "/tmp/ok.txt"):
        try:
            Path(target).write_bytes(b"ok")
            allowed[target] = "ok"
        except OSError as exc:
            allowed[target] = errno.errorcode.get(exc.errno or 0, str(exc.errno))
    ok = "WRITTEN" not in denied.values() and set(allowed.values()) == {"ok"}
    return ok, f"denied={denied} allowed={allowed}"


def probe_alloc(mib: int) -> tuple[bool, str]:
    """Allocate `mib` MiB and touch it; under a smaller RLIMIT_AS this raises MemoryError (non-zero exit)."""
    buf = bytearray(mib * 1024 * 1024)
    buf[:: 1 << 20] = b"\x01" * len(buf[:: 1 << 20])
    return True, f"ALLOCATED {mib} MiB"


def probe_busy_loop() -> tuple[bool, str]:
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        pass
    return True, "SURVIVED the time limit"


def _expect_killed(name: str, nr: int, *args: int) -> tuple[bool, str]:
    res, err = _raw_syscall(nr, *args)
    return False, f"{name} returned {res} errno={err} (expected SIGSYS)"


def probe_ptrace() -> tuple[bool, str]:
    return _expect_killed("ptrace", NR["ptrace"], 0, 0, 0, 0)


def probe_io_uring_setup() -> tuple[bool, str]:
    return _expect_killed("io_uring_setup", NR["io_uring_setup"], 1, 0)


def probe_unshare_newuser() -> tuple[bool, str]:
    return _expect_killed("unshare(CLONE_NEWUSER)", NR["unshare"], CLONE_NEWUSER)


def probe_clone_newuser() -> tuple[bool, str]:
    res, err = _raw_syscall(NR["clone"], CLONE_NEWUSER | SIGCHLD, 0, 0, 0, 0)
    if res == 0:  # the policy failed and this is the child: leave without running the parent's code
        os._exit(0)
    return False, f"clone(CLONE_NEWUSER) returned {res} errno={err} (expected SIGSYS)"


def probe_fsopen() -> tuple[bool, str]:
    return _expect_killed("fsopen", NR["fsopen"], 0, 0)


def probe_bpf() -> tuple[bool, str]:
    return _expect_killed("bpf", NR["bpf"], 0, 0, 0)


def probe_clone3() -> tuple[bool, str]:
    res, err = _raw_syscall(NR["clone3"], 0, 0)
    return res == -1 and err == errno.ENOSYS, f"clone3 returned {res} errno={err} (expected ENOSYS)"


def probe_x32_ptrace() -> tuple[bool, str]:
    """Reaching this return means the call was not killed: it must then have failed with ENOSYS."""
    res, err = _raw_syscall(X32_SYSCALL_BIT | NR_X32_PTRACE, 0, 0, 0, 0)
    return res == -1 and err == errno.ENOSYS, f"x32 ptrace returned {res} errno={err} (expected SIGSYS or ENOSYS)"


PROBES: dict[str, Callable[[], tuple[bool, str]]] = {
    "socket_inet": probe_socket_inet,
    "socket_families": probe_socket_families,
    "net_ifaces": probe_net_ifaces,
    "spool_isolation": probe_spool_isolation,
    "proc_environ": probe_proc_environ,
    "write_outside": probe_write_outside,
    "alloc_2x": lambda: probe_alloc(2 * ALLOC_LIMIT_MIB),
    "busy_loop": probe_busy_loop,
    "ptrace": probe_ptrace,
    "io_uring_setup": probe_io_uring_setup,
    "unshare_newuser": probe_unshare_newuser,
    "clone_newuser": probe_clone_newuser,
    "fsopen": probe_fsopen,
    "bpf": probe_bpf,
    "clone3": probe_clone3,
    "x32_ptrace": probe_x32_ptrace,
}
ALLOC_LIMIT_MIB = 512  # RLIMIT_AS of the alloc_2x jail


def run_probe(name: str) -> int:
    ok, detail = PROBES[name]()
    print(json.dumps({"ok": ok, "detail": detail}), flush=True)
    return 0


# ----------------------------------------------------------------------------------------------------------------------
# Driver: one jail per check, configured like ADR-002 §3 (with the corrections recorded in PRIVILEGES.md).
# ----------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Check:
    """One selftest row: `expect` is "probe" (the probe must report ok) or an exact nsjail exit status."""

    adr: int
    name: str
    probe: str
    expect: str | int
    policy: bool = True
    time_limit_s: int = 30
    rlimit_as_mib: int | None = 2048
    also_ok: tuple[int, ...] = ()


CHECKS: tuple[Check, ...] = (
    Check(1, "socket(AF_INET) fails", "socket_inet", "probe"),
    Check(2, "only lo, and lo is down (namespaces only, no policy)", "net_ifaces", "probe", policy=False),
    Check(3, "/spool outside the job dir is invisible", "spool_isolation", "probe"),
    Check(4, "/proc/1/environ is unreadable", "proc_environ", "probe"),
    Check(5, "ptrace -> SIGSYS", "ptrace", SIGSYS_EXIT),
    Check(5, "io_uring_setup -> SIGSYS", "io_uring_setup", SIGSYS_EXIT),
    Check(6, "writes outside /work/out and /tmp fail", "write_outside", "probe"),
    Check(7, "a 2x rlimit_as allocation dies", "alloc_2x", "dies", rlimit_as_mib=ALLOC_LIMIT_MIB),
    Check(8, "the time limit kills a busy loop", "busy_loop", SIGKILL_EXIT, time_limit_s=2),
    Check(9, "unshare(CLONE_NEWUSER) -> SIGSYS", "unshare_newuser", SIGSYS_EXIT),
    Check(9, "clone(CLONE_NEWUSER) -> SIGSYS", "clone_newuser", SIGSYS_EXIT),
    Check(9, "fsopen -> SIGSYS", "fsopen", SIGSYS_EXIT),
    Check(9, "bpf -> SIGSYS", "bpf", SIGSYS_EXIT),
    Check(9, "clone3 -> ENOSYS", "clone3", "probe"),
    Check(9, "socket(AF_INET6), socket(AF_NETLINK) -> EAFNOSUPPORT", "socket_families", "probe"),
    Check(10, "x32 ptrace killed or ENOSYS", "x32_ptrace", "probe", also_ok=(SIGSYS_EXIT,)),
)


@dataclass(slots=True)
class Result:
    adr: int
    name: str
    passed: bool
    exit_status: int
    wall_ms: int
    detail: str
    stderr_tail: str = field(default="")


def jail_config(check: Check, job_dir: Path) -> str:
    """The nsjail text-proto config of one selftest jail (ADR-002 §3 layout)."""
    rlimit_as = (
        f"rlimit_as_type: VALUE\nrlimit_as: {check.rlimit_as_mib}"
        if check.rlimit_as_mib is not None
        else "rlimit_as_type: INF"
    )
    return f"""
name: "selftest-{check.probe}"
mode: ONCE
hostname: "sbx"
cwd: "/work"
time_limit: {check.time_limit_s}
max_cpus: 1
keep_env: false
envar: "LANG=C.UTF-8"
envar: "HOME=/tmp"
clone_newnet: true
clone_newuser: true
clone_newns: true
clone_newpid: true
clone_newipc: true
clone_newuts: true
clone_newcgroup: true
iface_no_lo: true
uidmap {{ inside_id: "65534" outside_id: "{SLOT_UID}" count: 1 }}
gidmap {{ inside_id: "65534" outside_id: "{SLOT_UID}" count: 1 }}
{rlimit_as}
rlimit_cpu_type: VALUE
rlimit_cpu: {check.time_limit_s}
rlimit_fsize_type: VALUE
rlimit_fsize: 1
rlimit_nofile_type: VALUE
rlimit_nofile: 64
rlimit_nproc_type: VALUE
rlimit_nproc: 16
rlimit_stack_type: VALUE
rlimit_stack: 8
rlimit_core_type: VALUE
rlimit_core: 0
mount_proc: false
mount {{ src: "/usr" dst: "/usr" is_bind: true rw: false }}
mount {{ src: "/lib" dst: "/lib" is_bind: true rw: false }}
mount {{ src: "/lib64" dst: "/lib64" is_bind: true rw: false mandatory: false }}
mount {{ src: "/etc/ld.so.cache" dst: "/etc/ld.so.cache" is_bind: true rw: false }}
mount {{ src: "/opt/noema-sandbox" dst: "/opt/noema-sandbox" is_bind: true rw: false }}
mount {{ src: "{HERE}" dst: "{JAIL_SELFTEST_DIR}" is_bind: true rw: false }}
mount {{ src: "/dev/null" dst: "/dev/null" is_bind: true rw: true }}
mount {{ src: "/dev/urandom" dst: "/dev/urandom" is_bind: true rw: false }}
mount {{ dst: "/tmp" fstype: "tmpfs" rw: true options: "size=64m" nosuid: true nodev: true }}
mount {{ src: "{job_dir}/in" dst: "/work/in" is_bind: true rw: false }}
mount {{ src: "{job_dir}/out" dst: "/work/out" is_bind: true rw: true }}
"""


def _prepare_job(spool: Path, name: str) -> Path:
    """ADR-002 §6 spool layout: job dir 0711, in/ 0755 with input.bin 0644, out/ 0700 owned by the slot uid."""
    job = spool / f"sbx_selftest_{name}"
    shutil.rmtree(job, ignore_errors=True)
    (job / "in").mkdir(parents=True)
    (job / "out").mkdir()
    (job / "in" / "input.bin").write_bytes(b"selftest-input")
    os.chmod(job, 0o711)
    os.chmod(job / "in", 0o755)
    os.chmod(job / "in" / "input.bin", 0o644)
    os.chmod(job / "out", 0o700)
    os.chown(job / "out", SLOT_UID, SLOT_UID)
    return job


def run_check(check: Check, *, nsjail: str, policy: Path, spool: Path) -> Result:
    job = _prepare_job(spool, check.probe)
    with tempfile.NamedTemporaryFile("w", suffix=".cfg", prefix="selftest-", delete=False) as cfg:
        cfg.write(jail_config(check, job))
    argv = [nsjail, "--config", cfg.name, "--quiet"]
    if check.policy:
        argv += ["--seccomp_policy", str(policy)]
    argv += ["--", JAIL_PYTHON, "-I", f"{JAIL_SELFTEST_DIR}/selftest.py", "probe", check.probe]
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=check.time_limit_s + 30, check=False)
    finally:
        os.unlink(cfg.name)
        shutil.rmtree(job, ignore_errors=True)
    wall_ms = int((time.monotonic() - started) * 1000)
    reported = _last_json(proc.stdout)
    rc = proc.returncode
    if check.expect == "probe":
        passed = (rc == 0 and bool(reported.get("ok"))) or rc in check.also_ok
    elif check.expect == "dies":
        passed = rc != 0 and "ALLOCATED" not in proc.stdout and "MemoryError" in proc.stderr
    else:
        passed = rc == check.expect
    detail = str(reported.get("detail", "")) or (proc.stdout.strip()[-200:] or "no probe output")
    return Result(check.adr, check.name, passed, rc, wall_ms, detail, proc.stderr.strip()[-600:])


def _last_json(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def environment_report() -> dict[str, str]:
    """The container's effective privileges, recorded next to the results (PRIVILEGES.md)."""
    report: dict[str, str] = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("CapEff", "CapBnd", "Seccomp", "NoNewPrivs"):
            report[key] = value.strip()
    net = Path("/sys/class/net")
    report["container_interfaces"] = ",".join(sorted(os.listdir(net))) if net.is_dir() else "?"
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["probe"] and len(args) == 2:
        return run_probe(args[1])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="print one JSON document instead of a table")
    parser.add_argument(
        "--policy", type=Path, default=Path(os.environ.get("NOEMA_SANDBOX_POLICY", HERE / "seccomp.kafel"))
    )
    parser.add_argument("--nsjail", default=shutil.which("nsjail") or "/usr/local/bin/nsjail")
    parser.add_argument("--spool", type=Path, default=None, help="spool root (default: /spool if writable, else /tmp)")
    parser.add_argument("--only", nargs="*", default=None, help="probe names to run (default: all)")
    opts = parser.parse_args(args)
    spool = opts.spool or (Path("/spool") if os.access("/spool", os.W_OK) else Path(tempfile.mkdtemp(prefix="spool-")))
    sibling = spool / "sbx_selftest_other"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "secret.txt").write_text("must stay invisible")
    try:
        checks = [c for c in CHECKS if opts.only is None or c.probe in opts.only]
        results = [run_check(c, nsjail=opts.nsjail, policy=opts.policy, spool=spool) for c in checks]
    finally:
        shutil.rmtree(sibling, ignore_errors=True)
    jail_failed = any(r.exit_status == 255 and "launch the child" in r.stderr_tail for r in results)
    status = 2 if jail_failed else (0 if all(r.passed for r in results) else 1)
    if opts.json:
        doc = {"status": status, "environment": environment_report(), "results": [asdict(r) for r in results]}
        print(json.dumps(doc, indent=2))
    else:
        for key, value in environment_report().items():
            print(f"# {key}: {value}")
        for r in results:
            mark = "PASS" if r.passed else "FAIL"
            print(f"{mark}  check {r.adr:>2}  {r.name}  (exit {r.exit_status}, {r.wall_ms} ms)  {r.detail}")
            if not r.passed and r.stderr_tail:
                print("      nsjail: " + r.stderr_tail.replace("\n", "\n      nsjail: "))
    return status


if __name__ == "__main__":
    sys.exit(main())
