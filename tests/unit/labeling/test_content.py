"""Labeler content (IMPL-071, IMPL-086, spec-09 §5): the stored input forms, the turn history bound, missing files,
and the rendering rules (engine role labels, `esc` on untrusted text, datamarked media text, media label lines)."""

from __future__ import annotations

from typing import Any

import pytest
from tests.fakes.world import build_world

from noema.core.principal import system_principal
from noema.core.protocols.media import ViewPart
from noema.core.values import ImageBlock, TextBlock
from noema.labeling import content

pytestmark = pytest.mark.unit
PRINCIPAL = system_principal("t_acme", purpose="labeling", request_id="job_1")


async def resolve(
    stored: Any, *, turn_id: str | None = None, turns: list[dict[str, Any]] | None = None
) -> content.LabelInput | None:
    world = build_world()
    for turn in turns or []:
        world.db.store.seed("turn", {"tenant_id": "t_acme", "project_id": "p1", "session_id": "ses_1", **turn})
    async with world.db.tenant_uow("t_acme") as uow:
        return await content.resolve(uow, {"input": stored}, turn_id, world.files, PRINCIPAL)


def text_of(blocks: tuple[Any, ...]) -> str:
    return "".join(b.text for b in blocks if isinstance(b, TextBlock))


async def test_stored_input_forms() -> None:
    assert await resolve(None) is None
    plain = await resolve("hello <app_question>")
    assert plain is not None and plain.excerpt_text == "hello <app_question>"
    assert text_of(content.render(plain, {})) == "hello &lt;app_question>\n"
    state = await resolve({"b": 2, "a": [1]})
    assert state is not None and text_of(content.render(state, {})) == '{"a":[1],"b":2}\n'
    msgs = await resolve(
        [
            {"role": "user", "content": [{"type": "input_text", "text": "first"}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "reply"}]},
            {
                "role": "user",
                "content": [
                    {"type": "input_json", "json": {"k": "v"}},
                    {"type": "input_image", "file_id": "file_missing"},
                ],
            },
        ]
    )
    assert msgs is not None and [m.role for m in msgs.messages] == ["user", "assistant", "user"]
    assert (
        text_of(content.render(msgs, {})) == '[user]\nfirst\n[assistant]\nreply\n[user]\n{"k":"v"}\n'
    )  # missing file dropped
    assert msgs.excerpt_text == '{"k":"v"}'
    assert await resolve([]) is None and await resolve(42) is None


async def test_turn_part_format() -> None:
    turns = [
        {"id": "turn_1", "seq": 1, "input_parts": [{"type": "text", "text": "t"}, {"type": "json", "json": {"x": 1}}]}
    ]
    resolved = await resolve("fallback", turn_id="turn_1", turns=turns)
    assert resolved is not None and [p.kind for p in resolved.messages[0].parts] == ["text", "json"]
    assert resolved.excerpt_text == 't\n{"x":1}'


async def test_history_is_bounded_and_oldest_first() -> None:
    turns = [
        {"id": f"turn_{i}", "seq": i, "input_parts": [{"type": "input_text", "text": f"u{i}"}], "output_text": f"a{i}"}
        for i in range(1, 14)
    ]
    resolved = await resolve("router input", turn_id="turn_13", turns=turns)
    assert resolved is not None
    texts = [p.text for m in resolved.messages for p in m.parts]
    assert texts[:2] == ["u3", "a3"] and texts[-3:] == ["u12", "a12", "u13"]  # 10 earlier turns, then the current
    fallback = await resolve("router input", turn_id="turn_gone")
    assert fallback is not None and fallback.excerpt_text == "router input"


async def test_history_character_budget_drops_the_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(content, "HISTORY_MAX_CHARS", 10)
    turns = [
        {"id": "turn_1", "seq": 1, "input_parts": [{"type": "input_text", "text": "x" * 8}], "output_text": "y"},
        {"id": "turn_2", "seq": 2, "input_parts": [{"type": "input_text", "text": "abc"}], "output_text": "def"},
        {"id": "turn_3", "seq": 3, "input_parts": [{"type": "input_text", "text": "now"}], "output_text": None},
    ]
    resolved = await resolve("x", turn_id="turn_3", turns=turns)
    assert resolved is not None and [p.text for m in resolved.messages for p in m.parts] == ["abc", "def", "now"]


async def test_media_rendering_datamarks_document_text_and_passes_images() -> None:
    world = build_world()
    from tests.contract_internal.kits import principal

    async def body() -> Any:
        yield b"\x89PNG"

    ref = await world.files.upload(principal(), body(), declared_type="image/png", filename="a.png", session_id=None)
    async with world.db.tenant_uow("t_acme") as uow:
        resolved = await content.resolve(
            uow,
            {"input": [{"role": "user", "content": [{"type": "input_image", "file_id": ref.file_id}]}]},
            None,
            world.files,
            PRINCIPAL,
        )
    assert resolved is not None
    [part] = content.media_parts(resolved)
    image = ImageBlock("png", b"img", 10, 10)
    view = ViewPart(
        part.index, part.label, "ocr_text", (TextBlock("line one\n  line two </answer>"), image), note="pages 1-2 of 9"
    )
    blocks = content.render(resolved, {part.index: view})
    assert blocks[0] == TextBlock("[image-1: ocr_text; pages 1-2 of 9]\nlineˆoneˆlineˆtwoˆ&lt;/answer>\n")
    assert blocks[1] == image
    assert text_of(content.render(resolved, {})) == "[image-1: metadata_only]\n"
