"""SE9 atheris runner with coverage: the media-pipeline harness (`tests.integration.media_pipeline.fuzz_extractors`)
runs its per-input target without instrumenting any code, so libFuzzer gets no coverage feedback and its corpus never
grows ("Is the code instrumented for coverage?"). This runner reuses that target and instruments every loaded
Python module first (`atheris.instrument_all()`), inside the media image only:

    python -m tests.security.fuzz.harness <kind> [libFuzzer flags] <corpus dir>
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> None:
    import atheris  # type: ignore[import-untyped]  # the fuzz dependency group (media-test image only)
    from tests.integration.media_pipeline.fuzz_extractors import target

    one = target(argv[0])  # type: ignore[arg-type]  # validated by load_extractor
    atheris.instrument_all()
    atheris.Setup([sys.argv[0], *argv[1:]], one)
    atheris.Fuzz()


if __name__ == "__main__":
    main(sys.argv[1:])
