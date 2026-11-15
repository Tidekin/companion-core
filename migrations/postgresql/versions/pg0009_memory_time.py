"""Source-bound event intervals; message and knowledge times retain their existing columns."""

from alembic import op

revision = "pg0009_memory_time"
down_revision = "pg0008_companion_avatar"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE memories ADD COLUMN event_time_json TEXT "
               "CHECK(event_time_json IS NULL OR (octet_length(event_time_json)<=2048 "
               "AND json_typeof(event_time_json::json)='object'))")


def downgrade():
    raise RuntimeError("CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE")
