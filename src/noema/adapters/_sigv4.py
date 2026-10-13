"""A dict-based SigV4 signer for the lean Converse path (Wave E3): the headers `botocore.auth.SigV4Auth.add_auth`
adds to a `POST` with a bytes body and the one `Content-Type` header, byte for byte, without botocore's
`AWSRequest`, `HTTPHeaders` (email.message) and per-call `urlsplit` (E sprint A: ~65 us per call, the largest
single piece of a member call's CPU; tests/unit/adapters/test_sigv4.py compares every golden and fuzzed requests
with SigV4Auth).

What SigV4Auth does for such a request, and so this module:
- `X-Amz-Date` = the UTC time as `%Y%m%dT%H%M%SZ`; `X-Amz-Security-Token` when the credentials carry a token;
- the canonical request: `POST`, the URL path through botocore's `normalize_url_path` then `quote(safe="/~")`
  (the model id's percent-encoding is encoded again), the URL's query (split on `&`, `=`; sorted; not re-encoded),
  the signed headers lower-cased and sorted with their whitespace collapsed (`content-type`, `host`, `x-amz-date`,
  `x-amz-security-token`; `host` = the URL's lower-cased host, its port unless the scheme's default, IPv6 in
  brackets), the payload's SHA-256 (payload signing is on for every scheme: no `payload_signing_enabled` context);
- the string to sign over `<date>/<region>/<service>/aws4_request` and the derived HMAC-SHA256 key chain;
- `Authorization: AWS4-HMAC-SHA256 Credential=<akid>/<scope>, SignedHeaders=<names>, Signature=<hex>`.
The per-URL parts (host, canonical path and query) are cached (a few URLs per region and model).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
from functools import lru_cache
from typing import Final
from urllib.parse import quote, urlsplit

from botocore.utils import is_valid_ipv6_endpoint_url, normalize_url_path

ALGORITHM: Final = "AWS4-HMAC-SHA256"
TIMESTAMP_FORMAT: Final = "%Y%m%dT%H%M%SZ"  # botocore.auth.SIGV4_TIMESTAMP
EMPTY_SHA256: Final = hashlib.sha256(b"").hexdigest()
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_URL_CACHE: Final = 4096


def utcnow() -> datetime.datetime:
    """The signing clock (botocore's `get_current_datetime`: UTC; tests freeze it)."""
    return datetime.datetime.now(datetime.UTC)


def _canonical_query(query: str) -> str:
    """botocore's `_canonical_query_string_url`: the pairs sorted, as they are in the URL."""
    if not query:
        return ""
    pairs = []
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        pairs.append((key, value))
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


@lru_cache(maxsize=_URL_CACHE)
def url_parts(url: str) -> tuple[str, str, str]:
    """(host header, canonical path, canonical query) of `url`, as SigV4Auth derives them."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if is_valid_ipv6_endpoint_url(url):
        host = f"[{host}]"
    port = parts.port
    if port is not None and port != _DEFAULT_PORTS.get(parts.scheme):
        host = f"{host}:{port}"
    path = quote(normalize_url_path(parts.path), safe="/~")
    return host, path, _canonical_query(parts.query)


def _trim(value: str) -> str:
    return " ".join(value.split())


def sign_post(
    *,
    access_key: str,
    secret_key: str,
    token: str | None,
    region: str,
    service: str,
    url: str,
    body: bytes,
    content_type: str = "application/json",
    now: datetime.datetime | None = None,
) -> dict[str, str]:
    """The request headers after SigV4Auth.add_auth, in its order: `Content-Type`, `X-Amz-Date`,
    `X-Amz-Security-Token` (with a token), `Authorization`."""
    timestamp = (now or utcnow()).strftime(TIMESTAMP_FORMAT)
    date = timestamp[:8]
    host, path, query = url_parts(url)
    headers = {"Content-Type": content_type, "X-Amz-Date": timestamp}
    if token:
        headers["X-Amz-Security-Token"] = token
        signed = "content-type;host;x-amz-date;x-amz-security-token"
        canonical_headers = (
            f"content-type:{_trim(content_type)}\nhost:{_trim(host)}\nx-amz-date:{timestamp}\n"
            f"x-amz-security-token:{_trim(token)}\n"
        )
    else:
        signed = "content-type;host;x-amz-date"
        canonical_headers = f"content-type:{_trim(content_type)}\nhost:{_trim(host)}\nx-amz-date:{timestamp}\n"
    payload = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
    canonical = f"POST\n{path}\n{query}\n{canonical_headers}\n{signed}\n{payload}"
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = f"{ALGORITHM}\n{timestamp}\n{scope}\n{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    key = hmac.digest(f"AWS4{secret_key}".encode(), date.encode("utf-8"), "sha256")
    key = hmac.digest(key, region.encode("utf-8"), "sha256")
    key = hmac.digest(key, service.encode("utf-8"), "sha256")
    key = hmac.digest(key, b"aws4_request", "sha256")
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"
    )
    return headers
