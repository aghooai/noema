"""tools/check_ownership.py and OWNERS.yaml (PLAN Wave B: every changed path maps to exactly one owning role set)."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[3]


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_ownership", REPO / "tools" / "check_ownership.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


tool = _tool()
owners = tool.Owners.load()

EXPECTED = {
    "pyproject.toml": ("platform",),
    "uv.lock": ("platform",),
    "make/lint.mk": ("platform",),
    "src/noema/core/protocols/storage.py": ("platform",),
    "src/noema/contracts/_vendor/openapi.yaml": ("platform",),
    "src/noema/config/loader.py": ("platform",),
    "tests/unit/config/test_loader.py": ("platform",),
    "src/noema/db/uow.py": ("db-schema",),
    "src/noema/db/alembic/versions/0001_initial.py": ("db-schema",),
    "src/noema/db/repos/sessions.py": ("db-repos",),
    "src/noema/api/middleware/auth.py": ("api",),
    "src/noema/api/sse_frame.py": ("api",),
    "src/noema/api/routes/turns.py": ("orchestrator", "api"),
    "src/noema/api/routes/openai_compat.py": ("decision", "api"),
    "src/noema/api/routes/admin_readonly.py": ("labeling", "api"),
    "src/noema/composition.py": ("integrator", "fakes-telemetry"),
    "tests/fakes/scenario.py": ("fakes-telemetry",),
    "tests/contract_internal/test_clock_deadline.py": ("fakes-telemetry",),
    "deploy/docker/api.Dockerfile": ("infra", "integrator"),
    "deploy/compose/docker-compose.yml": ("platform", "infra", "integrator"),
    "src/noema/identity/sts.py": ("identity",),
    "tests/live/test_smoke.py": ("adapters",),
    "tools/mockbedrock/server.py": ("qa-tools",),
    "src/noema/decision/compiler.py": ("decision",),
    "src/noema/streaming/event_log.py": ("generation",),
    "tests/integration/streaming/test_sse.py": ("generation",),
    "src/noema/turns/prompt/preamble.py": ("orchestrator",),
    "src/noema/media/views.py": ("media-negotiation",),
    "src/noema/media/intake.py": ("media-pipeline",),
    "src/noema/media/extractors/pdf.py": ("media-pipeline",),
    "src/noema/ops/admission/gcra.py": ("ops-admission",),
    "src/noema/ops/cost.py": ("ops-ledger",),
    "src/noema/ops/outbox/drain.py": ("ops-ledger",),
    "src/noema/labeling/calibrators/platt.py": ("labeling",),
    "sdk/typescript/src/index.ts": ("sdk",),
    "web/src/App.tsx": ("ui",),
    "src/noema/entrypoints/jobs.py": ("integrator",),
    "tests/security/test_sts.py": ("qa-security",),
    "docs/modules/decision.md": ("decision",),
    "docs/impl-spec/02-interfaces.md": ("wave-a",),
    ".orchestration/requests/db-schema.md": ("db-schema",),
    ".orchestration/requests/unknown-role.md": ("orchestrator-control",),
    ".orchestration/PLAN.md": ("orchestrator-control",),
    "src/noema/decision/__init__.py": ("decision", "platform"),
    "src/noema/ops/__init__.py": ("ops-ledger", "ops-admission", "platform"),
}


@pytest.mark.parametrize(("path", "expected"), sorted(EXPECTED.items()))
def test_owners_of(path: str, expected: tuple[str, ...]) -> None:
    assert owners.owners_of(path) == expected


def test_unowned_paths() -> None:
    assert owners.owners_of("random/file.txt") == ()
    assert owners.owners_of("src/noema/unknown.py") == ()


def test_brace_expansion_and_globs() -> None:
    assert tool.expand_braces("a/{b,c}/{d,e}.py") == ["a/b/d.py", "a/b/e.py", "a/c/d.py", "a/c/e.py"]
    assert tool.expand_braces("x") == ["x"]
    rx = re.compile(tool.glob_to_regex("src/**/*.py"))
    assert rx.fullmatch("src/a.py") and rx.fullmatch("src/a/b/c.py")
    assert not rx.fullmatch("src/a/b.txt")
    star = re.compile(tool.glob_to_regex("a/*.md"))
    assert star.fullmatch("a/x.md") and not star.fullmatch("a/b/x.md")


def test_report_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert tool.main(["--paths", "src/noema/core/ids.py", "--role", "platform"]) == 0
    assert tool.main(["--paths", "src/noema/core/ids.py", "src/noema/db/uow.py", "--role", "platform"]) == 1
    assert tool.main(["--paths", "nowhere/x"]) == 1
    assert tool.main(["--paths", "src/noema/core/ids.py", "--role", "nobody"]) == 2
    assert tool.main(["--owner-of", "src/noema/api/routes/files.py"]) == 0
    assert capsys.readouterr().out.strip().endswith("media-pipeline, api")


def test_unknown_role_in_owners_file_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "OWNERS.yaml"
    bad.write_text("roles: {a: {}}\nrules:\n  - {pattern: 'x', owners: [b]}\n")
    with pytest.raises(ValueError, match="unknown roles"):
        tool.Owners.load(bad)


def test_changed_paths_parses_porcelain_z(monkeypatch: pytest.MonkeyPatch) -> None:
    out = b" M src/a.py\0?? new/file.txt\0R  new/name.py\0old/name.py\0 D gone.py\0"

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 0, stdout=out)

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    assert tool.changed_paths() == ["gone.py", "new/file.txt", "new/name.py", "old/name.py", "src/a.py"]
