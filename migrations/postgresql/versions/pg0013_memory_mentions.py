"""When the owner's own words touched a memory; ordering input, never a fact source."""

from alembic import op

revision = "pg0013_memory_mentions"
down_revision = "pg0012_native_devices"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE memory_mentions (
        memory_id TEXT NOT NULL REFERENCES memories(memory_id),
        event_id TEXT NOT NULL REFERENCES events(event_id),
        mentioned_at TEXT NOT NULL,
        PRIMARY KEY (memory_id, event_id)
    )""")
    op.execute("CREATE INDEX memory_mentions_event ON memory_mentions(event_id)")


def downgrade():
    raise RuntimeError("CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE")
