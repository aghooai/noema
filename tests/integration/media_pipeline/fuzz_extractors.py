"""SEC-SBX-08 (ADR-002): an atheris (libFuzzer-style) harness over the jailed extractors, one kind per run. Nightly,
inside the media image (the `fuzz` dependency group), never in `make check`:

    python -m tests.integration.media_pipeline.fuzz_extractors <kind> [libFuzzer flags: -runs=100000 -max_len=65536]

Each input is written to a scratch work dir and passed to the kind's extractor in-process, exactly as
`noema.media.extractors.entry` would; typed rejections and Python exceptions are the extractor's normal outcomes
(`rejected` / `failed`), so only native crashes (segfaults, aborts, sanitizer reports) and hangs surface as findings.
Seed corpora: the generated fixtures of `tests/unit/media_pipeline/media_fixtures.py`. The extractor and the libraries
it imports are loaded under `atheris.instrument_imports` (pikepdf excepted: see `UNINSTRUMENTED`), so libFuzzer gets
coverage feedback (Wave E: without it the nightly job was a blind random-bytes fuzzer)."""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final

from noema.core.protocols.sandbox import SandboxKind
from noema.core.values import JsonValue
from noema.media.extractors.common import Outputs, Rejection
from noema.media.extractors.entry import load_extractor
from noema.media.sandbox.limits import KINDS

# atheris aborts ("Unsupported call to As64 in exception handling") inside pikepdf's instrumented Python layer.
UNINSTRUMENTED: Final = ("pikepdf",)
PARAMS: Final[dict[str, dict[str, JsonValue]]] = {
    "image.normalize": {"format": "PNG"},
    "pdf.render": {"long_edge_px": 256},
    "ocr": {"pages": None},
}


def target(kind: SandboxKind) -> Callable[[bytes], None]:
    """The per-input function: one extractor run in a fresh work dir."""
    run, extractor = load_extractor(kind)
    params: dict[str, JsonValue] = dict(PARAMS.get(kind, {}))

    def one(data: bytes) -> None:
        work = Path(tempfile.mkdtemp(prefix="fuzz-"))
        try:
            (work / "in").mkdir()
            (work / "out").mkdir()
            (work / "in" / "input.bin").write_bytes(data)
            out = Outputs(kind, work / "out", extractor)
            try:
                run(work / "in" / "input.bin", out, params)
            except Rejection:
                out.write_manifest("corrupt")
            except Exception:  # the `failed` outcome of a real job: not a finding
                return
            out.write_manifest()
        finally:
            shutil.rmtree(work, ignore_errors=True)

    return one


def main(argv: list[str]) -> None:
    """`<kind> [libFuzzer flags]`; exits through atheris."""
    import atheris  # type: ignore[import-untyped]  # the fuzz dependency group (media-test image only)

    if not argv or argv[0] not in KINDS:
        raise SystemExit(f"usage: fuzz_extractors <kind> [flags]; kinds: {', '.join(KINDS)}")
    kind: SandboxKind = argv[0]
    with atheris.instrument_imports(exclude=list(UNINSTRUMENTED)):  # coverage feedback (Wave E: the run was blind)
        one = target(kind)
        one(b"")  # imports the extractor's lazily imported libraries inside the block
    atheris.Setup([sys.argv[0], *argv[1:]], one)
    atheris.Fuzz()


if __name__ == "__main__":
    main(sys.argv[1:])
