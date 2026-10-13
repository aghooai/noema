"""The jailed extractor entry point (ADR-002 §3, §5):

    python -I -m noema.media.extractors.entry <kind> [<params JSON>] [--work /work]

reads `<work>/in/input.bin`, writes the outputs and `manifest.json` to `<work>/out/` and exits 0 (also for a typed
rejection, which the manifest carries). Any other failure exits 1 with a one-line, content-free reason on stderr.
Params are validated again here (closed per kind). The process has no network, no credentials and no writable path
but `/work/out` and `/tmp`.
"""

from __future__ import annotations

import json
import sys
import warnings
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

from noema.core.protocols.sandbox import SandboxKind
from noema.core.values import JsonValue
from noema.media.extractors.common import Outputs, Rejection
from noema.media.sandbox.limits import KINDS, BadParams, check_params

Extractor = Callable[[Path, Outputs, Mapping[str, JsonValue]], None]
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2


def load_extractor(kind: SandboxKind) -> tuple[Extractor, str]:
    """The extractor function of `kind` and its manifest name (imported lazily: one kind's libraries only)."""
    if kind == "detect":
        from noema.media.extractors import detect

        return detect.run, detect.EXTRACTOR
    if kind == "image.normalize":
        from noema.media.extractors import image

        return image.run, image.EXTRACTOR
    if kind == "pdf.sanitize":
        from noema.media.extractors import pdf_sanitize

        return pdf_sanitize.run, pdf_sanitize.EXTRACTOR
    if kind == "pdf.render":
        from noema.media.extractors import pdf_render

        return pdf_render.run, pdf_render.EXTRACTOR
    if kind == "pdf.markdown":
        from noema.media.extractors import pdf_markdown

        return pdf_markdown.run, pdf_markdown.EXTRACTOR
    if kind == "ocr":
        from noema.media.extractors import ocr

        return ocr.run, ocr.EXTRACTOR
    from noema.media.extractors import markdown

    return markdown.run, markdown.EXTRACTOR


def _args(argv: Sequence[str]) -> tuple[SandboxKind, dict[str, JsonValue], Path]:
    args = list(argv)
    work = Path("/work")
    if "--work" in args:
        i = args.index("--work")
        work = Path(args[i + 1])
        del args[i : i + 2]
    if not 1 <= len(args) <= 2 or args[0] not in KINDS:
        raise BadParams("usage: entry <kind> [<params JSON>] [--work DIR]")
    raw = json.loads(args[1]) if len(args) == 2 else {}
    if not isinstance(raw, dict):
        raise BadParams("params must be a JSON object")
    kind: SandboxKind = args[0]
    return kind, check_params(kind, raw), work


def main(argv: Sequence[str] | None = None) -> int:
    """Run one job; the exit status is the result (see the module docstring)."""
    try:
        kind, params, work = _args(sys.argv[1:] if argv is None else argv)
    except (BadParams, ValueError, IndexError) as exc:
        sys.stderr.write(f"bad_request: {type(exc).__name__}\n")
        return EXIT_USAGE
    warnings.simplefilter("ignore")  # library chatter must not reach stderr; bombs are re-enabled where needed
    run, extractor = load_extractor(kind)
    out = Outputs(kind, work / "out", extractor)
    try:
        run(work / "in" / "input.bin", out, params)
    except Rejection as rejection:
        out.write_manifest(rejection.code)
        return EXIT_OK
    except Exception as exc:  # content-free reason only: the exception text may quote the input
        sys.stderr.write(f"failed: {type(exc).__name__}\n")
        return EXIT_FAILED
    out.write_manifest()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
