"""E sprint A: the lean Converse and ConverseStream requests are byte for byte the signed requests aiobotocore
sends (URL with the percent-encoded model id, endpoint per region from botocore's rules, rest-json body, SigV4
headers), for every Converse golden and for edge requests (non-ASCII and escaped text, image and document blobs,
temperature, stop sequences, effort, ARN model ids, no session token, an endpoint override)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from tests.fakes.config import example_snapshot
from tests.golden.wire import load_wire_goldens, request_from
from tests.unit.adapters.lean_reference import converse_model, creds, freeze, reference

from noema.adapters._converse_wire import converse_body, converse_url, signed_headers
from noema.adapters.wire import check_request, converse_params
from noema.core.protocols.adapters import ModelRequest
from noema.core.values import DocumentBlock, ImageBlock, Message, TextBlock

SIGNED = ("Content-Type", "X-Amz-Date", "X-Amz-Security-Token", "Authorization")
CONVERSE_GOLDENS = [
    g for g in load_wire_goldens() if g["op"] in ("converse", "converse-stream") and "error" not in g["expected"]
]


def _edge_requests() -> list[tuple[str, ModelRequest]]:
    text = 'héllo "quoted" \\ </untrusted_input>   \U0001f600 \x00 tab\t'
    image = ImageBlock("png", bytes(range(256)) * 3, 8, 8)
    document = DocumentBlock("pdf", "report (v2) [final]", b"%PDF-1.7 \xff\xfe", 2)
    base = ModelRequest(
        "opus-5.5", ("sys one", "sys two"), (Message("user", (TextBlock(text),)),), 64, "generation", "default"
    )
    return [
        ("text", base),
        ("media", replace(base, messages=(Message("user", (TextBlock("see"), image, document)),))),
        (
            "history",
            replace(base, messages=(*base.messages, Message("assistant", (TextBlock("A"),)), base.messages[0])),
        ),
        ("glm_controls", replace(base, model_alias="glm-5.3", temperature=0.3, stop=("\n\n", "END"), effort="low")),
        ("effort", replace(base, effort="low", system=())),
    ]


def _cases() -> list[tuple[str, ModelRequest]]:
    cases = [(g["name"], request_from(g["request"])) for g in CONVERSE_GOLDENS]
    return cases + _edge_requests()


async def _compare(
    monkeypatch: pytest.MonkeyPatch,
    req: ModelRequest,
    *,
    region: str = "us-east-1",
    endpoint: str | None = None,
    token: bool = True,
    model_id: str | None = None,
    stream: bool = False,
) -> None:
    freeze(monkeypatch)
    model = example_snapshot().model(req.model_alias)
    if model_id is not None:
        model = replace(model, id=model_id)
    check_request(req, model, "bedrock-converse")
    params = converse_params(req, model)
    async with reference(region, endpoint=endpoint, token=token) as ref:
        if stream:
            ref.stream_chunks = []
            await ref.client.converse_stream(**params)
        else:
            await ref.converse(params)
    ((url, headers, body),) = ref.sent
    wire = converse_model()
    lean_url = converse_url(endpoint or wire.endpoint(region), model.id, stream=stream)
    media = any("text" not in b for m in params["messages"] for b in m["content"])
    lean_body = converse_body(params, media=media)
    lean = signed_headers(creds(region, token=token), lean_url, lean_body)
    assert (lean_url, lean_body) == (url, body)
    for name in SIGNED:
        assert lean.get(name) == headers.get(name), name
    signed = headers["Authorization"].split("SignedHeaders=", 1)[1].split(",", 1)[0]
    assert signed == ("content-type;host;x-amz-date;x-amz-security-token" if token else "content-type;host;x-amz-date")
    assert lean["amz-sdk-request"] == headers["amz-sdk-request"] == "attempt=1"
    assert set(lean) - set(SIGNED) == {"User-Agent", "amz-sdk-invocation-id", "amz-sdk-request"}


@pytest.mark.parametrize("stream", [False, True], ids=["converse", "converse-stream"])
@pytest.mark.parametrize(("name", "req"), _cases(), ids=[n for n, _ in _cases()])
async def test_the_signed_request_is_the_one_botocore_sends(
    name: str, req: ModelRequest, stream: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _compare(monkeypatch, req, stream=stream)


@pytest.mark.parametrize(
    "region", ["us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1", "cn-north-1", "us-gov-west-1"]
)
async def test_endpoints_and_signing_region_follow_botocore(region: str, monkeypatch: pytest.MonkeyPatch) -> None:
    await _compare(monkeypatch, _edge_requests()[0][1], region=region)


COLON_IDS = [
    "anthropic.claude-haiku-5-5-20260901-v1:0",
    "us.anthropic.claude-haiku-5-5-20260901-v1:0",
    "arn:aws:bedrock:us-east-1:111122223333:inference-profile/us.anthropic.claude-haiku-5-5",
    "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/abc123",
    "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-5-5-20260901-v1:0",
]


@pytest.mark.parametrize("stream", [False, True], ids=["converse", "converse-stream"])
@pytest.mark.parametrize("model_id", [*COLON_IDS, "a b+c%d~e_f.g-h"])
async def test_model_ids_are_percent_encoded_as_botocore_does(
    model_id: str, stream: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _compare(monkeypatch, _edge_requests()[0][1], model_id=model_id, stream=stream)


@pytest.mark.parametrize("stream", [False, True], ids=["converse", "converse-stream"])
@pytest.mark.parametrize("model_id", COLON_IDS)
def test_a_colon_model_id_goes_on_the_wire_as_signed(model_id: str, stream: bool) -> None:
    """io finding 1: the transport sends `yarl.URL(url, encoded=True)` (`_unary.AiohttpUnary._send`), so the
    request-line path keeps botocore's `%3A` / `%2F`; a str URL would be re-quoted to a raw ':' and fail SigV4."""
    from yarl import URL

    url = converse_url("https://bedrock-runtime.us-east-1.amazonaws.com", model_id, stream=stream)
    signed_path = url.split("amazonaws.com", 1)[1]
    assert "%3A" in signed_path and ":" not in signed_path
    assert URL(url, encoded=True).raw_path == signed_path
    assert URL(url).raw_path != signed_path  # the re-quoting the fix avoids


async def test_no_session_token_and_an_endpoint_override(monkeypatch: pytest.MonkeyPatch) -> None:
    await _compare(monkeypatch, _edge_requests()[1][1], token=False, endpoint="http://127.0.0.1:28181")


def test_every_golden_body_is_the_golden_parameter_form() -> None:
    import base64
    import json

    for golden in CONVERSE_GOLDENS:
        req = request_from(golden["request"])
        model = example_snapshot().model(req.model_alias)
        params = converse_params(req, model)
        media = any("text" not in b for m in params["messages"] for b in m["content"])
        body: dict[str, Any] = json.loads(converse_body(params, media=media))
        for message in body["messages"]:
            for block in message["content"]:
                for kind in ("image", "document"):
                    if kind in block:
                        raw = block[kind]["source"]["bytes"]
                        block[kind]["source"]["bytes"] = {
                            "$bytes_b64": base64.b64encode(base64.b64decode(raw)).decode()
                        }
        assert {"modelId": model.id, **body} == golden["expected"]["body"], golden["name"]
