"""The live-smoke harness without live calls (spec-10 §6): the process-wide budget refuses the call over the limit
before any byte, the model allowlist, the max_tokens caps, redacted cassettes (no prompt or answer text), the
report, the global journal (every run's calls count; a spent journal refuses the session; `need` skips a test whose
worst case does not fit), and that the live config patches load."""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest
from tests.live import wiretap
from tests.live.budget import CountingAdapterRegistry, Ledger, LiveBudgetExceeded, journal_entries
from tests.live.livecfg import LIVE_TENANT, live_snapshot
from tests.unit.adapters.lean_reference import creds
from tests.unit.adapters.support import SOL, TIMEOUTS, Rig, request, rig
from tests.unit.adapters.test_lean_converse_stream import CASES

from noema.adapters._converse_wire import converse_url, signed_headers
from noema.adapters._eventstream import EventStreamDecoder
from noema.core.protocols.adapters import TextDelta
from noema.core.values import TextBlock
from noema.ops.cost.estimator import HeuristicTokenEstimator

SECRET_PROMPT = "the prompt text must never reach a cassette"


async def counted(r: Rig, ledger: Ledger, alias: str, **kw: object) -> None:
    registry = CountingAdapterRegistry(r.registry, ledger)
    req = request(alias, SECRET_PROMPT, **kw)  # type: ignore[arg-type]
    async with r.clock.autojump():
        await registry.for_model(alias).complete(req, tenant=r.tenant, deadline=r.deadline())


async def test_the_call_over_the_limit_is_refused_before_any_byte(tmp_path: Path) -> None:
    ledger = Ledger(run="t", root=tmp_path, limit=2)
    async with rig() as r:
        r.scenario({"match": {}, "answer": {"raw_text": "answer text must stay out"}})
        await counted(r, ledger, "sol-6.1")
        await counted(r, ledger, "deepseek-3.2")
        with pytest.raises(LiveBudgetExceeded):
            await counted(r, ledger, "sol-6.1")
        assert len(r.emulator.requests) == 2 and ledger.exceeded and ledger.count == 2
    report = orjson.loads(ledger.write_report().read_bytes())
    assert report["count"] == 2 and len(report["calls"]) == 2
    cassettes = sorted((tmp_path / "tests" / "cassettes" / "live-t").glob("*.json"))
    assert [p.name for p in cassettes] == ["01-sol-6.1-complete.json", "02-deepseek-3.2-complete.json"]
    for path in cassettes:
        text = path.read_text()
        assert SECRET_PROMPT not in text and "answer text" not in text
        assert orjson.loads(text)["text_chars"] == len("answer text must stay out")


async def test_allowlist_and_max_tokens_caps(tmp_path: Path) -> None:
    ledger = Ledger(run="t", root=tmp_path)
    async with rig() as r:
        r.scenario({"match": {}, "answer": {"raw_text": "ok"}})
        big = request("sol-6.1", max_tokens=4096)
        registry = CountingAdapterRegistry(r.registry, ledger)
        async with r.clock.autojump():
            await registry.for_model("sol-6.1").complete(big, tenant=r.tenant, deadline=r.deadline())
            member = request("haiku-5.5", kind="decision_member", max_tokens=4096)
            await registry.for_model("haiku-5.5").complete(member, tenant=r.tenant, deadline=r.deadline())
            labeler = request("sol-6.1", kind="labeling", max_tokens=4096)
            await registry.for_model("sol-6.1").complete(labeler, tenant=r.tenant, deadline=r.deadline())
        caps = [q.body["inferenceConfig"]["maxTokens"] for q in r.emulator.requests]
        assert caps == [512, 1024, 1040]  # decision members uncut (CR #26); labeling: 16 + the medium reserve (L9)
        with pytest.raises(LiveBudgetExceeded):
            ledger.take(request("unknown-model"))
        assert ledger.count == 3


