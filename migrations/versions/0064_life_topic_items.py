"""Allow independently due conversation items. Preserve original sources and results."""

from alembic import op

revision = "0064_life_topic_items"
down_revision = "0063_mood_appraisal_targets"

ACTIVE = "kind='conversation' AND state IN ('WAITING','DUE')"


def upgrade():
    db = op.get_bind()
    db.exec_driver_sql("DROP INDEX idx_life_active_conversation")
    db.exec_driver_sql(f"CREATE INDEX idx_life_active_conversation ON "
                       f"life_topics(conversation_id) WHERE {ACTIVE}")


def downgrade():
    db = op.get_bind()
    if db.exec_driver_sql(f"SELECT 1 FROM life_topics WHERE {ACTIVE} "
                          "GROUP BY conversation_id HAVING COUNT(*)>1 LIMIT 1").first():
        raise RuntimeError("LIFE_DOWNGRADE_REQUIRES_EXPLICIT_TOPIC_CLOSURE")
    db.exec_driver_sql("DROP INDEX idx_life_active_conversation")
    db.exec_driver_sql(f"CREATE UNIQUE INDEX idx_life_active_conversation ON "
                       f"life_topics(conversation_id) WHERE {ACTIVE}")
