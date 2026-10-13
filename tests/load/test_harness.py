"""Self-tests of the load harness's pure parts (no stack, no network): SSE framing and stamps in the driver, the
mock-side nonce, the nonce join and overhead arithmetic, the bootstrap CI, the lag file and the plan syntax.
`pytest tests/load/test_harness.py` (tests/load is never collected by make check)."""

from __future__ import annotations

from array import array
from pathlib import Path

import orjson
import pytest
from tests.load import stats
from tests.load.driver import KEEPALIVE_EXPIRY_S, TurnRecord, frame_event, on_frame, split_frames, turn_body
from tests.load.lag import read_lag
from tests.load.mockserve import compact, last_nonce
from tests.load.run import Side, parse_plan, parse_side, side_summary
from tests.load.slow import body as heavy_body

pytestmark = [pytest.mark.unit]
MS = 1_000_000


def _sse(event: str, data: dict[str, object]) -> bytes:
    return b"id: 1\nevent: " + event.encode() + b"\ndata: " + orjson.dumps(data) + b"\n\n"


def test_frames_split_across_chunks_and_the_first_stamp_is_kept() -> None:
    delta1 = _sse("generation.delta", {"type": "generation.delta", "text": "«ts:1000»a«ts:2000»b"})
    delta2 = _sse("generation.delta", {"type": "generation.delta", "text": "«ts:5000»c"})
    done = _sse("turn.completed", {"type": "turn.completed", "timings": {"holdback_ms": 0, "ttft_ms": 3}})
    stream = (
        b": ping\n\n" + _sse("turn.created", {"type": "turn.created", "turn_id": "turn_1"}) + delta1 + delta2 + done
    )
    rec, rest = TurnRecord(stream=0, n=0, nonce="n", t_send=0), b""
    for t, chunk in enumerate((stream[:40], stream[40:130], stream[130:])):
        blocks, rest = split_frames(rest + chunk)
        for block in blocks:
            event, data = frame_event(block)
            if event is not None and data is not None:
                on_frame(rec, event, data, 10_000 * (t + 1))
    assert rest == b""
    assert (rec.turn_id, rec.deltas, rec.stamp0, rec.holdback_ms, rec.ttft_ms) == ("turn_1", 2, 1000, 0, 3)
    assert rec.t_first_delta is not None and rec.egress_max_us == (30_000 - 5000) // 1000


def test_the_turn_body_pins_every_pre_commit_key_and_carries_the_nonce() -> None:
    body = turn_body("abc", 3, "ses_1")
    assert body["generation"] == {"profile": "sol-fast", "effort": "low", "max_output_tokens": 600}
    assert body["conditioning"] == "off" and body["session_id"] == "ses_1" and body["store"] is True
    assert "«nonce:abc»" in orjson.dumps(body).decode()


def test_the_mock_side_nonce_is_the_last_one_of_the_last_message() -> None:
    history = {"role": "user", "content": [{"text": "old «nonce:old1»"}]}
    current = {"role": "user", "content": [{"text": "history «nonce:old2» then «nonce:new»"}]}
    assert last_nonce({"messages": [history, {"role": "assistant", "content": []}, current]}) == "new"
    assert last_nonce({"prompt": "x «nonce:only»"}) == "only" and last_nonce(None) is None
    rec = compact({"seq": 4, "op": "chat", "t_req_recv_ns": 1, "body": {"messages": [current]}, "headers": {}})
    assert rec["nonce"] == "new" and rec["seq"] == 4 and "headers" not in rec


def _turn(nonce: str, send: int, first: int, done: int) -> dict[str, object]:
    return {"nonce": nonce, "status": 200, "error": None, "t_send": send, "t_first_delta": first, "t_done": done,
            "stamp0": first - MS, "holdback_ms": 0, "reason": "spec_agrees"}  # fmt: skip


def _stream(nonce: str, seq: int, recv: int, first_text: int, done: int) -> dict[str, object]:
    return {"seq": seq, "op": "converse-stream", "nonce": nonce, "t_req_recv_ns": recv,
            "t_first_byte_ns": recv, "t_first_text_sent_ns": first_text, "t_done_ns": done}  # fmt: skip


