"""Configuration snapshots (spec-02 §2, spec-05).

A `ConfigSnapshot` is immutable: one per activated config version, with defaults filled and every rule checked.
A turn pins one snapshot at ingress. The `*Cfg` types are frozen dataclasses owned by `noema.config.models`
(imported for typing only, so `core` keeps no runtime dependency on `config`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from noema.core.principal import TenantRef
from noema.core.protocols.adapters import StreamTimeouts
from noema.core.values import Effort

if TYPE_CHECKING:
    from noema.config.models import (
        DecisionProfileCfg,
        GenerationProfileCfg,
        LabelingCfg,
        ModelCfg,
        PlatformCfg,
        RoutingCfg,
        TemplateCfg,
        TenantCfg,
    )
    from noema.config.prices import PriceCatalog


class ConfigSnapshot(Protocol):
    """One activated config version. Every lookup is sync and in memory."""

    @property
    def version(self) -> int:
        """`config_version` of the activated row."""
        ...

    @property
    def digest(self) -> str:
        """`"sha256:" + hex` of the canonical JSON of the filled config."""
        ...

    @property
    def routing(self) -> RoutingCfg: ...

    @property
    def labeling(self) -> LabelingCfg: ...

    @property
    def platform(self) -> PlatformCfg:
        """`platform.*` with every default filled (spec-05 §1.1)."""
        ...

    @property
    def prices(self) -> PriceCatalog:
        """The catalog named by `platform.cost.prices_ref`; empty when it is absent (spec-05 §2)."""
        ...

    def tenant(self, tenant_id: str) -> TenantCfg:
        """Raises `NoemaError(not_found)` for an unknown tenant."""
        ...

    def tenant_ref(self, tenant_id: str) -> TenantRef:
        """Raises `NoemaError(not_found)` for an unknown tenant."""
        ...

    def model(self, alias: str) -> ModelCfg:
        """Raises `NoemaError(not_found)` for an unknown alias."""
        ...

    def template(self, ref: str) -> TemplateCfg:
        """`ref` is `name@version`. Raises `NoemaError(not_found)` for an unknown template."""
        ...

    def decision_profile(self, profile_id: str) -> DecisionProfileCfg:
        """The active version of the profile (IMPL-031). Raises `NoemaError(not_found)` for an unknown id."""
        ...

    def generation_profile(self, profile_id: str) -> GenerationProfileCfg:
        """Raises `NoemaError(not_found)` for an unknown id."""
        ...

    def route_timeouts(self, model_alias: str, effort: Effort | None) -> StreamTimeouts:
        """Per-attempt stream timeouts for the member's alias and effective effort (ROUTE_TIMEOUTS_V1, spec-05
        §1.4; IMPL-015, IMPL-084)."""
        ...


class ConfigRegistry(Protocol):
    """The process's view of the active config."""

    def current(self) -> ConfigSnapshot:
        """The active snapshot."""
        ...

    def get(self, version: int) -> ConfigSnapshot:
        """A pinned version: the current one or one of the previous 2 activated versions; any other version raises
        `NoemaError(not_found)`."""
        ...

    def on_change(self, cb: Callable[[ConfigSnapshot], None]) -> None:
        """Register `cb`; it runs (sync) after `current()` changed, with the new snapshot."""
        ...
