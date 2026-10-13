"""Inside the media image only (`make test-media`: network_mode none, container root, the ADR-002 capabilities):
the real NsjailRunner, sandboxd + RemoteSandboxRunner over the UDS, and the full pipeline on the probe fixtures.

MED-02 golden derivatives (number.png OCR reads 4471; invoice.pdf → pdf_sanitized, markdown via Docling,
page_images pages=1), SEC-SBX-02 (egress), SEC-SBX-04 (planted out/ entries through a real jail), SEC-SBX-05
(limits; the other slot keeps working), SEC-SBX-06 (no credentials in the jail)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from tests.contract_internal.kits import principal
from tests.unit.media_pipeline import media_fixtures as fx
from tests.unit.media_pipeline.support import media_env

from noema.core.protocols.sandbox import SandboxJob, SandboxLimits
from noema.media.sandbox import spool
from noema.media.sandbox.limits import KIND_LIMITS, clamp
from noema.media.sandbox.nsjail import JAIL_PYTHON, NsjailRunner

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("NOEMA_MEDIA_IMAGE") != "1", reason="runs inside the media image: make test-media"
    ),
]


def job_id() -> str:
    """A fresh valid `sbx_<ULID>` id (hex digits are Crockford base32)."""
    return "sbx_" + uuid.uuid4().hex.upper()[:26]


@pytest.fixture
def spool_root() -> Iterator[Path]:
    """A spool the jail uids can traverse (pytest's tmp dirs are 0700): /spool when writable, else /tmp/<x> 0711."""
    base = Path("/spool") if os.access("/spool", os.W_OK) else Path("/tmp")
    root = base / f"media-it-{uuid.uuid4().hex[:8]}"
    root.mkdir(mode=0o711)
    os.chmod(root, 0o711)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def nsjail(spool_root: Path) -> NsjailRunner:
    return NsjailRunner("test", spool_root=spool_root, owner_uid=os.getuid(), owner_gid=os.getgid())


async def probe(
    runner: NsjailRunner, spool_root: Path, code: str, limits: SandboxLimits | None = None
) -> tuple[str, int, Path]:
    """Run `python -I -c code` in a detect-shaped jail; returns (raw status, return code, job dir)."""
    jid = job_id()
    jd = spool.prepare(spool_root, jid, b"input")
    status, outcome = await runner.execute(
        jd, "detect", "{}", limits or KIND_LIMITS["detect"], argv=[JAIL_PYTHON, "-I", "-c", code]
    )
    return status, outcome.returncode, jd


EGRESS = """
import errno, json, socket
out = {}
for name, family, kind, addr in (("tcp", socket.AF_INET, socket.SOCK_STREAM, ("10.0.0.1", 80)),
                                 ("udp_dns", socket.AF_INET, socket.SOCK_DGRAM, ("8.8.8.8", 53)),
                                 ("tcp6", socket.AF_INET6, socket.SOCK_STREAM, ("2001:4860:4860::8888", 53, 0, 0))):
    try:
        s = socket.socket(family, kind)
        s.settimeout(2)
        s.connect(addr) if kind == socket.SOCK_STREAM else s.sendto(b"x", addr)
        out[name] = "connected"
    except OSError as exc:
        out[name] = errno.errorcode.get(exc.errno, str(exc.errno))
try:
    socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
    out["netlink"] = "opened"
except OSError as exc:
    out["netlink"] = errno.errorcode.get(exc.errno, str(exc.errno))
open("/work/out/egress.txt", "w").write(json.dumps(out))
"""


async def test_sec_sbx_02_no_egress_from_a_jail(nsjail: NsjailRunner, spool_root: Path) -> None:
    import json

    status, code, jd = await probe(nsjail, spool_root, EGRESS)
    assert status == "ok", code
    result = json.loads((jd / "out" / "egress.txt").read_text())
    assert result["tcp"] in ("EAFNOSUPPORT", "ENETUNREACH") and result["udp_dns"] in ("EAFNOSUPPORT", "ENETUNREACH")
    assert result["tcp6"] == "EAFNOSUPPORT" and result["netlink"] == "EAFNOSUPPORT"


async def test_sec_sbx_06_no_credentials_in_the_jail(nsjail: NsjailRunner, spool_root: Path) -> None:
    import json

    code = (
        "import os, json\n"
        "seen = {'env': sorted(os.environ), 'aws': os.path.exists('/root/.aws')}\n"
        "open('/work/out/env.txt','w').write(json.dumps(seen))"
    )
    status, _, jd = await probe(nsjail, spool_root, code)
    seen = json.loads((jd / "out" / "env.txt").read_text())
    assert status == "ok" and not seen["aws"]
    assert not [k for k in seen["env"] if k.startswith(("AWS_", "NOEMA_DB", "REDIS")) or "SECRET" in k]
    harness = {"AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_EC2_METADATA_DISABLED"}  # /dev/null pointers
    assert not [k for k in os.environ if k.startswith("AWS_") and k not in harness]  # nor in the container


async def test_sec_sbx_04_planted_entries_are_removed_and_targets_untouched(
    nsjail: NsjailRunner, spool_root: Path
) -> None:
    code = (
        "import os\n"
        "os.symlink('/etc/passwd', '/work/out/x.png')\n"
        "open('/work/out/ok.png','wb').write(b'\\x89PNG\\r\\n\\x1a\\n')\n"
        "os.link('/work/out/ok.png', '/work/out/hard.png')\n"
        "os.mkfifo('/work/out/fifo.png')\n"
        "os.mkdir('/work/out/sub')\n"
    )
    before = os.stat("/etc/passwd")
    status, _, jd = await probe(nsjail, spool_root, code)
    assert status == "ok"
    assert sorted(p.name for p in (jd / "out").iterdir()) == []  # ok.png had 2 links: removed too
    after = os.stat("/etc/passwd")
    assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
        before.st_uid,
        before.st_gid,
        stat.S_IMODE(before.st_mode),
    )


