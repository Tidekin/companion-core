"""Owner-granted keepsakes: which trimmed messages still support which memory."""

from alembic import op

revision = "pg0014_keepsakes"
down_revision = "pg0013_memory_mentions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE keepsakes (
        event_id TEXT PRIMARY KEY REFERENCES events(event_id),
        memory_id TEXT NOT NULL REFERENCES memories(memory_id),
        excerpt_sha256 TEXT NOT NULL CHECK(length(excerpt_sha256)=64),
        reason TEXT NOT NULL CHECK(reason IN ('SALIENCE','MENTIONS')),
        kept_at TEXT NOT NULL,
        until TEXT
    )""")
    op.execute("CREATE UNIQUE INDEX keepsakes_memory ON keepsakes(memory_id)")


def downgrade():
    raise RuntimeError("CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE")
