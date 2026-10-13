"""${up_revision}: ${message}

Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from noema.db.alembic.runner import run_sql_scripts

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    run_sql_scripts(${repr(up_revision)})


def downgrade() -> None:
    raise NotImplementedError("production migrations are forward-only (spec-03 §7)")
