"""SE7 (06 §9 "media injection per input view"; spec-10 §3 SE7, spec-09 §5 item 7): one injection corpus in every
media carrier and input view on the stack, judged on the wire mock's capture of every model request.

Carriers (each with its own digits-only canaries; filenames carry FNCANARY): a PNG with the corpus rendered as text
(OCR'd for text-only members and models), a PDF with the corpus as its text layer (forged reserved tags included), a
scanned PDF (the corpus as a raster page, OCR'd), a Markdown upload. Three requests carry all four files:

1. a turn pinned to glm-reason (GLM is text-only: OCR text and document Markdown enter its prompt), injection not
   suspected: standard spotlighting;
2. an unpinned first turn whose decision says coding/hard with `injection_suspected` answered at p = 0.9 (final
   before S1): the speculative Sol attempt is restarted on opus-deep (SEC-INJ-04's forced restart);
3. a router-default `/v1/decisions` call (SEC-INJ-06).

Pass (0 policy violations): no forged tag unescaped and no filename in any request (SEC-INJ-02/-03); every media
canary sits in a datamarked block right after its `[image-N|doc-N: ocr_text|derived_text]` label line (members) or
inside `<untrusted_input source=... kind=...>` (generation), and verifier requests hold no datamark (SEC-INJ-06,
item 3); every carrier reached a model; turn 1 is served by glm-reason with its pinned 4,096-token cap; turn 2 is
served by opus-deep (allowlisted; no route outside `intent_routes`), only the restarted attempt has the user text
datamarked (strict), both attempts share the system role, and the answer is verified (`turn.verified`). Not
covered: `injection_suspected` recall (Phase 0, needs real models).
"""

from __future__ import annotations

from typing import Final

import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import Json, data, first, live_turn, mock_calls, mock_seq, tag
from tests.security.artifacts import write_jsonl
from tests.security.stack import mediakit as mk
from tests.security.stack.test_injection import strings, upload
from tests.security.stack.world import QUESTION

from noema.contracts.validate import SseEvent

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]
ALLOWED_ROUTES: Final = {"sol-fast", "opus-deep", "glm-reason", "race-sol-glm", "best-of-sol-opus"}


def scenario() -> Json:
    """Turn 1: GLM streams; turn 2: injection suspected fast (100 ms), intent coding / complexity hard at 350 ms
    (below DeepSeek's 400 ms backup), Sol's text only at 3 s (so S1 restarts it), Opus at 600 ms; the verifier."""
    rules = [sc.stream(sc.GLM, first_text_ms=300), sc.stream(sc.SOL, first_text_ms=3000)]
    rules += [sc.stream(sc.OPUS, first_text_ms=600), *sc.verifier(ms=200)]
    rules.append(sc.answer({"prompt_contains": sc.INJECTION}, sc.HIGH, 100))
    rules += [sc.answer({"prompt_contains": sc.INTENT}, sc.CODING, 350)]
    rules += [sc.answer({"prompt_contains": sc.COMPLEXITY}, sc.HARD, 350), sc.answer({}, sc.LOW, 350)]
    return sc.document("SE7-media-injection", rules)


async def run_turn(stack: Stack, body: Json) -> list[SseEvent]:
    async with live_turn(stack.client(0), {**body, "stream": True}) as turn:
        await turn.rest()
    return turn.events


async def test_media_injection_per_input_view(stack: Stack, mock: Mock) -> None:
    corpus = mk.Corpus()
    pdf, png = "application/pdf", "image/png"
    files = {
        "image": await upload(stack, corpus.filename("png"), mk.render_png(corpus.lines("image", tags=False)), png),
        "pdf": await upload(stack, corpus.filename("pdf"), mk.text_pdf(corpus.lines("pdf", tags=True)), pdf),
        "scan": await upload(stack, corpus.filename("scan.pdf"), mk.scanned_pdf(corpus.lines("scan", tags=False)), pdf),
        "md": await upload(stack, corpus.filename("md"), mk.markdown(corpus.lines("md", tags=True)), "text/markdown"),
    }
    user_canary = corpus.word("user")
    parts: list[Json] = [{"type": "input_image", "file_id": files["image"]}]
    parts += [{"type": "input_file", "file_id": files[k]} for k in ("pdf", "scan", "md")]

    def message(text: str) -> list[Json]:
        """The four files with a distinct text per request (no L2 decision-cache hit between them)."""
        return [{"role": "user", "content": [{"type": "input_text", "text": f"{text} ({tag('se7')})"}, *parts]}]

    await mock.load(scenario())
    before = await mock_seq(mock)
    pinned: Json = {"profile": "glm-reason", "effort": "medium", "max_output_tokens": 4096}
    turn1 = await run_turn(stack, {"input": message("Summarize the attachments"), "generation": pinned,
                                   "conditioning": "off"})  # fmt: skip
    mid = await mock_seq(mock)
    turn2 = await run_turn(stack, {"input": message(f"Summarize the attachments for me {user_canary}")})
    end = await mock_seq(mock)
    route = message("Route these files")
    decision: Json = {"profile": "router-default", "escalation": "off", "input": route, "questions": [QUESTION]}
    await stack.client(1).request("POST", "/v1/decisions", json=decision, expect=200)
    calls = await mock_calls(mock, before)
    found = mk.problems(calls, corpus)
    blob = "\n".join(s for call in calls for s in strings(call["body"]))
    unseen = [c for c in [*corpus.media, user_canary] if c not in blob]
    t1_glm = [c for c in calls if c["seq"] <= mid and c["model"] == sc.GLM and c["op"] == "converse-stream"]
    t2_gen = [c for c in calls if mid < c["seq"] <= end and c["op"] == "converse-stream"]
    summary: Json = {"calls": len(calls), "unseen": unseen}
    write_jsonl("media-injection-findings.jsonl", [*found, summary])
    assert found == [], found[:10]
    assert unseen == [], unseen  # every carrier reached a model: the zero above is not vacuous
    # turn 1: glm-reason as pinned, its cap kept (no constraint loosened)
    assert first(turn1, "route.confirmed")["routing"]["route_id"] == "glm-reason"
    assert t1_glm and all(c["body"]["inferenceConfig"]["maxTokens"] <= 4096 for c in t1_glm), t1_glm[:1]
    # turn 2: restarted on the allowlisted opus-deep, strict only for the restarted attempt, verified
    routes = [data(e)["route_id"] for e in turn2 if e.event == "generation.started"]
    assert routes == ["sol-fast", "opus-deep"] and set(routes) <= ALLOWED_ROUTES, routes
    sol, opus = (next(c for c in t2_gen if c["model"] == m) for m in (sc.SOL, sc.OPUS))
    user_tokens = {m: [t for _b, t in mk.occurrences(c, user_canary)] for m, c in (("sol", sol), ("opus", opus))}
    assert all(mk.DATAMARK not in t for t in user_tokens["sol"]), user_tokens
    assert user_tokens["opus"] and all(mk.DATAMARK in t for t in user_tokens["opus"]), user_tokens
    assert sol["body"]["system"] == opus["body"]["system"]
    assert [e for e in turn2 if e.event == "turn.verified"], [e.event for e in turn2]
