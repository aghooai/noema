"""Loads a platform config into a `ConfigSnapshotImpl` (spec-05 §3).

Order: YAML (or an already-decoded mapping) -> JSON Schema (rev 6) -> fill defaults (schema `default`s, the
data-class table per field, `labeling.acceptance`, the P1-T1 decision-profile fill) -> the vendored
`check_config.semantic_errors` -> the P1 rules -> the price catalog named by `platform.cost.prices_ref` -> digest.
A failure at any step rejects the whole config with every error collected so far (`ConfigError.errors`).
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml

from noema.config._build import build_config
from noema.config.models import DEFAULT_DECISION_PROFILE
from noema.config.prices import EMPTY_CATALOG, PriceCatalog, PriceCatalogError, load_price_file, parse_price_catalog
from noema.config.registry import ConfigSnapshotImpl
from noema.config.rules import NoemaEnv, p1_errors, semantic_errors
from noema.contracts import vendor

# platform.data_classes defaults (spec-05 §1.1; 03:L392, 04:L147); overrides merge per field.
DATA_CLASS_DEFAULTS: Final[Mapping[str, Mapping[str, object]]] = {
    "public": {"store_content": "full", "retain_days": 90, "l2_cache": True},
    "internal": {"store_content": "full", "retain_days": 90, "l2_cache": True},
    "confidential": {"store_content": "redacted", "retain_days": 30, "l2_cache": False},
    "restricted": {"store_content": "metadata_only", "retain_days": 7, "l2_cache": False},
}

# db:<version> -> the platform.price_catalog body (prices.yaml as JSON), or None when the version doesn't exist.
type PriceCatalogSource = Callable[[int], Mapping[str, Any] | None]


class ConfigError(ValueError):
    """The config was rejected; `errors` lists every problem found."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__(f"config rejected ({len(errors)} errors): " + "; ".join(errors[:5]))
        self.errors: tuple[str, ...] = tuple(errors)


