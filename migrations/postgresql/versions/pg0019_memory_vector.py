"""Select exact semantic nominees across the eligible memory index."""

from alembic import op

from companion.vector_migration import backfill

revision = "pg0019_memory_vector"
down_revision = "pg0018_moment_images"


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    op.execute("ALTER TABLE memory_vectors ADD COLUMN embedding public.vector")
    backfill(op.get_bind().connection.driver_connection)


def downgrade():
    raise RuntimeError("CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE")
