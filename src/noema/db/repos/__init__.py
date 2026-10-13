"""noema.db.repos (spec-01 §1, spec-02 §7.1): the repositories, the only SQL of the app besides `noema.db.system`.

One class per repository Protocol, each bound to the open transaction (`noema.db.uow.Tx`) of one unit of work and
confined by its RLS scope; `make_unit_of_work(tx)` bundles them for `PgDatabase(engine, make_unit_of_work)`.

Conventions:
- Rows use spec-03 column names; a key that is not a writable column raises ValueError (a bug, never SQL text).
  tenant_id of new rows comes from the uow; project_id too for project tables in a project uow.
- `get` returns None for a missing, deleted or invisible row. Statement errors (unique, foreign-key and RLS
  violations) propagate as asyncpg errors; a lost connection is `NoemaError(internal_error)` (the engine's).
- Lists page by keyset (`after` = the previous page's `next_cursor`, base64url JSON of the last sort key; limit
  1..200); a malformed cursor or limit raises `NoemaError(schema_violation, param="after"|"limit")`.
- Time is the Postgres clock (`now()`), as in spec-04 §2 and spec-11 §5.
"""

from noema.db.repos._base import MAX_LIMIT, decode_cursor, encode_cursor
from noema.db.repos.calibrators import CALIBRATOR_UPDATABLE, PgCalibratorRepo
from noema.db.repos.decisions import PgDecisionRepo
from noema.db.repos.files import FILE_UPDATABLE, PgDerivativeRepo, PgFileRepo
from noema.db.repos.idempotency import IDEM_INLINE_MAX_BYTES, PgIdempotencyRepo, idempotency_body_key
from noema.db.repos.labels import PgLabelRepo
from noema.db.repos.misc import JOBS_CHANNEL, PgAuditRepo, PgJobRepo, PgProjectRepo
from noema.db.repos.sessions import SESSION_UPDATABLE, PgSessionRepo
from noema.db.repos.turns import PgAttemptRepo, PgTurnRepo
from noema.db.repos.unit_of_work import PgUnitOfWork, make_unit_of_work
from noema.db.repos.usage import GROUP_KEYS, PgUsageRepo

__all__ = [
    "CALIBRATOR_UPDATABLE",
    "FILE_UPDATABLE",
    "GROUP_KEYS",
    "IDEM_INLINE_MAX_BYTES",
    "JOBS_CHANNEL",
    "MAX_LIMIT",
    "SESSION_UPDATABLE",
    "PgAttemptRepo",
    "PgAuditRepo",
    "PgCalibratorRepo",
    "PgDecisionRepo",
    "PgDerivativeRepo",
    "PgFileRepo",
    "PgIdempotencyRepo",
    "PgJobRepo",
    "PgLabelRepo",
    "PgProjectRepo",
    "PgSessionRepo",
    "PgTurnRepo",
    "PgUnitOfWork",
    "PgUsageRepo",
    "decode_cursor",
    "encode_cursor",
    "idempotency_body_key",
    "make_unit_of_work",
]
