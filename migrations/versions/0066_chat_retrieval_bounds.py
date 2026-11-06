"""Indexed recent dialogue and bounded memory nomination; no responsibility changes."""

from alembic import op

revision = "0066_chat_retrieval_bounds"
down_revision = "0065_bili_grant_expiry"


def upgrade():
    for statement in (
        "CREATE INDEX idx_events_conversation_row ON events(conversation_id)",
        "CREATE INDEX idx_outbox_origin_reply ON outbox "
        "(origin_event_id,conversation_id,action_type,state)",
        "CREATE INDEX idx_memories_nomination ON memories "
        "(needs_review,(status='CONFIRMED') DESC,updated_at DESC,memory_id) "
        "WHERE status IN ('CONFIRMED','HYPOTHESIS') AND privacy_scope='private' AND confidence>0",
        "CREATE INDEX idx_memories_fact_key ON memories(fact_key COLLATE NOCASE)",
        "CREATE INDEX idx_memory_evidence_event ON memory_evidence(event_id,memory_id)",
        "CREATE INDEX idx_memory_evidence_order ON memory_evidence "
        "(memory_id,polarity,created_at,event_id)",
        "CREATE INDEX idx_memory_vectors_model ON memory_vectors(model_version)",
        "CREATE INDEX idx_memory_reference_owner ON runtime_meta "
        "(json_extract(value,'$.owner_event_id'),json_extract(value,'$.kind'),key) "
        "WHERE key GLOB 'memory_reference_input:*' AND json_valid(value)",
    ):
        op.execute(statement)


def downgrade():
    for name in ("idx_memory_reference_owner", "idx_memory_vectors_model",
                 "idx_memory_evidence_order",
                 "idx_memory_evidence_event",
                 "idx_memories_fact_key", "idx_memories_nomination",
                 "idx_outbox_origin_reply", "idx_events_conversation_row"):
        op.execute("DROP INDEX " + name)
