"""Hidden reasoning on Converse JSON (Wave E2, live-smoke fidelity item 1 / finding 25a): Claude 5.5 reasons
adaptively and Bedrock hides it (one reasoningContent block with an empty text and a signature). The reasoning
counts toward maxTokens, so at the live verify-dist@1 budget (88) the answer has no text and stops max_tokens, as
live Haiku did in L7; with room left the text follows. Planned by `stream.reasoning.hidden_tokens`."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Final

import orjson
import pytest
from tests.live.wiretap import key_paths
from tests.unit.mockbedrock.helpers import HAIKU, SOL, USER_HI, scenario
from tools.mockbedrock import MockBedrock
from types_aiobotocore_bedrock_runtime import BedrockRuntimeClient

pytestmark = [pytest.mark.unit]

LIVE: Final = Path(__file__).resolve().parents[3] / "tests/cassettes/live-20261011T073404/35-haiku-5.5-complete.json"
ANSWER: Final = '{"A": 0.9, "B": 0.1}'  # 20 characters = 5 tokens


def hidden(model: str, tokens: int) -> dict[str, Any]:
    return scenario(
        {"match": {"model": model}, "answer": {"raw_text": ANSWER}, "stream": {"reasoning": {"hidden_tokens": tokens}}}
    )


async def converse(bedrock: BedrockRuntimeClient, model: str, max_tokens: int) -> Any:
    return await bedrock.converse(
        modelId=model, messages=USER_HI, inferenceConfig={"maxTokens": max_tokens}, outputConfig={"effort": "low"}
    )


async def test_at_the_live_budget_the_answer_is_reasoning_only_as_live(
    mock: MockBedrock, bedrock: BedrockRuntimeClient
) -> None:
    live = orjson.loads(LIVE.read_bytes())
    mock.load_scenario(hidden(HAIKU, 200))
    resp = await converse(bedrock, HAIKU, live["max_tokens"])
    assert key_paths(resp) == live["wire"]["response"]  # the live wire shape, key for key
    assert resp["stopReason"] == live["stop"] == "max_tokens"
    assert resp["usage"]["outputTokens"] == live["usage"]["output_tokens"] == 88
    (block,) = resp["output"]["message"]["content"]
    assert block["reasoningContent"]["reasoningText"]["text"] == ""
    assert block["reasoningContent"]["reasoningText"]["signature"]


async def test_with_room_left_the_text_follows_the_hidden_reasoning(
    mock: MockBedrock, bedrock: BedrockRuntimeClient
) -> None:
    mock.load_scenario(hidden(HAIKU, 200))
    resp = await converse(bedrock, HAIKU, 600)  # answer-verifier Haiku with the effort-low reserve (CR #26)
    reasoning, text = resp["output"]["message"]["content"]
    assert reasoning["reasoningContent"]["reasoningText"]["text"] == "" and text == {"text": ANSWER}
    assert resp["stopReason"] == "end_turn"
    assert resp["usage"]["outputTokens"] == 200 + 5


async def test_the_text_is_cut_where_the_budget_ends(mock: MockBedrock, bedrock: BedrockRuntimeClient) -> None:
    mock.load_scenario(hidden(HAIKU, 200))
    resp = await converse(bedrock, HAIKU, 202)
    assert resp["output"]["message"]["content"][1] == {"text": ANSWER[:8]}
    assert (resp["stopReason"], resp["usage"]["outputTokens"]) == ("max_tokens", 202)


async def test_the_openai_family_redacts_its_hidden_reasoning(mock: MockBedrock, bedrock: BedrockRuntimeClient) -> None:
    mock.load_scenario(hidden(SOL, 40))
    resp = await converse(bedrock, SOL, 512)
    reasoning, text = resp["output"]["message"]["content"]
    assert set(reasoning["reasoningContent"]) == {"redactedContent"} and text == {"text": ANSWER}
    assert base64.b64encode(reasoning["reasoningContent"]["redactedContent"])  # botocore decodes the blob
    assert resp["usage"]["outputTokens"] == 40 + 5


async def test_no_hidden_reasoning_by_default(mock: MockBedrock, bedrock: BedrockRuntimeClient) -> None:
    """The live Haiku router calls (verbalized-dist@1, effort low) did not reason: the default plans none."""
    mock.load_scenario(scenario({"match": {"model": HAIKU}, "answer": {"raw_text": ANSWER}}))
    resp = await converse(bedrock, HAIKU, 88)
    assert resp["output"]["message"]["content"] == [{"text": ANSWER}]
    assert (resp["stopReason"], resp["usage"]["outputTokens"]) == ("end_turn", 5)
