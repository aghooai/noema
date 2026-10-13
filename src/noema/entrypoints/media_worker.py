"""`python -m noema.entrypoints.media_worker [--kinds media.process,file.purge]` (spec-01 §4, ADR-002).

The media-worker process (Z2, no parser libraries): the `media.process` (file runs and watchdog checks) and
`file.purge` (purges and delayed sweeps) workers of `noema.media.worker.make_handlers`, which may run nowhere else.
The sandbox runner comes from the process settings (`NOEMA_SANDBOX_BACKEND`: `remote` on `NOEMA_SANDBOX_SOCKET`,
the only backend allowed in staging/prod; `bwrap` dev/test; `inproc_rlimit` test) and is checked with
`assert_backend_allowed(backend, env, process="media-worker")` before the worker claims anything.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Final

from noema.entrypoints._worker import HandlerFactory, parse_args, run

DEFAULT_KINDS: Final = "media.process,file.purge"


def factories() -> tuple[HandlerFactory, ...]:
    from noema.media.worker import make_handlers

    return (make_handlers,)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv, prog="noema.entrypoints.media_worker", default_kinds=DEFAULT_KINDS)
    return run("media-worker", factories(), args)


if __name__ == "__main__":
    sys.exit(main())