def test_the_join_computes_the_overhead_against_the_mock_timestamps() -> None:
    turns = [_turn("a", 0, 312 * MS, 7000 * MS), _turn("b", 0, 320 * MS, 7010 * MS)]
    turns.append({"nonce": "c", "status": 429, "error": "rate_limited", "t_send": 0})
    mock = [_stream("a", 2, 10 * MS, 310 * MS, 6910 * MS), _stream("a", 9, 50 * MS, 400 * MS, 7000 * MS)]
    mock += [_stream("b", 3, 12 * MS, 312 * MS, 6912 * MS)]
    mock += [{"seq": 5, "op": "chat", "nonce": "a", "t_req_recv_ns": 0}] * 15
    joined, skipped = stats.join(turns, mock)
    assert [round(j.ttft_overhead_ms, 3) for j in joined] == [12.0, 20.0]  # the first stream record of "a"
    assert [round(j.e2e_overhead_ms, 3) for j in joined] == [100.0, 110.0]
    assert [j.ingress_ms for j in joined] == [10.0, 12.0] and joined[0].decisions == 15
    assert skipped == {"error:429:rate_limited": 1}


def test_the_bootstrap_ci_brackets_the_estimate_and_is_deterministic() -> None:
    values = [float(v % 97) for v in range(2000)]
    est, low, high = stats.percentile_ci(values, 95)
    assert low <= est <= high and (est, low, high) == stats.percentile_ci(values, 95)
    assert stats.summarize([])["n"] == 0


def test_the_lag_file_round_trips_and_ignores_a_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / "x.lag"
    path.write_bytes(array("q", [1, 2, 3, 4]).tobytes() + b"\x01\x02")
    assert read_lag(path) == [(1, 2), (3, 4)]
    assert stats.lag_summary([path], 0, 2)["n"] == 1


def test_the_plan_syntax() -> None:
    assert [(s.streams, s.hold_s) for s in parse_plan("50:300,200:900")] == [(50, 300.0), (200, 900.0)]


def test_the_join_takes_the_earliest_stream_across_mock_processes() -> None:
    """Four mock processes number their records independently: the first generation request is the earliest one
    received, not the lowest seq."""
    turns = [_turn("a", 0, 312 * MS, 7000 * MS)]
    mock = [_stream("a", 9, 10 * MS, 310 * MS, 6910 * MS), _stream("a", 2, 50 * MS, 400 * MS, 7000 * MS)]
    (joined,), _ = stats.join(turns, mock)
    assert joined.stream["seq"] == 9 and round(joined.ttft_overhead_ms, 3) == 12.0


def _lag_file(path: Path, lags_ms: list[float]) -> Path:
    path.write_bytes(array("q", [v for i, lag in enumerate(lags_ms) for v in (i, int(lag * MS))]).tobytes())
    return path


def test_validity_needs_99_9_percent_spec_agrees_at_holdback_0_and_every_process_lag(tmp_path: Path) -> None:
    turns = [_turn(f"n{i}", 0, 312 * MS, 7000 * MS) for i in range(1000)]
    mock = [_stream(f"n{i}", i, 10 * MS, 310 * MS, 6910 * MS) for i in range(1000)]
    lags = {"drivers": [_lag_file(tmp_path / "d.lag", [0.5] * 100)], "mock": [_lag_file(tmp_path / "m.lag", [1.0])]}
    turns[0] = {**turns[0], "holdback_ms": 3}
    joined, _ = stats.join(turns, mock)
    ok = stats.validity(joined, lags, 0, 10_000)
    assert ok["valid"] and ok["holdback_zero_and_spec_agrees_share"] == 0.999 and ok["holdback_nonzero_ms"] == {"3": 1}
    turns[1] = {**turns[1], "reason": "holdback_deadline"}
    assert not stats.validity(stats.join(turns, mock)[0], lags, 0, 10_000)["valid"]
    lags["mock"].append(_lag_file(tmp_path / "m2.lag", [2.5] * 100))  # one process over 2 ms invalidates
    bad = stats.validity(joined, lags, 0, 10_000)
    assert (
        not bad["valid"] and bad["checks"]["mock_lag_p99"] is False and bad["worst_process_lag_p99_ms"]["mock"] == 2.5
    )