async def test_stream_cassette_counts_events(tmp_path: Path) -> None:
    ledger = Ledger(run="s", root=tmp_path)
    async with rig() as r:
        r.scenario({"match": {"model": SOL}, "stream": {"chunks": ["a", "b"]}})
        adapter = CountingAdapterRegistry(r.registry, ledger).for_model("sol-6.1")
        async with r.clock.autojump():
            cm = adapter.stream(request("sol-6.1"), tenant=r.tenant, timeouts=TIMEOUTS, deadline=r.deadline())
            async with cm as events:
                texts = [e for e in [ev async for ev in events] if isinstance(e, TextDelta)]
    assert len(texts) == 2
    (cassette,) = ledger.calls
    assert cassette["counts"] == {"MessageStart": 1, "TextDelta": 2, "Stop": 1, "UsageEvent": 1}
    assert cassette["events"] == ["MessageStart", "TextDelta", "Stop", "UsageEvent"] and cassette["stop"] == "end_turn"


def test_live_config_patches_load() -> None:
    base = live_snapshot("123456789012")
    assert base.tenant_ref(LIVE_TENANT).aws_account_id == "123456789012"
    assert base.config.labeling.sample.uniform_rate == 0 and base.config.labeling.sample.uncertainty_rate == 0
    smoke = live_snapshot("123456789012", smoke_router=True, version=2)
    assert smoke.config.routing.router_profile == "smoke-router" and smoke.version == 2
    assert len(smoke.decision_profile("smoke-router").members) == 1


async def test_the_l9_labeler_smoke_offline(tmp_path: Path) -> None:
    # tests/live/test_labelers_live.py on the emulator: both shipped labelers, 2 ledger calls, parseable answers
    from tests.live.test_labelers_live import QUESTION

    from noema.core.values import TextBlock
    from noema.labeling.labelers import build_request, letter_order, parse_answer, question_from_wire

    ledger = Ledger(run="l9", root=tmp_path)
    async with rig() as r:
        r.scenario({"match": {}, "answer": {"raw_text": "A"}})
        snapshot, q = r.config.current(), question_from_wire(QUESTION)
        registry = CountingAdapterRegistry(r.registry, ledger)
        for alias in snapshot.labeling.labelers:
            model = snapshot.model(alias)
            order = letter_order(q, "dec_live_smoke", model.id)
            req = build_request(
                q, order, (TextBlock("Let me talk to a person.\n"),), alias=alias, model=model, snapshot=snapshot
            )
            async with r.clock.autojump():
                resp = await registry.for_model(alias).complete(req, tenant=r.tenant, deadline=r.deadline())
            assert parse_answer(resp.text, q, order) is not None
    assert ledger.count == 2 and [c["alias"] for c in ledger.calls] == list(snapshot.labeling.labelers)


def test_the_journal_budget_spans_runs(tmp_path: Path) -> None:
    # the live run's limit is global: the journal of every earlier run counts, one JSON line per call before any byte
    journal = tmp_path / "live-calls.jsonl"
    journal.write_text("".join(f'{{"n": {i}}}\n' for i in range(86)))
    ledger = Ledger(run="j", root=tmp_path, journal=journal)
    assert ledger.remaining() == 1
    ledger.take(request("sol-6.1"), "stream")
    line = orjson.loads(journal.read_text().splitlines()[-1])
    assert line["model"] == "us.openai.gpt-6.1-sol" and line["operation"] == "stream" and {"ts", "test"} <= set(line)
    with pytest.raises(LiveBudgetExceeded):
        ledger.take(request("sol-6.1"))
    assert journal_entries(journal) == 87 and ledger.count == 1 and ledger.exceeded


def test_need_skips_a_test_whose_worst_case_does_not_fit(tmp_path: Path) -> None:
    journal = tmp_path / "live-calls.jsonl"
    journal.write_text('{"n": 0}\n' * 85)
    ledger = Ledger(run="n", root=tmp_path, journal=journal)
    ledger.need(2)  # fits
    with pytest.raises(pytest.skip.Exception):
        ledger.need(3)


