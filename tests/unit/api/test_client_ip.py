"""The client IP for the Authenticator (spec-09 §2 step 5, NOEMA_TRUSTED_PROXIES)."""

from __future__ import annotations

import pytest

from noema.api.middleware.client_ip import client_ip, parse_trusted_proxies

pytestmark = pytest.mark.unit
PROXIES = parse_trusted_proxies("10.0.0.0/8, 192.168.1.1 fd00::/8")


@pytest.mark.parametrize(
    ("peer", "xff", "expected"),
    [
        ("203.0.113.9", None, "203.0.113.9"),
        ("203.0.113.9", "1.2.3.4", "203.0.113.9"),  # untrusted peer: X-Forwarded-For ignored
        ("10.1.2.3", None, "10.1.2.3"),
        ("10.1.2.3", "1.2.3.4", "1.2.3.4"),
        ("10.1.2.3", "6.6.6.6, 1.2.3.4, 10.9.9.9", "1.2.3.4"),  # rightmost untrusted hop; spoofed left part ignored
        ("10.1.2.3", "10.5.5.5,192.168.1.1", "10.5.5.5"),  # all trusted: the leftmost
        ("10.1.2.3", "garbage, 10.5.5.5", ""),  # malformed hop: unknown client
        ("fd00::1", "2001:db8::7", "2001:db8::7"),
        ("testclient", "1.2.3.4", ""),  # not an IP (in-process transports)
        (None, None, ""),
    ],
)
def test_client_ip(peer: str | None, xff: str | None, expected: str) -> None:
    assert client_ip(peer, xff, PROXIES) == expected


def test_without_trusted_proxies_the_peer_is_the_client() -> None:
    assert client_ip("10.1.2.3", "1.2.3.4", ()) == "10.1.2.3"
    assert parse_trusted_proxies(None) == ()
    assert parse_trusted_proxies(["10.0.0.0/8"]) == PROXIES[:1]


def test_ipv4_mapped_peers_and_hops_are_their_ipv4_address() -> None:
    """Wave E edge fix 26: on a dual-stack listener IPv4 peers arrive as ::ffff:a.b.c.d; they match IPv4 proxy
    CIDRs (as `authn` normalizes them for ip_allowlists), and the client IP is reported in its IPv4 form."""
    proxies = parse_trusted_proxies("10.0.0.0/8")
    assert client_ip("::ffff:10.0.0.5", "203.0.113.9", proxies) == "203.0.113.9"
    assert client_ip("::ffff:10.0.0.5", "203.0.113.9, ::ffff:10.0.0.7", proxies) == "203.0.113.9"
    assert client_ip("::ffff:203.0.113.9", None, proxies) == "203.0.113.9"
    assert client_ip("::ffff:10.0.0.5", "::ffff:198.51.100.4", proxies) == "198.51.100.4"
