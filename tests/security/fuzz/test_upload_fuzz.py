"""SE9 (06 §9 "Uploads"; ADR-002 SEC-SBX-03 corpus and a short SEC-SBX-08 run): inside the media image only
(`make qa-security-fuzz`: `make test-media`'s network-less media-sandbox settings). Two parts:

1. A deterministic mutation corpus (bit flips, byte runs, truncation, splices, header swaps over the attack seeds:
   malicious PDFs, polyglots, bombs, truncated images, hostile Markdown) through the REAL `NsjailRunner` per kind.
   Every job ends with one typed status (`ok` with manifest-validated outputs, `rejected`, `failed`, `timeout`,
   `violation`): the runner itself never raises, so no crash escapes the jail. Afterwards a jail still has no
   network (`EAFNOSUPPORT`/`ENETUNREACH`, no netlink) and the container has no interface but `lo`.
2. atheris (the `fuzz` group), coverage-instrumented (tests/security/fuzz/harness.py over the media-pipeline
   harness's target), per kind, seeded with the same corpus: no native crash, sanitizer report or hang (exit 0),
   and the corpus grew (coverage feedback works).
Counts per kind and status are written to `$NOEMA_SEC_ARTIFACTS/fuzz-summary.json`."""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import socket
import sys
import uuid
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import anyio
import pytest
from tests.security.artifacts import artifact_dir
from tests.unit.media_pipeline import media_fixtures as fx

from noema.core.protocols.sandbox import SandboxJob, SandboxKind
from noema.core.values import JsonValue
from noema.media.sandbox.limits import KIND_LIMITS
from noema.media.sandbox.nsjail import NsjailRunner

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.timeout(3000),
    pytest.mark.skipif(os.environ.get("NOEMA_MEDIA_IMAGE") != "1", reason="runs inside the media image"),
]
KINDS: Final[tuple[SandboxKind, ...]] = ("detect", "image.normalize", "pdf.sanitize", "pdf.render", "markdown.sanitize")
PARAMS: Final[dict[str, dict[str, JsonValue]]] = {
    "image.normalize": {"format": "PNG"},
    "pdf.render": {"long_edge_px": 256},
}
MUTANTS_PER_KIND: Final = int(os.environ.get("NOEMA_SEC_FUZZ_MUTANTS", "40"))
ATHERIS_RUNS: Final = int(os.environ.get("NOEMA_SEC_FUZZ_RUNS", "3000"))
STATUSES: Final = {"ok", "rejected", "failed", "timeout", "violation"}
REGRESSIONS: Final = Path(__file__).parent / "corpus"
HOSTILE_MD: Final = b"# t\n\n<script>alert(1)</script> [x](javascript:alert(1)) ![i](http://e/x.png)\n\n" + b"[" * 5000


def seeds() -> dict[str, bytes]:
    out = {
        "png": fx.png(32, 24),
        "jpeg": fx.jpeg(),
        "gif": fx.animated("GIF", 3),
        "pdf": fx.text_pdf(),
        "md": HOSTILE_MD,
        "pdf_zip": fx.pdf_zip_polyglot(),
        "png_pdf": fx.png_pdf_polyglot(),
        "zip_bomb_pdf": fx.zip_bomb_as_pdf(),
        "ztxt_bomb": fx.png_ztxt_bomb(8),
        "png_header_50k": fx.png_header_only(50_000, 50_000),
        "truncated_pdf": fx.truncated(fx.text_pdf()),
    }
    for name, (data, _facts) in fx.malicious_pdfs().items():
        out[f"pdf_{name}"] = data
    for path in sorted(REGRESSIONS.iterdir()):  # earlier findings, e.g. the pikepdf SystemError input
        out[f"regress_{path.stem}"] = path.read_bytes()
    return out


def mutate(rng: random.Random, data: bytes, other: bytes) -> bytes:
    buf = bytearray(data)
    for _ in range(rng.randint(1, 4)):
        op = rng.randrange(6)
        at = rng.randrange(len(buf)) if buf else 0
        if op == 0 and buf:  # bit flips
            buf[at] ^= 1 << rng.randrange(8)
        elif op == 1:  # an interesting run
            buf[at:at] = rng.choice([b"\xff" * 8, b"\x00" * 8, b"\x7f\xff\xff\xff", b"%%EOF", b"obj<<", b"\n" * 64])
        elif op == 2:  # truncation
            del buf[at:]
        elif op == 3:  # a splice from another seed
            start = rng.randrange(len(other)) if other else 0
            buf[at:at] = other[start : start + rng.randint(1, 4096)]
        elif op == 4:  # duplicate a chunk
            buf[at:at] = buf[at : at + rng.randint(1, 2048)]
        else:  # header swap
            buf[:8] = other[:8]
    return bytes(buf)


def corpus(kind: str, n: int) -> Iterator[tuple[str, bytes]]:
    pool = seeds()
    rng = random.Random(f"noema-sec-fuzz|{kind}")
    names = sorted(pool)
    for name in names:
        yield f"seed:{name}", pool[name]
    for i in range(n):
        a, b = rng.choice(names), rng.choice(names)
        yield f"mut:{i}:{a}+{b}", mutate(rng, pool[a], pool[b])


@pytest.fixture
def spool_root() -> Iterator[Path]:
    base = Path("/spool") if os.access("/spool", os.W_OK) else Path("/tmp")
    root = base / f"sec-fuzz-{uuid.uuid4().hex[:8]}"
    root.mkdir(mode=0o711)
    os.chmod(root, 0o711)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def findings(path: Path) -> list[str]:
    """libFuzzer's crash/timeout/oom artifacts."""
    return sorted(p.name for p in path.iterdir() if p.name.split("-")[0] in ("crash", "timeout", "oom", "leak"))


