"""Builds the frozen `*Cfg` dataclasses from a validated, filled config dict (by reflection over the dataclass
type hints, so the dataclasses are the only description of the mapping)."""

from __future__ import annotations

import dataclasses
import types
from collections.abc import Mapping
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from noema.config.models import KEY_FIELD, NoemaConfig


class ConfigBuildError(ValueError):
    """The dict does not match the dataclasses (a loader bug: schema validation runs first)."""


_HINTS: dict[type[Any], dict[str, Any]] = {}


def _hints(cls: type[Any]) -> dict[str, Any]:
    hints = _HINTS.get(cls)
    if hints is None:
        hints = _HINTS[cls] = get_type_hints(cls)
    return hints


def _convert(tp: Any, value: object, path: str, key: str | None = None) -> object:
    origin = get_origin(tp)
    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        return _build(tp, value, path, key)
    if origin is Union or isinstance(tp, types.UnionType):
        args = get_args(tp)
        if value is None:
            if type(None) in args:
                return None
            raise ConfigBuildError(f"{path}: null is not allowed")
        rest = [a for a in args if a is not type(None)]
        if len(rest) == 1:
            return _convert(rest[0], value, path, key)
        return value  # a union of primitives (e.g. str | bool): schema-validated already
    if origin is tuple:
        item_tp = get_args(tp)[0]
        if not isinstance(value, list | tuple):
            raise ConfigBuildError(f"{path}: expected a list")
        return tuple(_convert(item_tp, v, f"{path}[{i}]") for i, v in enumerate(value))
    if origin is frozenset:
        item_tp = get_args(tp)[0]
        if not isinstance(value, list | tuple):
            raise ConfigBuildError(f"{path}: expected a list")
        return frozenset(_convert(item_tp, v, path) for v in value)
    if origin is Mapping or origin is dict:
        _, value_tp = get_args(tp)
        if not isinstance(value, Mapping):
            raise ConfigBuildError(f"{path}: expected an object")
        return types.MappingProxyType({k: _convert(value_tp, v, f"{path}.{k}", k) for k, v in value.items()})
    if origin is Literal:
        if value not in get_args(tp):
            raise ConfigBuildError(f"{path}: {value!r} is not one of {get_args(tp)}")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigBuildError(f"{path}: expected a number")
        return float(value)
    if tp in (int, str, bool):
        if not isinstance(value, tp) or (tp is int and isinstance(value, bool)):
            raise ConfigBuildError(f"{path}: expected {tp.__name__}")
        return value
    raise ConfigBuildError(f"{path}: unsupported type {tp!r}")


def _build(cls: type[Any], value: object, path: str, key: str | None) -> object:
    if not isinstance(value, Mapping):
        raise ConfigBuildError(f"{path}: expected an object")
    hints = _hints(cls)
    fields = {f.name: f for f in dataclasses.fields(cls)}
    kwargs: dict[str, object] = {}
    for name, f in fields.items():
        if f.metadata.get(KEY_FIELD):
            if key is None:
                raise ConfigBuildError(f"{path}: {cls.__name__} must be built from a mapping entry")
            kwargs[name] = key
        elif name in value:
            kwargs[name] = _convert(hints[name], value[name], f"{path}.{name}")
    unknown = set(value) - {n for n, f in fields.items() if not f.metadata.get(KEY_FIELD)}
    if unknown:
        raise ConfigBuildError(f"{path}: unexpected keys {sorted(unknown)}")
    try:
        return cls(**kwargs)
    except TypeError as exc:  # a required field is missing
        raise ConfigBuildError(f"{path}: {exc}") from exc


def build_config(filled: Mapping[str, object]) -> NoemaConfig:
    """Convert a schema-valid, default-filled config dict. Raises ConfigBuildError on a mismatch."""
    result = _build(NoemaConfig, filled, "$", None)
    assert isinstance(result, NoemaConfig)
    return result