def test_a_live_session_refuses_to_start_on_a_spent_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.live import conftest as live_conftest

    journal = tmp_path / "live-calls.jsonl"
    journal.write_text('{"n": 0}\n' * 87)
    monkeypatch.setattr(live_conftest, "JOURNAL", journal)
    monkeypatch.setattr(live_conftest, "MOCK", False)
    monkeypatch.setenv("NOEMA_LIVE", "1")
    with pytest.raises(pytest.exit.Exception, match="already holds 87 >= 87 calls"):
        live_conftest.pytest_configure(pytest.Config.__new__(pytest.Config))


async def test_cassettes_keep_input_chars_and_the_resolved_model_id(tmp_path: Path) -> None:
    # K1 needs the prompt length (never the text); the journal and cassette name the id the request resolves to
    journal = tmp_path / "live-calls.jsonl"
    ledger = Ledger(run="k", root=tmp_path, journal=journal)
    ledger.estimator = HeuristicTokenEstimator()
    arn = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.openai.gpt-6.1-sol"
    snapshot = live_snapshot("123456789012", extra={"models": {"sol-6.1": {"id": arn}}})
    async with rig() as r:
        r.scenario({"match": {}, "answer": {"raw_text": "ok"}})
        await counted(r, ledger, "sol-6.1", snapshot=snapshot)
    (cassette,) = ledger.calls
    assert cassette["input_chars"] == len("You are terse.") + len(SECRET_PROMPT)
    assert cassette["model_id"] == arn and orjson.loads(journal.read_text())["model"] == arn
    assert cassette["estimated_input_tokens_chars4"] == 4 + 11  # ceil(14 / 4) + ceil(43 / 4): chars/4 per item
    assert cassette["estimated_input_tokens"] == HeuristicTokenEstimator().estimate(
        "sol-6.1", ("You are terse.",), [TextBlock(SECRET_PROMPT)]
    )


def test_the_signer_shadow_compares_with_sigv4auth() -> None:
    url = converse_url("https://bedrock-runtime.us-east-1.amazonaws.com", "arn:aws:bedrock:us-east-1:1:x/y")
    tap = wiretap.open_tap()
    wiretap._wrap_signer(signed_headers)(creds(), url, b"{}")
    signed = ["authorization", "content-type", "x-amz-date", "x-amz-security-token"]
    assert tap.signer == {
        "equal_to_botocore": True,
        "model_path_colon_encoded": True,
        "session_token": True,
        "signed": signed,
    }

    def forged(*args: object) -> dict[str, str]:
        return signed_headers(*args) | {"Authorization": "AWS4-HMAC-SHA256 forged"}  # type: ignore[arg-type]

    wiretap._wrap_signer(forged)(creds(), url, b"{}")
    assert tap.signer["equal_to_botocore"] is False


def test_the_decoder_shadow_compares_with_eventstreambuffer() -> None:
    body = b"".join(CASES["reasoning_then_text"])
    tap = wiretap.open_tap()
    shadow = wiretap._ShadowDecoder(EventStreamDecoder(), tap)
    messages = 0
    for i in range(0, len(body), 7):  # split chunks: messages straddle them
        shadow.add_data(body[i : i + 7])
        while shadow.next() is not None:
            messages += 1
    assert tap.decoder is not None and tap.decoder["messages"] == tap.decoder["equal"] == messages > 0
    assert tap.decoder["botocore_extra"] == tap.decoder["botocore_errors"] == 0 and not tap.decoder["fallback"]

    class Corrupting:
        def __init__(self) -> None:
            self.inner = EventStreamDecoder()

        def add_data(self, data: bytes) -> None:
            self.inner.add_data(data)

        def next(self) -> object:
            message = self.inner.next()
            return None if message is None else (message[0], message[1] + b"x")

    tap = wiretap.open_tap()
    shadow = wiretap._ShadowDecoder(Corrupting(), tap)
    shadow.add_data(body)
    while shadow.next() is not None:
        pass
    assert tap.decoder is not None and tap.decoder["equal"] == 0 < tap.decoder["messages"]
