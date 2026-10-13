"""`python -m noema.entrypoints.jobs --kinds labeling.*,session.delete,retention.sweep` (spec-01 §4).

The jobs process: the job worker for the identity (`session.delete`), labeling (`labeling.*`) and retention
(`retention.sweep`) handlers, plus what `composition.start(services, "jobs")` runs (the periodic-job scheduler, the
job reaper, the notify bridge, the outbox writer and drainer, the config watcher, the calibrator refresh and the
breaker / retry-budget sync). It never claims `media.process` or `file.purge` (the media-worker's kinds).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Final

from noema.entrypoints._worker import HandlerFactory, parse_args, run

DEFAULT_KINDS: Final = "labeling.*,session.delete,retention.sweep"


def factories() -> tuple[HandlerFactory, ...]:
    from noema.identity.jobs import make_handlers as identity_handlers
    from noema.labeling import make_handlers as labeling_handlers
    from noema.ops.retention import make_handlers as retention_handlers

    return identity_handlers, labeling_handlers, retention_handlers


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv, prog="noema.entrypoints.jobs", default_kinds=DEFAULT_KINDS)
    return run("jobs", factories(), args)


if __name__ == "__main__":
    sys.exit(main())
