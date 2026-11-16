"""Independently revocable native device credentials and immutable identity revisions."""

from alembic import op

revision = 'pg0012_native_devices'
down_revision = 'pg0011_mood_refusal'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE native_device_revisions (
        revision_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
        version BIGINT NOT NULL CHECK(version>0),
        state TEXT NOT NULL CHECK(state IN ('ACTIVE','REVOKED')),
        identity_json TEXT NOT NULL CHECK(jsonb_typeof(identity_json::jsonb)='object'),
        grant_id TEXT NOT NULL, restore_generation TEXT NOT NULL,
        operation_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
        UNIQUE(device_id,version)
    )""")
    op.execute("""CREATE TABLE native_devices (
        device_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL UNIQUE REFERENCES native_device_revisions(revision_id)
    )""")
    op.execute('CREATE INDEX native_device_evidence ON '
               'native_device_revisions(device_id,grant_id,restore_generation)')
    from companion.native_devices import SEED_LEGACY

    op.execute(SEED_LEGACY)


def downgrade():
    raise RuntimeError('CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE')
