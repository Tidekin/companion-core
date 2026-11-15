"""Private caring intent, with native event provenance and activity delivery references."""

from alembic import op

revision = "pg0010_care_plans"
down_revision = "pg0009_memory_time"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE care_plans (
        plan_id TEXT PRIMARY KEY,
        source_event_id TEXT NOT NULL REFERENCES events(event_id),
        intent_event_id TEXT NOT NULL REFERENCES events(event_id),
        version BIGINT NOT NULL CHECK(version>0),
        state TEXT NOT NULL CHECK(state IN ('PENDING','QUEUED','CLOSED','ERASED')),
        detail_json TEXT NOT NULL CHECK(
            detail_json IS JSON OBJECT AND octet_length(detail_json)<=4096),
        due_at TEXT,
        expires_at TEXT,
        job_id TEXT REFERENCES jobs(job_id),
        message_id TEXT REFERENCES outbox(message_id),
        reason_code TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(state='ERASED' OR (
            due_at IS NOT NULL AND expires_at IS NOT NULL AND expires_at>due_at))
    )""")
    op.execute("CREATE INDEX care_plans_due ON care_plans(due_at) WHERE state='PENDING'")
    op.execute("CREATE INDEX care_plans_source ON care_plans(source_event_id)")
    op.execute("CREATE INDEX care_plans_intent ON care_plans(intent_event_id)")


def downgrade():
    raise RuntimeError("CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE")
