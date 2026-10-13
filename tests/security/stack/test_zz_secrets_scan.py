"""SE12 (06 §9 "Secrets"; SEC-RED-1 / SEC-STS-12 on the stack): after the e2e suite and this security suite ran
against the security stack (`make qa-security` runs it last: test_zz_), every container log line, the Valkey dump, the
Postgres data dump and the Prometheus text hold 0 matches of the spec-09 §4 patterns and 0 occurrences of any
known secret (API and operator keys and their secret halves, the presented bad keys' secret halves, the peppers,
the DSN passwords, the ExternalIds). The wire mock's request log (what reached the models) holds none of the
Noema secrets (its AKID fields are the mock's own record of the STS keys). Findings are written masked."""

from __future__ import annotations

from typing import Final

import orjson
import pytest
from tests.e2e.conftest import Stack
from tests.security.artifacts import artifact_dir, write_jsonl
from tests.security.secret_patterns import PATTERNS, scan
from tests.security.stack.capture import capture, moto_secrets, secret_values

from noema.core.values import JsonValue

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(300)]
# The mock records the SigV4 AKID and the broker's session per request by design (spec-10 §2.2): not Noema telemetry.
MOCK_KINDS: Final = tuple(k for k in PATTERNS if k not in ("aws_key_id", "sigv4_param", "aws_session_token"))


async def test_no_secret_in_logs_stores_or_metrics(stack: Stack) -> None:
    files = await capture(stack, artifact_dir() / "capture")
    presented_path = artifact_dir() / "secrets" / "presented-keys.json"
    presented = orjson.loads(presented_path.read_bytes()) if presented_path.exists() else {}
    moto = await moto_secrets(stack)
    values = secret_values(stack, moto, presented)
    assert len(values) >= 40 and any(k.startswith("pepper:") for k in values), sorted(values)
    # positive control: the scanner finds every key, operator key and ExternalId in the bootstrap state itself
    in_state = {f.kind for f in scan("state", orjson.dumps(stack.state, option=orjson.OPT_INDENT_2).decode(), values)}
    assert {k for k in in_state if k.startswith("value:key:")} == {f"value:{k}" for k in values if k.startswith("key:")}
    assert {"api_key", "operator_key", "external_id"} <= in_state
    findings = []
    sizes: dict[str, JsonValue] = {}
    for name, path in files.items():
        text = path.read_text(errors="replace")
        sizes[name] = len(text.splitlines())
        kinds = MOCK_KINDS if name.startswith("mock") else tuple(PATTERNS)
        findings += scan(name, text, values, kinds=kinds)
    rows: list[JsonValue] = [
        {"source": f.source, "line": f.line, "kind": f.kind, "excerpt": f.excerpt} for f in findings
    ]
    write_jsonl("secrets-findings.jsonl", [*rows, {"lines": sizes, "values": len(values)}])
    assert int(str(sizes["compose.log"])) > 1000  # the run's traffic is in the capture
    assert rows == []