def test_the_driver_records_the_server_decision_timings() -> None:
    rec = TurnRecord(stream=0, n=0, nonce="n", t_send=0)
    timings = {"holdback_ms": 0, "decision_ms": 57, "decision_lead_ms": 240, "ttft_ms": 310, "e2e_ms": 7000}
    on_frame(rec, b"turn.completed", orjson.dumps({"type": "turn.completed", "timings": timings}), 1)
    assert (rec.decision_ms, rec.decision_lead_ms, rec.holdback_ms) == (57, 240, 0)


def test_the_side_syntax() -> None:
    assert parse_side("flood:t_beta:200:1") == Side("flood", 200, 1, tenant="t_beta")
    assert parse_side("slow:20:2") == Side("slow", 20, 2, rate_bps=12_600.0)
    assert parse_side("slow:20:2:5000") == Side("slow", 20, 2, rate_bps=5000.0)
    assert parse_side("fast:20:1") == Side("fast", 20, 1, rate_bps=0.0)
    for bad in ("flood:200:1", "slow:20", "drip:1:1"):
        with pytest.raises(ValueError, match="--side"):
            parse_side(bad)


def test_heavy_turns_carry_the_c3_tag_a_nonce_and_the_pinned_keys() -> None:
    a, b = heavy_body("ses_1", 0), heavy_body("ses_1", 0)
    text = a["input"][0]["content"][0]["text"]
    assert "«c3»" in text and "«nonce:" in text and text != b["input"][0]["content"][0]["text"]
    assert a["generation"] == {"profile": "sol-fast", "effort": "low", "max_output_tokens": 600}
    assert a["store"] is True and a["stream"] is True and a["conditioning"] == "off"


def test_side_summaries_split_by_group_and_count_coalescing(tmp_path: Path) -> None:
    flood = [
        {"group": "flood", "status": 200, "error": None},
        {"group": "flood", "status": 429, "error": "queue_timeout"},
    ]
    heavy = [
        {"kind": "slow", "status": 200, "coalesced": 3, "eof_after_s": None, "turn_id": "turn_1", "errors": []}
        | {"bytes": 1000, "t_send": 0, "t_end": 10**9},
        {"kind": "slow", "status": 200, "coalesced": 0, "eof_after_s": 90.4, "turn_id": "turn_2", "errors": []}
        | {"bytes": 3000, "t_send": 0, "t_end": 10**9},
        {"kind": "slow", "violation": "bad frame"},
    ]
    (tmp_path / "side1-flood-0-0.jsonl").write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in flood))
    (tmp_path / "side2-slow-0.jsonl").write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in heavy))
    one, two = side_summary(tmp_path, 1), side_summary(tmp_path, 2)
    flood_summary = {"turns": 2, "status": {"200": 1, "429": 1}, "errors": {"queue_timeout": 1}, "violations": 0}
    assert one == {"flood": flood_summary}
    slow = two["slow"]
    assert (slow["turns"], slow["violations"], slow["coalesced_turns"], slow["coalesced_frames"]) == (3, 1, 1, 3)
    assert slow["eof_after_s"] == [90.4] and slow["turn_ids"] == ["turn_1", "turn_2"]
    assert slow["read_bps_p50"] == 3000
    assert side_summary(tmp_path, 0) == {}


def test_the_load_config_overrides_one_tenant_and_keeps_the_others_lifted() -> None:
    from tests.load.stack import LOAD_LIMITS, load_config

    doc = load_config(7, ["t_beta:concurrent_streams_per_project=40"], ["t_acme:sol-6.1:600:1000000000"])
    beta, acme = doc["tenants"]["t_beta"]["limits"], doc["tenants"]["t_acme"]["limits"]
    assert beta["concurrent_streams_per_project"] == 40 and "model_quotas" not in beta
    assert acme["concurrent_streams_per_project"] == LOAD_LIMITS["concurrent_streams_per_project"]
    assert acme["model_quotas"] == {"sol-6.1": {"rpm": 600, "tpm": 1_000_000_000}}
    assert doc["config_version"] == 7


def test_the_driver_expires_idle_connections_before_uvicorn_closes_them() -> None:
    import uvicorn

    # the image CMD keeps uvicorn's default timeout_keep_alive
    assert uvicorn.Config("noema.api.app:create_app").timeout_keep_alive > KEEPALIVE_EXPIRY_S
