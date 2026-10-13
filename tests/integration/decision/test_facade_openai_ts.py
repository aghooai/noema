"""Gate Q2, TS half (spec-10 §3; sdk request 3): the unmodified `openai` npm client of
`sdk/typescript/test/facade.openai.live.test.ts` against a fresh in-process facade (127.0.0.1, port 0), with the
scenarios of `test_facade_openai_sdk.py` plus one rule under which every call of a question fails (`unavailable`).
The client talks only to the local server; Noema never calls OpenAI (D1). Skipped when npx or vitest is missing."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from tests.contract_internal.kits import principal
from tests.integration.decision.server import real_clock_world, serve
from tests.integration.decision.test_facade_openai_sdk import FlakyAdmission, rules
from tests.unit.decision.app import KEY, make

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
SDK = ROOT / "sdk" / "typescript"
GONE = "GONE check"
HAS_VITEST = shutil.which("npx") is not None and any(
    (d / "node_modules" / ".bin" / "vitest").exists() for d in (SDK, ROOT)
)


@pytest.mark.skipif(not HAS_VITEST, reason="needs npx and vitest (make npm-ci)")
@pytest.mark.timeout(120)
async def test_facade_with_the_official_openai_npm_client(tmp_path: Path) -> None:
    ew = real_clock_world()
    ew.use([{"match": {"prompt_contains": GONE}, "fault": {"kind": "http_5xx"}, "latency_ms": 5}, *rules()])
    app, svc, _ = make(ew)
    svc.admission = FlakyAdmission(ew.world.admission)  # type: ignore[attr-defined]
    svc.authn.register("flaky-key", replace(principal(), key_id="flaky"))
    report = tmp_path / "vitest.json"
    cmd = ["npx", "--no-install", "vitest", "run", "test/facade.openai.live.test.ts", "--reporter=json"]
    async with serve(app) as base:
        env = {
            **os.environ,
            "NOEMA_FACADE_BASE_URL": base,
            "NOEMA_FACADE_API_KEY": KEY,
            "NOEMA_FACADE_THROTTLED_API_KEY": "flaky-key",
            "NOEMA_FACADE_UNAVAILABLE_INSTRUCTIONS": GONE,
        }
        done = await anyio.run_process([*cmd, f"--outputFile={report}"], cwd=SDK, env=env, check=False)
    output = (done.stdout + done.stderr).decode(errors="replace")[-6000:]
    assert done.returncode == 0 and report.exists(), output
    summary = json.loads(report.read_text())
    assert summary["numFailedTests"] == 0, output
    assert summary["numPassedTests"] == summary["numTotalTests"] > 0, output  # ran, not skipped for a missing variable
    assert app.state.contract_violations == []
