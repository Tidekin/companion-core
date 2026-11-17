"""A generated picture for one of her moments; bytes live in the state directory."""

from alembic import op

revision = "pg0018_moment_images"
down_revision = "pg0017_moment_trigger"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE moment_images (
        moment_id TEXT NOT NULL REFERENCES moments(moment_id),
        slot INTEGER NOT NULL CHECK(slot = 0),
        state TEXT NOT NULL CHECK(state IN ('QUEUED','RESERVED','DISPATCHED','READY',
                                            'FAILED','UNKNOWN','EXPIRED','ERASED')),
        usage_request_id TEXT UNIQUE,
        mime TEXT,
        sha256 TEXT,
        byte_length INTEGER,
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(moment_id, slot),
        CHECK(state <> 'READY' OR (mime IN ('image/png','image/jpeg')
                                   AND length(sha256) = 64 AND byte_length > 0))
    )""")
    op.execute("CREATE INDEX moment_images_queue ON moment_images(created_at) "
               "WHERE state = 'QUEUED'")


def downgrade():
    raise RuntimeError("CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE")