@pytest.mark.timeout(120)
async def test_sec_sbx_05_limits_kill_and_the_other_slot_keeps_working(nsjail: NsjailRunner, spool_root: Path) -> None:
    busy = clamp("detect", SandboxLimits(wall_s=3, cpu_s=10, mem_mib=3072, fsize_mib=1, nproc=32, max_outputs=0))
    results: dict[str, str] = {}

    async def run_busy() -> None:
        results["busy"] = (await probe(nsjail, spool_root, "while True: pass", busy))[0]

    async def run_normal() -> None:
        src = spool_root / "number.png"
        src.write_bytes(fx.NUMBER_PNG.read_bytes() if fx.NUMBER_PNG.exists() else fx.png())
        result = await nsjail.run(SandboxJob(job_id(), "detect", src, {}), KIND_LIMITS["detect"])
        results["normal"] = result.status

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_busy)
        tg.start_soon(run_normal)
    assert results == {"busy": "timeout", "normal": "ok"}
    memory = clamp("detect", SandboxLimits(wall_s=15, cpu_s=10, mem_mib=256, fsize_mib=1, nproc=32, max_outputs=0))
    assert (await probe(nsjail, spool_root, "x = bytearray(512 * 1024 * 1024)", memory))[0] == "failed"
    fork = "import os\nwhile True:\n    os.fork()"
    assert (await probe(nsjail, spool_root, fork, busy))[0] in ("failed", "timeout")
    write = "open('/work/out/big.bin','wb').write(b'0' * (1024 * 1024 * 1024))"
    assert (await probe(nsjail, spool_root, write))[0] == "failed"  # RLIMIT_FSIZE (1 MiB for detect)
    ptrace = "import ctypes; ctypes.CDLL(None).syscall(101, 0, 0, 0, 0)"
    assert (await probe(nsjail, spool_root, ptrace))[0] == "violation"  # the kafel deny-list: SIGSYS


@asynccontextmanager
async def sandboxd(spool_root: Path, *, allowed_uid: int) -> AsyncIterator[Path]:
    """sandboxd on a test socket inside this process (the test runs as container root)."""
    from noema.media.sandbox.sandboxd import Sandboxd, serve

    runner = NsjailRunner("test", spool_root=spool_root, owner_uid=os.getuid(), owner_gid=os.getgid())
    socket_path = spool_root / "sandboxd.sock"
    async with anyio.create_task_group() as tg:
        await tg.start(serve, Sandboxd(runner, spool_root=spool_root, allowed_uid=allowed_uid), socket_path)
        try:
            yield socket_path
        finally:
            tg.cancel_scope.cancel()


