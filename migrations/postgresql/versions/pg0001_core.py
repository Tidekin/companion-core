"""Native Core baseline preserving the business schema through SQLite revision 0068."""

from pathlib import Path

from alembic import op

revision = "pg0001_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    directory = Path(__file__).resolve().parents[1]
    # Native PostgreSQL DDL contains literal percent signs, not bind parameters.
    connection = op.get_bind()
    for name in ("schema.sql", "constraints.sql"):
        connection.exec_driver_sql(
            (directory / name).read_text("utf8"), execution_options={"no_parameters": True},
        )


def downgrade():
    raise RuntimeError("CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE")
