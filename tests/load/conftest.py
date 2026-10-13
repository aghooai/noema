"""Fixtures of the stack gates in tests/load (`make qa-gates`, make/qa-gates.mk; tests/load/gatekit.py).

The gate modules (GATES) run only with NOEMA_QA_GATES=1 (the make targets), so a plain `pytest tests/load` never
drives another agent's e2e stack; `test_harness.py` (pure) always runs. Session: the gate config (tenant limits
lifted, the DeepSeek-only L5 profile) active on both replicas. The measured numbers of every gate that ran go to
NOEMA_QA_GATES_RESULTS (JSON, merged into an existing file).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import orjson
import pytest
from tests.e2e.conftest import Stack, mock, stack
from tests.load import gatekit

__all__ = ["gate_config", "mock", "stack"]

GATES: Final = frozenset({"test_holdback.py", "test_backup_tail.py", "test_replay.py", "test_hier_deadline.py"})


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if gatekit.ENABLED:
        return
    skip = pytest.mark.skip(reason="stack gate: run it with `make qa-gates` (NOEMA_QA_GATES=1, a stack of its own)")
    for item in items:
        path = Path(str(item.fspath))
        if path.name in GATES and path.is_relative_to(Path(__file__).parent):
            item.add_marker(skip)


@pytest.fixture(scope="session")
async def gate_config(stack: Stack) -> int:
    """The gate config version (activated once per session)."""
    return await gatekit.ensure_gate_config(stack)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    target = os.environ.get("NOEMA_QA_GATES_RESULTS")
    if not target or not gatekit.RESULTS:
        return
    path = Path(target)
    merged = orjson.loads(path.read_bytes()) if path.exists() else {}
    merged.update(gatekit.RESULTS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(merged, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
