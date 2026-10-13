"""Typed access to the vendored, executable contract modules: `_vendor/check_config.py`, `_vendor/check_examples.py`
and the tolerant client models `_vendor/python_sdk_types.py`. They are loaded from their files (the vendored tree is
not a package, so it stays a byte copy) and read their schemas and examples relative to themselves."""

from __future__ import annotations

import functools
import importlib.util
import sys
from collections.abc import Callable, Mapping
from types import ModuleType
from typing import Any, Final, Literal, cast

from noema.contracts.bundle import VENDOR_DIR

VendoredModule = Literal["check_config", "check_examples", "python_sdk_types"]
_MODULE_PREFIX: Final = "noema.contracts._vendor."


@functools.cache
def load(name: VendoredModule) -> ModuleType:
    """Import a vendored checker once per process."""
    path = VENDOR_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(_MODULE_PREFIX + name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # pydantic resolves the SDK types' string annotations through sys.modules
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    return module


def function(name: VendoredModule, attr: str) -> Callable[..., Any]:
    """A function of a vendored checker."""
    fn = getattr(load(name), attr)
    if not callable(fn):
        raise TypeError(f"{name}.{attr} is not callable")
    return cast("Callable[..., Any]", fn)


def constant(name: VendoredModule, attr: str) -> Any:
    """A module-level constant of a vendored checker (e.g. check_examples.BANDS)."""
    return getattr(load(name), attr)


def config_schema_errors(cfg: Mapping[str, Any]) -> list[str]:
    """JSON Schema errors of a config, formatted exactly like `check_config.check` (one line per error)."""
    validator = constant("check_config", "VALIDATOR")
    return [
        f"schema: {'/'.join(map(str, e.absolute_path))}: {e.message[:120]}"
        for e in sorted(validator.iter_errors(cfg), key=lambda e: list(map(str, e.absolute_path)))
    ]


def config_semantic_errors(cfg: Mapping[str, Any]) -> list[str]:
    """`check_config.semantic_errors(cfg)`: the design rules plus P1-C1-C4 (spec-08 §14.4)."""
    errs = function("check_config", "semantic_errors")(cfg)
    return [str(e) for e in errs]


def config_negative_cases() -> Mapping[str, Any]:
    """`check_config.NEGATIVE`: name -> mutate or (mutate, expected message fragment)."""
    cases: Mapping[str, Any] = constant("check_config", "NEGATIVE")
    return cases
