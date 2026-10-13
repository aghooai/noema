"""Request authentication as the mock sees it (spec-10 §2.2 "Auth", ADR-001 §6).

The mock cannot verify signatures. It parses what a real client sends and records the access key id (AKID), so
tenant-isolation tests can compare it with the key the broker issued:
- SigV4: `Authorization: AWS4-HMAC-SHA256 Credential=<AKID>/<date>/<region>/<service>/aws4_request, ...` plus
  `X-Amz-Security-Token` (STS credentials);
- Mantle short-term bearer: `Authorization: Bearer bedrock-api-key-<base64(presigned URL + "&Version=1")>`
  (aws-bedrock-token-generator); the AKID and the session token are read from the presigned query string.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import parse_qs, urlsplit

SIGV4_PREFIX: Final = "AWS4-HMAC-SHA256 "
BEARER_PREFIX: Final = "Bearer "
SHORT_TERM_PREFIX: Final = "bedrock-api-key-"
Scheme = Literal["sigv4", "bearer_short_term", "bearer_other", "none"]


@dataclass(frozen=True, slots=True)
class AuthInfo:
    """What the request presented. `akid` is None when it could not be read."""

    scheme: Scheme
    akid: str | None = None
    region: str | None = None
    service: str | None = None
    session_token: bool = False

    def as_record(self) -> dict[str, object]:
        return {
            "scheme": self.scheme,
            "akid": self.akid,
            "region": self.region,
            "service": self.service,
            "session_token": self.session_token,
        }


def _credential_scope(credential: str) -> tuple[str | None, str | None, str | None]:
    parts = credential.split("/")
    if len(parts) != 5 or parts[4] != "aws4_request" or not parts[0]:
        return None, None, None
    return parts[0], parts[2], parts[3]


def _sigv4(value: str, headers: Mapping[str, str]) -> AuthInfo:
    fields = dict(item.strip().split("=", 1) for item in value[len(SIGV4_PREFIX) :].split(",") if "=" in item)
    akid, region, service = _credential_scope(fields.get("Credential", ""))
    return AuthInfo("sigv4", akid, region, service, bool(headers.get("x-amz-security-token")))


def _short_term(token: str) -> AuthInfo:
    try:
        url = base64.b64decode(token[len(SHORT_TERM_PREFIX) :], validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return AuthInfo("bearer_other")
    query = parse_qs(urlsplit("https://" + url).query)
    if query.get("Action") != ["CallWithBearerToken"]:
        return AuthInfo("bearer_other")
    akid, region, service = _credential_scope(query.get("X-Amz-Credential", [""])[0])
    if akid is None:
        return AuthInfo("bearer_other")
    return AuthInfo("bearer_short_term", akid, region, service, bool(query.get("X-Amz-Security-Token")))


def parse_auth(headers: Mapping[str, str]) -> AuthInfo:
    """Classify the request's credentials (`headers` are lower-cased)."""
    value = headers.get("authorization", "")
    if value.startswith(SIGV4_PREFIX):
        return _sigv4(value, headers)
    if value.startswith(BEARER_PREFIX):
        token = value[len(BEARER_PREFIX) :].strip()
        if token.startswith(SHORT_TERM_PREFIX):
            return _short_term(token)
        return AuthInfo("bearer_other")
    return AuthInfo("none")


SECRET_HEADERS: Final = frozenset({"authorization", "x-amz-security-token", "x-api-key", "api-key"})


def redacted_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """The request headers without credentials (what `GET /_mock/requests` shows)."""
    return {k: v for k, v in headers.items() if k not in SECRET_HEADERS}
