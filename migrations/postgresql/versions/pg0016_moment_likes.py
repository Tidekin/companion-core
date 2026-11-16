"""The owner's like on a moment; one owner, so one nullable timestamp."""

from alembic import op

revision = "pg0016_moment_likes"
down_revision = "pg0015_moments"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE moments ADD COLUMN liked_at TEXT")


def downgrade():
    raise RuntimeError("CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE")
