"""`SecretStr`: a string that never prints (spec-09 §4).

A value constructed with `register=True` (at least 8 characters) is also recorded in a process-wide registry, so
the telemetry redactor replaces its exact value (`[REDACTED:secret_value]`). Loaders of platform and tenant secrets
register (`SecretsProvider`, credential brokers, DSNs); client-presented credentials (API key secrets) are never
registered: attacker-chosen strings would enter the redaction set. A registered value stays in the registry only
while a `SecretStr` holding it is alive (expired STS tokens drop out), so the set stays bounded on long-lived pods.
The registry holds values only in process memory.
"""

from __future__ import annotations

import hmac
import threading
import weakref
from collections import deque
from typing import Final

_MASK: Final = "**********"
_MIN_REGISTERED_LEN: Final = 8

_registry_lock = threading.Lock()
_registry: dict[str, int] = {}  # value -> number of live registered SecretStr objects holding it
_registry_version = 0
# Values whose holder was garbage-collected. Finalizers run at arbitrary points (also inside the locked sections
# below, in the same thread), so they only append here (atomic, lock-free); the locked functions apply the drops.
_dropped: deque[str] = deque()


def _apply_drops() -> None:
    """Under `_registry_lock`: forget the values of collected holders."""
    global _registry_version
    while _dropped:
        value = _dropped.popleft()
        count = _registry.get(value, 0)
        if count <= 1:
            if _registry.pop(value, None) is not None:
                _registry_version += 1
        else:
            _registry[value] = count - 1


def _register(owner: SecretStr, value: str) -> None:
    global _registry_version
    if len(value) < _MIN_REGISTERED_LEN:
        return
    with _registry_lock:
        _apply_drops()
        count = _registry.get(value, 0)
        _registry[value] = count + 1
        if count == 0:
            _registry_version += 1
    weakref.finalize(owner, _dropped.append, value)


def registered_secrets() -> tuple[int, frozenset[str]]:
    """`(version, values)` of every registered secret value (>= 8 characters) still held by a live `SecretStr`.
    The version changes whenever a value is added or dropped, so a redactor recompiles its pattern only then."""
    with _registry_lock:
        _apply_drops()
        return _registry_version, frozenset(_registry)


class SecretStr:
    """An immutable secret string. `str()`/`repr()`/`format()` give `**********`; read it with
    `get_secret_value()`. Equality is constant-time. `register=True` adds the value to the redaction registry for
    this object's lifetime (see the module docstring; never for client-presented credentials)."""

    __slots__ = ("__weakref__", "_value")

    def __init__(self, value: str, *, register: bool = False) -> None:
        if not isinstance(value, str):
            raise TypeError("SecretStr wraps a str")
        self._value: str = value
        if register:
            _register(self, value)

    def get_secret_value(self) -> str:
        """The plain value. Never log, persist or send it anywhere except its intended destination."""
        return self._value

    def __str__(self) -> str:
        return _MASK

    def __repr__(self) -> str:
        return f"SecretStr('{_MASK}')"

    def __format__(self, format_spec: str) -> str:
        return _MASK

    def __len__(self) -> int:
        return len(self._value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretStr):
            return NotImplemented
        return hmac.compare_digest(self._value.encode(), other._value.encode())

    def __hash__(self) -> int:
        return hash(("SecretStr", self._value))

    def __copy__(self) -> SecretStr:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> SecretStr:
        return self

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("SecretStr cannot be pickled")
