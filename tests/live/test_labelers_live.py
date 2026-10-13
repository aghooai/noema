"""Live smoke L9 (spec-10 §6, labeling.md item 10): the labeler@1 request of both shipped labelers (`sol-6.1`,
`sonnet-5.5`) on one predicate, with the labeler's own max_tokens (16 + the reasoning reserve; the ledger's labeling
cap). 2 calls through the session `live` fixture, so they count toward the 60-call hard stop and get cassettes. The
live config keeps `labeling.sample` rates 0, so the request is built directly. NOT run in `make check`."""

from __future__ import annotations

from typing import Final

import pytest
from tests.live.conftest import Live

from noema.core.values import TextBlock
from noema.labeling.labelers import build_request, letter_order, parse_answer, question_from_wire

pytestmark = [pytest.mark.live, pytest.mark.enable_socket]
QUESTION: Final = {
    "type": "predicate",
    "name": "needs_human",
    "instructions": "The customer explicitly asks to speak with a human.",
    "criteria": {"true": "Asks for a person, agent or call back", "false": "No such request"},
}


async def test_both_labelers_answer_one_letter(live: Live) -> None:
    live.ledger.need(2)
    snapshot, q = live.config.current(), question_from_wire(QUESTION)
    blocks = (TextBlock("Please let me talk to a real person about my bill.\n"),)
    for alias in snapshot.labeling.labelers:
        model = snapshot.model(alias)
        order = letter_order(q, "dec_live_smoke", model.id)
        req = build_request(q, order, blocks, alias=alias, model=model, snapshot=snapshot)
        resp = await live.adapters.for_model(alias).complete(req, tenant=live.tenant, deadline=live.deadline())
        assert not resp.refused and parse_answer(resp.text, q, order) is not None, (alias, resp.text[:40])
