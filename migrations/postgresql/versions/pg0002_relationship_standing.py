"""Match relationship nomination's mixed-direction ordering before LIMIT."""

from alembic import op

revision = "pg0002_relationship_standing"
down_revision = "pg0001_core"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("idx_memories_relationship_standing", table_name="memories")
    op.execute(
        "CREATE INDEX idx_memories_relationship_standing ON memories "
        "(importance DESC,updated_at DESC,memory_id) "
        "WHERE subject='relationship' AND status='CONFIRMED' AND needs_review=0 "
        "AND kind<>'episode' AND origin<>'event_observed' AND privacy_scope='private' "
        "AND confidence>0"
    )


def downgrade():
    raise RuntimeError("CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE")
