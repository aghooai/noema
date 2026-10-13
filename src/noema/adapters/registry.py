"""`AdapterRegistry` (spec-02 §4.1): the adapter of `models.<alias>.adapter` in the current snapshot."""

from __future__ import annotations

from collections.abc import Mapping

from noema.core.protocols.adapters import Adapter
from noema.core.protocols.config import ConfigRegistry


class AdapterRegistryImpl:
    """Implements `AdapterRegistry` over one adapter per API kind (`bedrock-converse`, `bedrock-mantle-chat`).
    `for_model` raises `NoemaError(not_found)` for an unknown alias and KeyError for a kind without an adapter
    (a wiring bug)."""

    def __init__(self, config: ConfigRegistry, adapters: Mapping[str, Adapter]) -> None:
        self._config = config
        self._adapters = dict(adapters)

    def for_model(self, alias: str) -> Adapter:
        return self._adapters[self._config.current().model(alias).adapter]

    @property
    def adapters(self) -> Mapping[str, Adapter]:
        """The adapters by API kind."""
        return self._adapters