@pytest.mark.enable_socket
async def test_remote_runner_over_sandboxd(spool_root: Path) -> None:
    from noema.media.sandbox.remote import RemoteSandboxRunner

    async with sandboxd(spool_root, allowed_uid=os.getuid()) as sock:
        remote = RemoteSandboxRunner("test", socket_path=sock, spool_root=spool_root)
        jid = job_id()
        spool.prepare(spool_root, jid, fx.png(64, 40))
        job = SandboxJob(jid, "image.normalize", spool.input_path(spool_root, jid), {"format": "PNG"})
        result = await remote.run(job, KIND_LIMITS["image.normalize"])
        assert result.status == "ok" and {o.kind for o in result.outputs} == {"raster", "raster_1568", "thumbnail"}
        assert all(o.path.read_bytes().startswith(b"\x89PNG") for o in result.outputs)
        bad_job = SandboxJob(job_id(), "image.normalize", spool_root / "nope", {"format": "BMP"})
        bad = await remote.run(bad_job, KIND_LIMITS["image.normalize"])
        assert bad.status == "failed" and bad.facts["error"] == "bad_request"
    async with sandboxd(spool_root, allowed_uid=12345) as sock:  # SO_PEERCRED: not the media-worker uid
        remote = RemoteSandboxRunner("test", socket_path=sock, spool_root=spool_root)
        jid = job_id()
        spool.prepare(spool_root, jid, fx.png())
        refused = await remote.run(
            SandboxJob(jid, "detect", spool.input_path(spool_root, jid), {}), KIND_LIMITS["detect"]
        )
        assert refused.status == "failed" and refused.facts["error"] == "bad_request"


@pytest.mark.slow
@pytest.mark.timeout(900)
async def test_med_02_golden_derivatives_through_nsjail(nsjail: NsjailRunner, spool_root: Path, tmp_path: Path) -> None:
    if not (fx.NUMBER_PNG.is_file() and fx.INVOICE_PDF.is_file()):
        pytest.skip(f"probe fixtures not mounted at {fx.PROBES}")
    env = media_env(tmp_path, sandbox=nsjail, spool_root=spool_root)

    async def chunks(data: bytes) -> AsyncIterator[bytes]:
        yield data

    me = principal()
    png = await env.files.upload(
        me, chunks(fx.NUMBER_PNG.read_bytes()), declared_type="image/png", filename="number.png", session_id=None
    )
    pdf = await env.files.upload(
        me,
        chunks(fx.INVOICE_PDF.read_bytes()),
        declared_type="application/pdf",
        filename="invoice.pdf",
        session_id=None,
    )
    await env.drain()
    image = await env.files.get(me, png.file_id)
    assert image.status == "ready", image
    assert "4471" in (await env.files.read_derivative(me, png.file_id, "ocr_text")).decode()
    doc = await env.files.get(me, pdf.file_id)
    statuses = {d.kind: d.status for d in doc.derivatives}
    assert (
        doc.status == "ready"
        and statuses["pdf_sanitized"] == statuses["markdown"] == statuses["page_images"] == "ready"
    )
    assert next(d for d in doc.derivatives if d.kind == "page_images").pages == 1 and "ocr_text" not in statuses
    markdown = (await env.files.read_derivative(me, pdf.file_id, "markdown")).decode()
    assert "4471" in markdown and markdown.count("\f") == 0  # one page: one segment


@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.parametrize("kind", ["detect", "image.normalize", "pdf.sanitize", "pdf.render", "markdown.sanitize"])
async def test_sec_sbx_08_fuzz_harness_smoke(kind: str, tmp_path: Path) -> None:
    """A short atheris run per kind proves the nightly harness works (the nightly job runs it for hours)."""
    pytest.importorskip("atheris")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    seeds = {"seed-png": fx.png(8, 8), "seed-pdf": fx.text_pdf(), "seed-md": b"# a\n\n[x](javascript:y)\n"}
    for name, data in seeds.items():
        (corpus / name).write_bytes(data)
    cmd = ["python", "-m", "tests.integration.media_pipeline.fuzz_extractors", kind, "-runs=300", "-max_len=4096"]
    root = Path(__file__).parents[3]  # the repo (or /opt/noema-test) root: `tests` is importable from there
    done = await anyio.run_process([*cmd, str(corpus)], check=False, cwd=root)
    log = done.stderr.decode(errors="replace")
    assert done.returncode == 0, log[-2000:]
    # Wave E (QA security finding 21): uninstrumented, libFuzzer ran blind ("Is the code instrumented ...", cov 0)
    assert "Is the code instrumented for coverage" not in log, log[-2000:]
    assert max((int(n) for n in re.findall(r"cov: (\d+)", log)), default=0) > 0, log[-2000:]
