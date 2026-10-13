"""Wave E3: the dict-based SigV4 signer (`noema.adapters._sigv4.sign_post`) adds exactly the headers, in the same
order, that `botocore.auth.SigV4Auth.add_auth` adds to the same request: every Converse / ConverseStream golden in
six regions with and without a session token, then fuzzed requests (regions, ARN, ":" and arbitrary model ids,
session tokens incl. empty and whitespace, endpoint overrides with ports, IPv6, paths and queries, empty and binary
bodies, times). The lean path's full request equality with aiobotocore stays in test_lean_converse_wire.py."""

from __future__ import annotations

import datetime
import random
import string
from typing import Any

import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import ReadOnlyCredentials
from tests.fakes.config import example_snapshot
from tests.golden.wire import load_wire_goldens, request_from
from tests.unit.adapters.lean_reference import converse_model

from noema.adapters._converse_wire import converse_body, converse_url
from noema.adapters._sigv4 import sign_post, url_parts
from noema.adapters.wire import converse_params

REGIONS = ["us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1", "cn-north-1", "us-gov-west-1"]
GOLDENS = [
    g for g in load_wire_goldens() if g["op"] in ("converse", "converse-stream") and "error" not in g["expected"]
]
WHEN = datetime.datetime(2026, 10, 12, 23, 59, 59, tzinfo=datetime.UTC)


def botocore_headers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    akid: str,
    secret: str,
    token: str | None,
    region: str,
    service: str,
    url: str,
    body: bytes,
    now: datetime.datetime,
) -> list[tuple[str, str]]:
    monkeypatch.setattr("botocore.auth.get_current_datetime", lambda *a, **k: now)
    request = AWSRequest(method="POST", url=url, data=body, headers={"Content-Type": "application/json"})
    SigV4Auth(ReadOnlyCredentials(akid, secret, token), service, region).add_auth(request)
    return [(k, str(v)) for k, v in request.headers.items()]


def lean_headers(**kw: Any) -> list[tuple[str, str]]:
    akid, secret = kw.pop("akid"), kw.pop("secret")
    return list(sign_post(access_key=akid, secret_key=secret, **kw).items())


def _same(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> None:
    expected = botocore_headers(monkeypatch, **kw)
    assert lean_headers(**kw) == expected, kw


@pytest.mark.parametrize("token", [True, False], ids=["token", "no-token"])
@pytest.mark.parametrize("region", REGIONS)
def test_every_golden_is_signed_as_botocore_signs_it(region: str, token: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = converse_model().endpoint(region)
    for golden in GOLDENS:
        req = request_from(golden["request"])
        params = converse_params(req, example_snapshot().model(req.model_alias))
        media = any("text" not in b for m in params["messages"] for b in m["content"])
        url = converse_url(endpoint, params["modelId"], stream=golden["op"] == "converse-stream")
        _same(
            monkeypatch,
            akid="ASIAEXAMPLEEXAMPLE01",
            secret="c2VjcmV0LXNlY3JldC1zZWNyZXQ=",
            token="FwoGZXIvYXdzE" + "x" * 300 if token else None,
            region=region,
            service="bedrock",
            url=url,
            body=converse_body(params, media=media),
            now=WHEN,
        )


ENDPOINTS = [
    None,  # botocore's endpoint for the region
    "http://127.0.0.1:28181",
    "http://localhost:80",
    "https://bedrock-runtime.us-east-1.amazonaws.com:443",
    "https://EXAMPLE.internal:8443/prefix/v1",
    "http://[::1]:9000",
    "https://[fd00::12]",
    "https://vpce-0abc-de.bedrock-runtime.us-east-1.vpce.amazonaws.com",
]
MODEL_ID_CHARS = string.ascii_letters + string.digits + ":/.-_~%+ =&?#@!$'()*,;[]" + "éü漢\U0001f600"
FIXED_IDS = [
    "anthropic.claude-haiku-5-5-20260901-v1:0",
    "us.anthropic.claude-opus-5-5",
    "arn:aws:bedrock:us-east-1:111122223333:inference-profile/us.anthropic.claude-haiku-5-5",
    "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/abc123",
    "arn:aws-cn:bedrock:cn-north-1::foundation-model/anthropic.claude-haiku-5-5-20260901-v1:0",
    "..",
    ".",
    "a/../b",
]


def _token(rng: random.Random) -> str | None:
    kind = rng.randrange(5)
    if kind == 0:
        return None
    if kind == 1:
        return ""
    alphabet = string.ascii_letters + string.digits + "+/="
    token = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 1200)))
    return token if kind != 4 else f"  {token[:20]}  \t {token[20:]} "  # whitespace SigV4 collapses


def _case(rng: random.Random, model: Any) -> dict[str, Any]:
    region = rng.choice([*REGIONS, "eu-central-2", "ap-southeast-7", "xx-test-9", "mx-central-1"])
    endpoint = rng.choice(ENDPOINTS) or model.endpoint(region if region != "xx-test-9" else "us-east-1")
    if rng.random() < 0.15:
        endpoint += rng.choice(["?b=2&a=1", "?x", "?a=1&a=0&=z"])
    if rng.random() < 0.5:
        model_id = rng.choice(FIXED_IDS)
    else:
        model_id = "".join(rng.choice(MODEL_ID_CHARS) for _ in range(rng.randrange(1, 80)))
    url = converse_url(endpoint, model_id, stream=rng.random() < 0.5)
    if "?" in endpoint:  # an override with a query string: the path goes before it
        base, _, query = endpoint.partition("?")
        url = converse_url(base, model_id, stream=rng.random() < 0.5) + "?" + query
    body_kind = rng.randrange(3)
    body = b"" if body_kind == 0 else rng.randbytes(rng.randrange(1, 9000)) if body_kind == 1 else b'{"a":"\\u00e9"}'
    seconds = rng.randrange(0, 20 * 365 * 86400)
    return {
        "akid": "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(20)),
        "secret": "".join(rng.choice(string.ascii_letters + string.digits + "+/") for _ in range(40)),
        "token": _token(rng),
        "region": region,
        "service": rng.choice(["bedrock", "bedrock", "bedrock-runtime"]),
        "url": url,
        "body": body,
        "now": datetime.datetime(2015, 1, 1, tzinfo=datetime.UTC) + datetime.timedelta(seconds=seconds),
    }


@pytest.mark.parametrize("seed", range(4))
def test_fuzzed_requests_are_signed_as_botocore_signs_them(seed: int, monkeypatch: pytest.MonkeyPatch) -> None:
    rng, model = random.Random(seed), converse_model()
    for _ in range(500):
        _same(monkeypatch, **_case(rng, model))


def test_the_url_cache_returns_what_a_fresh_derivation_returns() -> None:
    url = "https://Bedrock-Runtime.us-east-1.amazonaws.com:443/model/arn%3Aaws%3Ab/converse?z=1&a=2"
    first = url_parts(url)
    url_parts.cache_clear()
    assert (
        url_parts(url)
        == first
        == ("bedrock-runtime.us-east-1.amazonaws.com", "/model/arn%253Aaws%253Ab/converse", "a=2&z=1")
    )
