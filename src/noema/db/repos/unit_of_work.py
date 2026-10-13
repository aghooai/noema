"""`PgUnitOfWork` (spec-02 §7 `UnitOfWork`) and `make_unit_of_work`, the factory the composition root hands to
`PgDatabase`: `PgDatabase(engine, make_unit_of_work)` is the production `Database`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from noema.db.repos.calibrators import PgCalibratorRepo
from noema.db.repos.decisions import PgDecisionRepo
from noema.db.repos.files import PgDerivativeRepo, PgFileRepo
from noema.db.repos.idempotency import PgIdempotencyRepo
from noema.db.repos.labels import PgLabelRepo
from noema.db.repos.misc import PgAuditRepo, PgJobRepo, PgProjectRepo
from noema.db.repos.sessions import PgSessionRepo
from noema.db.repos.turns import PgAttemptRepo, PgTurnRepo
from noema.db.repos.usage import PgUsageRepo
from noema.db.uow import Tx

if TYPE_CHECKING:
    from noema.core.protocols import storage as p
    from noema.db.uow import PgDatabase


class PgUnitOfWork:
    """Every spec-02 §7.1 repository bound to one open transaction (`Tx`) and its RLS scope."""

    __slots__ = (
        "_attempts",
        "_audit",
        "_calibrators",
        "_decisions",
        "_derivatives",
        "_files",
        "_idempotency",
        "_jobs",
        "_labels",
        "_projects",
        "_sessions",
        "_turns",
        "_tx",
        "_usage",
    )

    def __init__(self, tx: Tx) -> None:
        self._tx = tx
        self._sessions = PgSessionRepo(tx)
        self._turns = PgTurnRepo(tx)
        self._attempts = PgAttemptRepo(tx)
        self._decisions = PgDecisionRepo(tx)
        self._files = PgFileRepo(tx)
        self._derivatives = PgDerivativeRepo(tx)
        self._usage = PgUsageRepo(tx)
        self._labels = PgLabelRepo(tx)
        self._calibrators = PgCalibratorRepo(tx)
        self._idempotency = PgIdempotencyRepo(tx)
        self._audit = PgAuditRepo(tx)
        self._projects = PgProjectRepo(tx)
        self._jobs = PgJobRepo(tx)

    @property
    def tx(self) -> Tx:
        """The transaction (connection and scope) the repositories share."""
        return self._tx

    @property
    def sessions(self) -> PgSessionRepo:
        return self._sessions

    @property
    def turns(self) -> PgTurnRepo:
        return self._turns

    @property
    def attempts(self) -> PgAttemptRepo:
        return self._attempts

    @property
    def decisions(self) -> PgDecisionRepo:
        return self._decisions

    @property
    def files(self) -> PgFileRepo:
        return self._files

    @property
    def derivatives(self) -> PgDerivativeRepo:
        return self._derivatives

    @property
    def usage(self) -> PgUsageRepo:
        return self._usage

    @property
    def labels(self) -> PgLabelRepo:
        return self._labels

    @property
    def calibrators(self) -> PgCalibratorRepo:
        return self._calibrators

    @property
    def idempotency(self) -> PgIdempotencyRepo:
        return self._idempotency

    @property
    def audit(self) -> PgAuditRepo:
        return self._audit

    @property
    def projects(self) -> PgProjectRepo:
        return self._projects

    @property
    def jobs(self) -> PgJobRepo:
        return self._jobs


def make_unit_of_work(tx: Tx) -> PgUnitOfWork:
    """The `PgDatabase` factory: the repositories of one unit of work."""
    return PgUnitOfWork(tx)


if TYPE_CHECKING:  # static conformance with the frozen Protocols (spec-02 §7.1)

    def _conforms(u: PgUnitOfWork, db: PgDatabase[PgUnitOfWork]) -> tuple[p.UnitOfWork, p.Database]:
        return u, db

    def _repos(u: PgUnitOfWork) -> tuple[
        p.SessionRepo, p.TurnRepo, p.AttemptRepo, p.DecisionRepo, p.FileRepo, p.DerivativeRepo, p.UsageRepo,
        p.LabelRepo, p.CalibratorRepo, p.IdempotencyRepo, p.AuditRepo, p.ProjectRepo, p.JobRepo,
    ]:  # fmt: skip
        return (
            u.sessions, u.turns, u.attempts, u.decisions, u.files, u.derivatives, u.usage, u.labels, u.calibrators,
            u.idempotency, u.audit, u.projects, u.jobs,
        )  # fmt: skip
