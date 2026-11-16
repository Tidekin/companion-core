"""Owner-only moments written by Life for the native client."""

from alembic import op

revision = "pg0015_moments"
down_revision = "pg0014_keepsakes"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE moments (
        moment_id TEXT PRIMARY KEY,
        source_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
        text TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        trigger TEXT NOT NULL CHECK(trigger IN ('LIFE','KEEPSAKE')),
        source_event_ids_json TEXT NOT NULL CHECK(source_event_ids_json IS JSON ARRAY),
        state TEXT NOT NULL CHECK(state IN ('PUBLISHED','HIDDEN','ERASED')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(state = 'ERASED' OR (length(text) BETWEEN 1 AND 500
                                   AND length(content_sha256) = 64))
    )""")
    op.execute("CREATE INDEX moments_feed ON moments(created_at DESC) WHERE state <> 'ERASED'")


def downgrade():
    raise RuntimeError("CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE")
