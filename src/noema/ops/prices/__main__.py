"""`python -m noema.ops.prices placeholder --config <yaml> [--region R ...] [--effective-from YYYY-MM-DD]`: print the
placeholder price catalog of a config (see `noema.ops.prices.placeholder`). Exit 2 on bad arguments or input."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

from noema.ops.prices.placeholder import placeholder_catalog, render


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m noema.ops.prices")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("placeholder", help="print a TBD price catalog for every model and region of a config")
    gen.add_argument("--config", type=Path, required=True)
    gen.add_argument("--region", action="append", help="a region (repeatable); default: the tenants' home regions")
    gen.add_argument("--effective-from", type=dt.date.fromisoformat, default=dt.datetime.now(dt.UTC).date())
    args = parser.parse_args(argv)
    try:
        config = yaml.safe_load(args.config.read_text())
        doc = placeholder_catalog(config, effective_from=args.effective_from, regions=args.region)
    except (OSError, yaml.YAMLError, ValueError, KeyError, TypeError, AttributeError) as exc:
        sys.stderr.write(f"error: {type(exc).__name__}: {exc}\n")
        return 2
    sys.stdout.write(render(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
