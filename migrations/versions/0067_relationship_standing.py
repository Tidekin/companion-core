"""Index bounded standing relationship selection by existing importance."""

from alembic import op

revision = "0067_relationship_standing"
down_revision = "0066_chat_retrieval_bounds"


def upgrade():
    op.execute(
        "CREATE INDEX idx_memories_relationship_standing ON memories "
        "(importance DESC,updated_at DESC,memory_id) "
        "WHERE subject='relationship' AND status='CONFIRMED' AND needs_review=0 "
        "AND kind<>'episode' AND origin<>'event_observed' "
        "AND privacy_scope='private' AND confidence>0"
    )


def downgrade():
    op.execute("DROP INDEX idx_memories_relationship_standing")
