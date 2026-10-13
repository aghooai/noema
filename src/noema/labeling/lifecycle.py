"""The calibrator lifecycle (ADR-003 §1, §3, §5; IMPL-072).

`fitted -> accepted | rejected`; `accepted -> active` by two-person activation only (operator A approves, operator
B != A activates, through the noema-admin CLI and the admin.* definer functions); `active -> downgraded` (drift) or
`retired` (replaced, manual, profile version retired); any non-retired status may be retired. Nothing re-activates a
downgraded or rejected artifact: re-entry needs a new fit and the full gate. Labeling itself performs only
`fitted -> accepted | rejected` (CalibratorRepo, tenant rows) and `active -> downgraded` (SystemCalls, drift job).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, Literal

CalibratorStatus = Literal["fitted", "rejected", "accepted", "active", "downgraded", "retired"]
DowngradeReason = Literal["drift_ece", "drift_ci"]
RetireReason = Literal["replaced", "manual", "profile_version_retired"]

TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    "fitted": frozenset({"accepted", "rejected", "retired"}),
    "accepted": frozenset({"active", "retired"}),
    "active": frozenset({"downgraded", "retired"}),
    "rejected": frozenset({"retired"}),
    "downgraded": frozenset({"retired"}),
    "retired": frozenset(),
}


class InvalidTransition(ValueError):
    """A lifecycle move that ADR-003 does not allow."""


def check_transition(current: str, target: str) -> None:
    """Raise `InvalidTransition` unless `current -> target` is a lifecycle edge."""
    allowed = TRANSITIONS.get(current)
    if allowed is None or target not in allowed:
        raise InvalidTransition(f"calibrator transition {current} -> {target} is not allowed")


def check_activation(approved_by: str | None, activated_by: Sequence[str]) -> None:
    """Two-person activation: the approver and the activator are two distinct operators and `activated_by` is
    `[approver, activator]`. Raises `InvalidTransition` otherwise."""
    if approved_by is None or len(activated_by) != 2:
        raise InvalidTransition("activation needs an approval and a second operator")
    if activated_by[0] != approved_by or activated_by[0] == activated_by[1]:
        raise InvalidTransition("the activator must differ from the approver")


def gate_status(passed: bool) -> CalibratorStatus:
    """The status the acceptance gate moves a fitted artifact to."""
    return "accepted" if passed else "rejected"
