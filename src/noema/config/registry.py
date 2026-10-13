"""`ConfigSnapshotImpl` (the frozen snapshot) and `InMemoryConfigRegistry` (spec-02 §2).

The registry holds the current snapshot and the previous 2 activated versions; the config watcher (composition
root) loads each newly activated version with `config.loader` and calls `activate`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from noema.config.models import (
    DecisionProfileCfg,
    GenerationProfileCfg,
    LabelingCfg,
    ModelCfg,
    NoemaConfig,
    PlatformCfg,
    RoutingCfg,
    TemplateCfg,
    TenantCfg,
    route_timeouts_v1,
)
from noema.config.prices import PriceCatalog
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import TenantRef
from noema.core.protocols.adapters import StreamTimeouts
from noema.core.protocols.config import ConfigSnapshot
from noema.core.values import Effort

PINNED_VERSIONS: Final = 3  # the current version plus the previous 2


def _not_found(kind: str, key: str) -> NoemaError:
    return NoemaError(ErrorCode.NOT_FOUND, f"Unknown {kind} {key!r}.")


@dataclass(frozen=True, slots=True)
class ConfigSnapshotImpl:
    """One activated config version (implements `ConfigSnapshot`). `config` is the full typed config and `body`
    the filled JSON-ready dict the digest was computed over."""

    config: NoemaConfig
    digest: str
    prices: PriceCatalog
    body: Mapping[str, Any]

    @property
    def version(self) -> int:
        return self.config.config_version

    @property
    def routing(self) -> RoutingCfg:
        return self.config.routing

    @property
    def labeling(self) -> LabelingCfg:
        return self.config.labeling

    @property
    def platform(self) -> PlatformCfg:
        return self.config.platform

    def tenant(self, tenant_id: str) -> TenantCfg:
        try:
            return self.config.tenants[tenant_id]
        except KeyError:
            raise _not_found("tenant", tenant_id) from None

    def tenant_ref(self, tenant_id: str) -> TenantRef:
        t = self.tenant(tenant_id)
        return TenantRef(tenant_id=tenant_id, aws_account_id=t.aws_account_id, home_region=t.home_region)

    def model(self, alias: str) -> ModelCfg:
        try:
            return self.config.models[alias]
        except KeyError:
            raise _not_found("model", alias) from None

    def template(self, ref: str) -> TemplateCfg:
        try:
            return self.config.templates[ref]
        except KeyError:
            raise _not_found("template", ref) from None

    def decision_profile(self, profile_id: str) -> DecisionProfileCfg:
        try:
            return self.config.decision_profiles[profile_id]
        except KeyError:
            raise _not_found("decision profile", profile_id) from None

    def generation_profile(self, profile_id: str) -> GenerationProfileCfg:
        try:
            return self.config.generation_profiles[profile_id]
        except KeyError:
            raise _not_found("generation profile", profile_id) from None

    def route_timeouts(self, model_alias: str, effort: Effort | None) -> StreamTimeouts:
        return route_timeouts_v1(model_alias, effort)


class InMemoryConfigRegistry:
    """Implements `ConfigRegistry`. Thread-safe; `on_change` callbacks run synchronously in `activate`, after
    `current()` changed; a failing callback is isolated (its exception is collected and re-raised as an
    ExceptionGroup after every callback ran)."""

    def __init__(self, initial: ConfigSnapshot) -> None:
        self._lock = threading.Lock()
        self._window: list[ConfigSnapshot] = [initial]  # most recently activated first
        self._callbacks: list[Callable[[ConfigSnapshot], None]] = []

    def current(self) -> ConfigSnapshot:
        return self._window[0]

    def get(self, version: int) -> ConfigSnapshot:
        for snap in tuple(self._window):
            if snap.version == version:
                return snap
        raise NoemaError(ErrorCode.NOT_FOUND, f"Config version {version} is not pinned in this process.")

    def on_change(self, cb: Callable[[ConfigSnapshot], None]) -> None:
        with self._lock:
            self._callbacks.append(cb)

    def activate(self, snapshot: ConfigSnapshot) -> bool:
        """Make `snapshot` current; returns False (and changes nothing) if it is already current with the same
        digest. Re-activating a pinned version (rollback) moves it to the front."""
        with self._lock:
            head = self._window[0]
            if head.version == snapshot.version and head.digest == snapshot.digest:
                return False
            rest = [s for s in self._window if s.version != snapshot.version]
            self._window = [snapshot, *rest][:PINNED_VERSIONS]
            callbacks = tuple(self._callbacks)
        failures: list[Exception] = []
        for cb in callbacks:
            try:
                cb(snapshot)
            except Exception as exc:  # isolate subscribers from each other
                failures.append(exc)
        if failures:
            raise ExceptionGroup("config on_change callbacks failed", failures)
        return True
