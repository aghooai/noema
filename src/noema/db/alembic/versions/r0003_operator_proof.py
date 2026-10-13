"""0003_operator_proof (Wave E): the operator check proves the operator key to the database (spec-03 §4, IMPL-044).

`admin._operator` (the check every operator-facing admin.* function runs) requires the connection's session setting
`noema.operator_key` to prove the key of the operator named by `p_operator_id` (HMAC-SHA256 against
`platform.operator.key_hash`, computed by the new internal helper `admin._hmac_sha256`). A `noema_cli` connection
therefore acts only as the operator whose secret it holds, and the two-person rule of config approval and
calibrator activation no longer trusts a plain operator id. No table, column or granted function changes.

SQL: `sql/0003_operator_proof/*.sql`; the CI/test-only downgrade runs `sql/0003_operator_proof/down/*.sql`.
"""

from noema.db.alembic.runner import run_downgrade_scripts, run_sql_scripts

revision = "0003_operator_proof"
down_revision: str | None = "0002_c2a_requests"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    run_sql_scripts("0003_operator_proof")


def downgrade() -> None:
    run_downgrade_scripts("0003_operator_proof")  # CI/test only; refuses outside NOEMA_ENV dev/test
