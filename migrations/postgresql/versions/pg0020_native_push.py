"""Per-device Android notification opt-ins and original private publication receipts."""

from alembic import op

revision = "pg0020_native_push"
down_revision = "pg0019_memory_vector"


def upgrade():
    op.execute("ALTER TABLE native_devices ADD COLUMN push_session_sequence BIGINT NOT NULL "
               "DEFAULT 0")
    op.execute("""CREATE TABLE native_push_registrations (
        device_id TEXT PRIMARY KEY REFERENCES native_devices(device_id),
        device_revision TEXT NOT NULL REFERENCES native_device_revisions(revision_id),
        session_hash TEXT NOT NULL REFERENCES console_sessions(token_sha256) ON DELETE CASCADE,
        binding_id TEXT NOT NULL UNIQUE,
        project_id TEXT NOT NULL,
        route TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        token_ciphertext TEXT NOT NULL
    )""")
    op.execute("""CREATE TABLE native_push_deliveries (
        notification_id TEXT PRIMARY KEY,
        group_id TEXT NOT NULL,
        message_id TEXT NOT NULL REFERENCES outbox(message_id) ON DELETE CASCADE,
        device_id TEXT NOT NULL,
        binding_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('PENDING','UNKNOWN','ACCEPTED','CANCELLED','FAILED')),
        attempts INTEGER NOT NULL DEFAULT 0,
        next_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        error_code TEXT,
        provider_message_id TEXT,
        UNIQUE(group_id, device_id)
    )""")
    op.execute("""CREATE INDEX native_push_due ON native_push_deliveries(next_at, notification_id)
                  WHERE state='PENDING'""")


def downgrade():
    raise RuntimeError("CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE")
