"""The vendored contract tree (tools/sync_contracts.py) and the bundled OpenAPI operation table (IMPL-041, IMPL-066)."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest

from noema.contracts.bundle import BUNDLE_PATH, VENDOR_DIR, Bundle, default_bundle, pointer
from noema.core.errors import ERROR_TABLE, ErrorCode

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[3]


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_contracts", REPO / "tools" / "sync_contracts.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vendored_tree_is_identical_to_the_canonical_tree() -> None:
    tool = _tool()
    if not tool.DEFAULT_SOURCE.is_dir():
        pytest.skip(f"canonical contracts not present at {tool.DEFAULT_SOURCE}")
    assert tool.differences(tool.DEFAULT_SOURCE, VENDOR_DIR) == []


def test_sync_copies_bytes_removes_extras_and_ignores_caches(tmp_path: Path) -> None:
    tool = _tool()
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "examples").mkdir(parents=True)
    (src / "a.json").write_bytes(b'{"a": 1}\n')
    (src / "examples" / "b.yaml").write_bytes(b"b: 2\r\n")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (dst / "stale").mkdir(parents=True)
    (dst / "stale" / "old.txt").write_text("old")
    assert tool.sync(src, dst) == 3
    assert (dst / "examples" / "b.yaml").read_bytes() == b"b: 2\r\n"
    assert not (dst / "stale").exists()
    assert not (dst / "__pycache__").exists()
    assert tool.differences(src, dst) == []
    assert tool.sync(src, dst) == 0
    (dst / "a.json").write_bytes(b'{"a": 2}\n')
    (dst / "extra.ts").write_text("x")
    (dst / "examples" / "b.yaml").unlink()
    assert tool.differences(src, dst) == [
        "missing in _vendor: examples/b.yaml",
        "not in canonical tree: extra.ts",
        "content differs: a.json",
    ]


def test_check_mode_exit_codes(tmp_path: Path) -> None:
    tool = _tool()
    if not tool.DEFAULT_SOURCE.is_dir():
        pytest.skip("canonical contracts not present")
    assert tool.main(["--check"]) == 0
    copy = tmp_path / "contracts"
    shutil.copytree(tool.DEFAULT_SOURCE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "openapi.yaml").write_bytes((copy / "openapi.yaml").read_bytes() + b"\n")
    assert tool.main(["--check", "--source", str(copy)]) == 1


def test_bundle_has_every_operation() -> None:
    ops = default_bundle().operations()
    assert len(ops) == 25
    assert len({o.operation_id for o in ops}) == 25
    unauthenticated = {o.operation_id for o in ops if not o.authenticated}
    assert unauthenticated == {"healthz", "readyz"}
    assert BUNDLE_PATH.is_file()


def test_x_noema_extensions_on_every_authenticated_operation() -> None:
    roles = {"tenant_admin", "developer", "reviewer", "end_user", "auditor"}
    for op in default_bundle().operations():
        if not op.authenticated:
            continue
        assert op.error_codes, op.operation_id
        assert set(op.error_codes) <= {c.value for c in ErrorCode}, op.operation_id
        assert op.roles and set(op.roles) <= roles, op.operation_id


def test_statuses_implied_by_error_codes_equal_the_declared_error_statuses() -> None:
    for op in default_bundle().operations():
        if not op.authenticated:
            continue
        implied = {str(ERROR_TABLE[ErrorCode(c)].status) for c in op.error_codes if ERROR_TABLE[ErrorCode(c)].status}
        declared = {s for s in op.responses if s[0] in "45"}
        assert implied == declared, op.operation_id


def test_every_response_declares_x_request_id() -> None:
    for op in default_bundle().operations():
        if op.authenticated:
            assert all("x-request-id" in h for h in op.response_headers.values()), op.operation_id


def test_pointer_and_resolve() -> None:
    b = default_bundle()
    assert pointer("paths", "/v1/turns", "post") == "/paths/~1v1~1turns/post"
    assert pointer("a~b") == "/a~0b"
    resolved = b.resolve({"$ref": "#/components/schemas/ErrorCode"})
    assert resolved["type"] == "string"
    with pytest.raises(ValueError, match="unsupported"):
        b.resolve({"$ref": "other.yaml#/x"})
    with pytest.raises(ValueError, match="OpenAPI"):
        Bundle.load(VENDOR_DIR / "examples" / "platform.example.yaml")
