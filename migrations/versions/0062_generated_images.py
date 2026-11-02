"""One model-requested picture per owner chat job, delivered with that job's reply."""

from alembic import op

revision = "0062_generated_images"
down_revision = "0061_singing_voice"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE generated_images (
        image_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
        event_id TEXT NOT NULL REFERENCES events(event_id),
        conversation_id TEXT NOT NULL,
        tool_call_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('RESERVED','DISPATCHED','SUCCEEDED','FAILED',
            'UNKNOWN','ERASED')),
        size TEXT NOT NULL,
        prompt_sha256 TEXT NOT NULL,
        usage_request_id TEXT NOT NULL REFERENCES model_usage(request_id),
        mime TEXT,
        sha256 TEXT,
        byte_length INTEGER,
        message_id TEXT,
        error_code TEXT,
        expires_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    op.execute("CREATE INDEX idx_generated_images_created ON generated_images(created_at)")
    op.execute("CREATE INDEX idx_generated_images_event ON generated_images(event_id)")


def downgrade():
    op.execute("DROP TABLE generated_images")
