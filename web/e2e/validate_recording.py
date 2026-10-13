"""Check every API response the dev proxy recorded during a Playwright run against the contract.

The proxy (NOEMA_UI_RECORD=<file>) appends one JSON line per proxied /v1 response: method, path, status, headers and
the base64 body of JSON and SSE responses. Each line is matched to its operation by method and path template and
validated with the server's ContractValidator (schemas, required headers, error envelopes, decision invariants, SSE
frames). Usage: python web/e2e/validate_recording.py <recording.jsonl> (from the repo root, in the project venv).
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

from noema.contracts.bundle import default_bundle
from noema.contracts.validate import ContractValidator, ContractViolation


def _matchers() -> list[tuple[str, re.Pattern[str], str]]:
    out = []
    for op in default_bundle().operations():
        pattern = "^" + re.sub(r"\{[^/}]+\}", "[^/]+", re.escape(op.path).replace(r"\{", "{").replace(r"\}", "}")) + "$"
        out.append((op.method, re.compile(pattern), op.operation_id))
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stdout.write(__doc__ + "\n")
        return 2
    path = Path(argv[1])
    if not path.exists():
        sys.stdout.write(f"{path}: no recording (the UI made no API call through the proxy)\n")
        return 1
    validator = ContractValidator()
    matchers = _matchers()
    failures: list[str] = []
    checked: dict[str, int] = {}
    for n, line in enumerate(path.read_text().splitlines(), 1):
        entry = json.loads(line)
        method, url = entry["method"], entry["path"]
        ops = [op_id for m, rx, op_id in matchers if m == method and rx.match(url)]
        if len(ops) != 1:
            failures.append(f"line {n}: {method} {url} matches {len(ops)} operations")
            continue
        try:
            validator.check_response(ops[0], entry["status"], entry["headers"], base64.b64decode(entry["body_b64"]))
        except ContractViolation as e:
            failures.append(f"line {n}: {method} {url} {entry['status']}: {e}")
        checked[ops[0]] = checked.get(ops[0], 0) + 1
    for f in failures:
        sys.stdout.write(f"CONTRACT VIOLATION {f}\n")
    summary = ", ".join(f"{k} {v}" for k, v in sorted(checked.items()))
    sys.stdout.write(f"validated {sum(checked.values())} responses ({summary}); {len(failures)} failure(s)\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
