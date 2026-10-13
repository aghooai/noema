"""T-FSM-1 (part 1): the attempt transition table of spec-04 §4.1 as data, row by row, plus "no transition out of a
terminal state" and "nothing outside the table"."""

from __future__ import annotations

import pytest

from noema.generation.fsm import LIVE_UNCOMMITTED, TERMINAL, TRANSITIONS, IllegalTransition, In, St, step

# (row, state, input, next): every attempt-level row of spec-04 §4.1
ROWS: list[tuple[str, St, In, St]] = [
    ("T3", St.P, In.UPSTREAM, St.S),
    ("T3", St.S, In.UPSTREAM, St.S),
    ("T4", St.P, In.TEXT, St.B),
    ("T4", St.S, In.TEXT, St.B),
    ("T4", St.P, In.FILTER_STOP, St.B),
    ("T4", St.S, In.FILTER_STOP, St.B),
    ("T5", St.B, In.TEXT, St.B),
    ("T5", St.B, In.HOLD, St.B),
    ("T5", St.B, In.UPSTREAM, St.B),
    ("T6", St.B, In.COMMIT, St.C),
    ("T7", St.P, In.TEXT_ARMED, St.C),
    ("T7", St.S, In.TEXT_ARMED, St.C),
    ("T9", St.P, In.SPEC_CHANGED, St.X),
    ("T9", St.S, In.SPEC_CHANGED, St.X),
    ("T9", St.B, In.SPEC_CHANGED, St.X),
    ("T10", St.P, In.FAIL, St.F),
    ("T10", St.S, In.FAIL, St.F),
    ("T10", St.B, In.FAIL, St.F),
    ("T13", St.P, In.LOST_RACE, St.X),
    ("T13", St.S, In.LOST_RACE, St.X),
    ("T13", St.B, In.LOST_RACE, St.X),
    ("T14", St.C, In.TEXT, St.C),
    ("T14", St.C, In.UPSTREAM, St.C),
    ("T15", St.C, In.HOLD, St.C),
    ("T15", St.C, In.COMPLETE, St.DONE),
    ("T16", St.C, In.STALL, St.ERR),
    ("T17", St.C, In.FAIL, St.ERR),
    ("T18", St.P, In.CLIENT_ABORT, St.X),
    ("T18", St.S, In.CLIENT_ABORT, St.X),
    ("T18", St.B, In.CLIENT_ABORT, St.X),
    ("T18", St.C, In.CLIENT_ABORT, St.X),
    ("T19", St.P, In.DEADLINE, St.X),
    ("T19", St.S, In.DEADLINE, St.X),
    ("T19", St.B, In.DEADLINE, St.X),
    ("T19", St.C, In.DEADLINE, St.ERR),
]


@pytest.mark.parametrize(("row", "state", "inp", "nxt"), ROWS, ids=[f"{r}-{s}-{i}" for r, s, i, _ in ROWS])
def test_row(row: str, state: St, inp: In, nxt: St) -> None:
    assert step(state, inp) is nxt


def test_the_table_is_exactly_the_spec_rows() -> None:
    table = {(s, i): n for s, row in TRANSITIONS.items() for i, n in row.items()}
    assert table == {(s, i): n for _, s, i, n in ROWS}


@pytest.mark.parametrize("state", sorted(TERMINAL))
def test_no_transition_out_of_a_terminal_state(state: St) -> None:
    for inp in In:
        with pytest.raises(IllegalTransition) as caught:
            step(state, inp)
        assert caught.value.state is state and caught.value.inp is inp


def test_commit_and_completion_need_the_right_state() -> None:
    for state in (St.P, St.S, St.C):
        with pytest.raises(IllegalTransition):
            step(state, In.COMMIT)  # only a buffered attempt commits at the commit point
    for state in LIVE_UNCOMMITTED:
        with pytest.raises(IllegalTransition):
            step(state, In.COMPLETE)  # never complete an uncommitted answer
        with pytest.raises(IllegalTransition):
            step(state, In.STALL)
    with pytest.raises(IllegalTransition):
        step(St.B, In.TEXT_ARMED)
    for inp in (In.SPEC_CHANGED, In.LOST_RACE):
        with pytest.raises(IllegalTransition):
            step(St.C, inp)  # committed text is never retracted