def job_id() -> str:
    return "sbx_" + uuid.uuid4().hex.upper()[:26]  # hex digits are Crockford base32


async def test_mutation_corpus_through_the_jail_never_escapes(spool_root: Path) -> None:
    from tests.integration.media_pipeline.test_media_image import EGRESS, probe

    runner = NsjailRunner("test", spool_root=spool_root, owner_uid=os.getuid(), owner_gid=os.getgid())
    counts: dict[str, Counter[str]] = {kind: Counter() for kind in KINDS}
    escaped: list[str] = []
    limiter = anyio.Semaphore(2)  # the runner's two jail slots

    async def one(kind: SandboxKind, name: str, data: bytes) -> None:
        async with limiter:
            src = spool_root / f"in-{uuid.uuid4().hex}.bin"
            src.write_bytes(data)
            try:
                result = await runner.run(SandboxJob(job_id(), kind, src, dict(PARAMS.get(kind, {}))),
                                          KIND_LIMITS[kind])  # fmt: skip
            except Exception as exc:  # a crash outside the jail: the gate's failure
                escaped.append(f"{kind} {name}: {type(exc).__name__}: {exc}"[:300])
                return
            finally:
                src.unlink(missing_ok=True)
            reason = result.rejection_code or str(result.facts.get("error") or result.facts.get("code") or "")
            counts[kind][f"{result.status}:{reason}" if reason else result.status] += 1
            if result.status not in STATUSES or (result.status != "ok" and result.outputs):
                escaped.append(f"{kind} {name}: {result.status} with {len(result.outputs)} outputs")

    async with anyio.create_task_group() as tg:
        for kind in KINDS:
            for name, data in corpus(kind, MUTANTS_PER_KIND):
                tg.start_soon(one, kind, name, data)
    status, code, jd = await probe(runner, spool_root, EGRESS)
    egress = json.loads((jd / "out" / "egress.txt").read_text())
    interfaces = sorted(name for _, name in socket.if_nameindex())
    summary = {"counts": {k: dict(v) for k, v in counts.items()}, "escaped": escaped, "egress": egress,
               "interfaces": interfaces, "mutants_per_kind": MUTANTS_PER_KIND}  # fmt: skip
    (artifact_dir() / "fuzz-summary.json").write_text(json.dumps(summary, indent=2))
    print("SE9-FUZZ-SUMMARY " + json.dumps(summary))  # the media container is removed after the run
    assert escaped == []
    assert all(sum(c.values()) == len(seeds()) + MUTANTS_PER_KIND for c in counts.values()), counts
    assert status == "ok", code
    assert egress["tcp"] in ("EAFNOSUPPORT", "ENETUNREACH") and egress["udp_dns"] in ("EAFNOSUPPORT", "ENETUNREACH")
    assert egress["netlink"] != "opened" and interfaces == ["lo"], (egress, interfaces)


@pytest.mark.parametrize("kind", KINDS)
async def test_atheris_findings_are_contained_by_the_jail(kind: SandboxKind, tmp_path: Path, spool_root: Path) -> None:
    """SEC-SBX-08 semantics ("crashes contained; findings filed"): libFuzzer stops at its first finding (a native
    crash, an exit or a hang of the in-process target); that input is then replayed through the real jail, which must
    end it with a typed status and no output. A clean run must show coverage feedback (the corpus grew)."""
    pytest.importorskip("atheris")
    seed_dir = tmp_path / "corpus"
    seed_dir.mkdir()
    for name, data in seeds().items():
        if not name.startswith("regress_"):  # a known finding would stop libFuzzer at its first input
            (seed_dir / name).write_bytes(data[:65536])
    argv = [sys.executable, "-m", "tests.security.fuzz.harness", kind]  # coverage-instrumented (see harness.py)
    argv += [f"-runs={ATHERIS_RUNS}", "-max_len=65536", "-max_total_time=300", "-timeout=30"]
    argv += [f"-artifact_prefix={tmp_path}/", str(seed_dir)]
    root = Path(__file__).parents[3]
    done = await anyio.run_process(argv, check=False, cwd=root)
    tail = done.stderr.decode(errors="replace")[-1500:]
    (artifact_dir() / f"atheris-{kind}.log").write_text(tail)
    found = findings(tmp_path)
    print(f"SE9-ATHERIS {kind} rc={done.returncode} findings={found} " + " | ".join(tail.strip().splitlines()[-3:]))
    if done.returncode == 0:
        final = re.search(r"DONE\s+cov: (\d+) ft: \d+ corp: (\d+)/", tail)
        assert final is not None and int(final.group(2)) > 1, tail  # coverage feedback: the corpus grew
        assert found == []
        return
    assert found, tail  # a non-zero exit always leaves its input
    runner = NsjailRunner("test", spool_root=spool_root, owner_uid=os.getuid(), owner_gid=os.getgid())
    for name in found:
        data = (tmp_path / name).read_bytes()
        result = await runner.run(SandboxJob(job_id(), kind, tmp_path / name, dict(PARAMS.get(kind, {}))),
                                  KIND_LIMITS[kind])  # fmt: skip
        print(f"SE9-ATHERIS-FINDING {kind} {name} {len(data)} B {data.hex()[:96]} -> jail {result.status} "
              f"{result.rejection_code or result.facts.get('error')}")  # fmt: skip
        assert result.status in STATUSES - {"ok"} and result.outputs == (), result
