"""Match the native fact-key index to the preserved ASCII identity expression."""

from alembic import op

revision = "pg0002_fact_key_ascii"
down_revision = "pg0001_core"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP INDEX idx_memories_fact_key")
    op.execute("CREATE INDEX idx_memories_fact_key ON memories "
               "(translate(fact_key,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))")


def downgrade():
    op.execute("DROP INDEX idx_memories_fact_key")
    op.execute("CREATE INDEX idx_memories_fact_key ON memories (lower(fact_key))")