def parse_source(source: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    """A deep, plain copy of the config: YAML text/bytes are parsed with the safe loader. Raises ConfigError."""
    if isinstance(source, Mapping):
        return copy.deepcopy(dict(source))
    try:
        doc = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise ConfigError([f"yaml: {str(exc)[:200]}"]) from None
    if not isinstance(doc, dict):
        raise ConfigError(["yaml: the config must be a mapping"])
    return doc


def _deref(schema: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any]:
    while "$ref" in schema:
        node: Any = root
        for token in str(schema["$ref"]).removeprefix("#/").split("/"):
            node = node[token]
        schema = node
    return schema


def _has_defaults(schema: Mapping[str, Any], root: Mapping[str, Any]) -> bool:
    for sub in schema.get("properties", {}).values():
        resolved = _deref(sub, root)
        if "default" in resolved or (resolved.get("type") == "object" and _has_defaults(resolved, root)):
            return True
    return False


def _fill_schema_defaults(node: Any, schema: Mapping[str, Any], root: Mapping[str, Any]) -> None:
    schema = _deref(schema, root)
    if isinstance(node, dict):
        props: Mapping[str, Any] = schema.get("properties", {})
        for key, sub in props.items():
            resolved = _deref(sub, root)
            if key not in node:
                if "default" in resolved:
                    node[key] = copy.deepcopy(resolved["default"])
                elif resolved.get("type") == "object" and _has_defaults(resolved, root):
                    node[key] = {}
            if key in node:
                _fill_schema_defaults(node[key], resolved, root)
        extra = schema.get("additionalProperties")
        if isinstance(extra, Mapping):
            for key, value in node.items():
                if key not in props:
                    _fill_schema_defaults(value, extra, root)
    elif isinstance(node, list) and isinstance(schema.get("items"), Mapping):
        for value in node:
            _fill_schema_defaults(value, schema["items"], root)


def fill_defaults(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """A filled deep copy of a schema-valid config (spec-05 §3 step 3)."""
    filled = copy.deepcopy(dict(cfg))
    schema: Mapping[str, Any] = vendor.constant("check_config", "SCHEMA")
    _fill_schema_defaults(filled, schema, schema)
    classes = filled.setdefault("platform", {}).setdefault("data_classes", {})
    for name, defaults in DATA_CLASS_DEFAULTS.items():
        classes[name] = {**defaults, **classes.get(name, {})}
    for tenant in filled["tenants"].values():
        tenant.setdefault("defaults", {}).setdefault("decision_profile", DEFAULT_DECISION_PROFILE)
    return filled


def digest_of(filled: Mapping[str, Any]) -> str:
    """`"sha256:" + hex` of the canonical JSON (sorted keys, no whitespace, UTF-8) of the filled config."""
    canonical = json.dumps(filled, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def referenced_price_version(source: str | bytes | Mapping[str, Any]) -> int | None:
    """The `<n>` of `platform.cost.prices_ref: db:<n>`, so a caller can fetch the catalog row before loading."""
    ref = parse_source(source).get("platform", {}).get("cost", {}).get("prices_ref")
    if isinstance(ref, str) and ref.startswith("db:") and ref[3:].isdigit():
        return int(ref[3:])
    return None


def validate_config(source: str | bytes | Mapping[str, Any], *, env: NoemaEnv) -> tuple[dict[str, Any], list[str]]:
    """Steps 1-5: `(filled config, errors)`; errors empty = valid. Schema errors stop before the rules."""
    raw = parse_source(source)
    errors = vendor.config_schema_errors(raw)
    if errors:
        return raw, errors
    filled = fill_defaults(raw)
    errors = vendor.config_schema_errors(filled)  # the fill must keep the config schema-valid
    if errors:
        return filled, [f"defaults: {e}" for e in errors]
    errors = semantic_errors(filled) + p1_errors(filled, env=env)
    return filled, errors


def _load_prices(filled: Mapping[str, Any], prices: PriceCatalogSource | None, base_dir: Path | None) -> PriceCatalog:
    ref = filled["platform"]["cost"].get("prices_ref")
    if ref is None:
        return EMPTY_CATALOG
    kind, _, arg = ref.partition(":")
    try:
        if kind == "file":
            path = Path(arg)
            return load_price_file(path if path.is_absolute() or base_dir is None else base_dir / path)
        version = int(arg)
        body = prices(version) if prices is not None else None
        if body is None:
            raise ConfigError([f"platform.cost.prices_ref: price catalog db:{version} does not exist (P1-P1)"])
        return parse_price_catalog(body, version=version)
    except PriceCatalogError as exc:
        raise ConfigError([f"platform.cost.prices_ref {ref}: {e}" for e in exc.errors]) from None


def load_config(
    source: str | bytes | Mapping[str, Any],
    *,
    env: NoemaEnv,
    prices: PriceCatalogSource | None = None,
    base_dir: Path | None = None,
) -> ConfigSnapshotImpl:
    """Validate, fill and freeze a config (steps 1-6). `prices` resolves `db:<n>` catalogs (fetch the row of
    `referenced_price_version(source)` first); `file:` paths are relative to `base_dir`. Raises ConfigError."""
    filled, errors = validate_config(source, env=env)
    if errors:
        raise ConfigError(errors)
    catalog = _load_prices(filled, prices, base_dir)
    return ConfigSnapshotImpl(
        config=build_config(filled), digest=digest_of(filled), prices=catalog, body=MappingProxyType(filled)
    )


def load_config_file(path: Path, *, env: NoemaEnv, prices: PriceCatalogSource | None = None) -> ConfigSnapshotImpl:
    """`load_config` over a YAML file; `file:` price paths are relative to the file's directory."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError([f"cannot read {path}: {type(exc).__name__}"]) from None
    return load_config(text, env=env, prices=prices, base_dir=path.parent)
