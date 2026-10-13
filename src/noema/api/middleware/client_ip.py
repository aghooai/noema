"""The client IP handed to the Authenticator (spec-09 §2 step 5, spec-05 §5 `NOEMA_TRUSTED_PROXIES`).

The client IP is the TCP peer, unless the peer is a trusted proxy; then it is the rightmost `X-Forwarded-For` entry
that is not itself a trusted proxy (entries left of it are client-controlled and ignored). IPv4-mapped IPv6
addresses (a dual-stack listener) count as their IPv4 address, as `identity.authn` treats them for ip_allowlists.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterable, Sequence
from typing import Final

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
TRUSTED_PROXIES_ENV: Final = "NOEMA_TRUSTED_PROXIES"


def parse_trusted_proxies(value: str | Iterable[str] | None) -> tuple[Network, ...]:
    """Parse a CIDR list (comma- or whitespace-separated string, or an iterable). Raises ValueError on an invalid
    entry (a startup error)."""
    if value is None:
        return ()
    items = value.replace(",", " ").split() if isinstance(value, str) else list(value)
    return tuple(ipaddress.ip_network(item.strip(), strict=False) for item in items if item.strip())


def trusted_proxies_from_env() -> tuple[Network, ...]:
    """`NOEMA_TRUSTED_PROXIES` of the process environment (empty when unset)."""
    return parse_trusted_proxies(os.environ.get(TRUSTED_PROXIES_ENV))


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address, an IPv4-mapped IPv6 one (`::ffff:a.b.c.d`, a dual-stack listener) as its IPv4 address."""
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _trusted(addr: ipaddress.IPv4Address | ipaddress.IPv6Address, proxies: Sequence[Network]) -> bool:
    return any(addr.version == net.version and addr in net for net in proxies)


def client_ip(peer: str | None, forwarded_for: str | None, proxies: Sequence[Network]) -> str:
    """The client IP for `peer` (the ASGI `client` host) and the joined `X-Forwarded-For` values. Returns "" when
    the client is unknown: no peer address (e.g. an in-process transport) or a malformed forwarded hop."""
    peer_addr = _ip(peer) if peer else None
    if peer_addr is None:
        return ""
    if not proxies or not forwarded_for or not _trusted(peer_addr, proxies):
        return str(peer_addr)
    current = peer_addr
    for entry in reversed(forwarded_for.split(",")):
        addr = _ip(entry)
        if addr is None:
            return ""  # a malformed hop: unknown client (fails an ip_allowlist closed)
        current = addr
        if not _trusted(addr, proxies):
            return str(addr)
    return str(current)
