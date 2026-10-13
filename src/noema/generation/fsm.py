"""The attempt state machine (spec-04 §4.1; IMPL-011, IMPL-016).

States: P pending (call open, no upstream event), S streaming (events, no text), B buffering (text or a
content-filter stop held, uncommitted), C committed; terminal DONE, ERR, X (cancelled) and F (failed).

`TRANSITIONS` is the attempt-level part of the transition table (rows T3-T10 and T13-T19; T1, T2, T11 and T12 are
the profile run's walk, `noema.generation.run`). Terminal states have no outgoing transition: `step` raises
`IllegalTransition` for them, which is how a bug that touches a finished attempt shows up.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class St(StrEnum):
    """Attempt states."""

    P = "P"
    S = "S"
    B = "B"
    C = "C"
    DONE = "done"
    ERR = "err"
    X = "x"
    F = "f"


class In(StrEnum):
    """Inputs of the attempt state machine."""

    UPSTREAM = "upstream"  # MessageStart / ReasoningDelta (T3)
    TEXT = "text"  # a non-empty TextDelta (T4, T5, T14)
    TEXT_ARMED = "text_armed"  # the first TextDelta of an armed (or hold=False) run (T7)
    FILTER_STOP = "filter_stop"  # Stop{content_filter} without text: an empty committable answer (T4)
    HOLD = "hold"  # Stop / UsageEvent / stream end while buffered or committed (T5; T15 waits for the end)
    COMMIT = "commit"  # the commit point (T6, T8)
    FAIL = "fail"  # ErrorEvent, adapter error, timeout, text-less stop (T10, T17)
    STALL = "stall"  # idle timeout after commit (T16)
    SPEC_CHANGED = "spec_changed"  # T9
    LOST_RACE = "lost_race"  # T13
    COMPLETE = "complete"  # the stream ended after a Stop (T15)
    CLIENT_ABORT = "client_abort"  # T18
    DEADLINE = "deadline"  # T19


TERMINAL: Final[frozenset[St]] = frozenset({St.DONE, St.ERR, St.X, St.F})
LIVE_UNCOMMITTED: Final[frozenset[St]] = frozenset({St.P, St.S, St.B})


def _rows() -> dict[St, dict[In, St]]:
    rows: dict[St, dict[In, St]] = {s: {} for s in St}
    for s in (St.P, St.S):
        rows[s][In.UPSTREAM] = St.S  # T3
        rows[s][In.TEXT] = St.B  # T4
        rows[s][In.FILTER_STOP] = St.B  # T4
        rows[s][In.TEXT_ARMED] = St.C  # T7
    rows[St.B][In.UPSTREAM] = St.B
    rows[St.B][In.TEXT] = St.B  # T5
    rows[St.B][In.HOLD] = St.B  # T5: a Stop / usage / stream end is held
    rows[St.B][In.COMMIT] = St.C  # T6, T8
    for s in LIVE_UNCOMMITTED:
        rows[s][In.SPEC_CHANGED] = St.X  # T9
        rows[s][In.FAIL] = St.F  # T10
        rows[s][In.LOST_RACE] = St.X  # T13
        rows[s][In.CLIENT_ABORT] = St.X  # T18
        rows[s][In.DEADLINE] = St.X  # T19
    rows[St.C][In.UPSTREAM] = St.C
    rows[St.C][In.TEXT] = St.C  # T14
    rows[St.C][In.HOLD] = St.C  # a Stop or usage before the stream end (T15 completes at the end)
    rows[St.C][In.COMPLETE] = St.DONE  # T15
    rows[St.C][In.STALL] = St.ERR  # T16
    rows[St.C][In.FAIL] = St.ERR  # T17
    rows[St.C][In.CLIENT_ABORT] = St.X  # T18: committed, finish_reason = cancelled
    rows[St.C][In.DEADLINE] = St.ERR  # T19: committed, finish_reason = cancelled, no frame
    return rows


TRANSITIONS: Final[Mapping[St, Mapping[In, St]]] = MappingProxyType(
    {state: MappingProxyType(row) for state, row in _rows().items()}
)


class IllegalTransition(RuntimeError):
    """An input the state machine does not accept in the current state (a bug)."""

    def __init__(self, state: St, inp: In) -> None:
        super().__init__(f"illegal attempt transition: {state.value} --{inp.value}-->")
        self.state = state
        self.inp = inp


def step(state: St, inp: In) -> St:
    """The next state; raises `IllegalTransition` when `inp` is not accepted in `state` (always for terminal
    states)."""
    nxt = TRANSITIONS[state].get(inp)
    if nxt is None:
        raise IllegalTransition(state, inp)
    return nxt
