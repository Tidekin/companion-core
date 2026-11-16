"""A moment is LIFE, or MEMORY when its sources taught her something important."""

from alembic import op

revision = "pg0017_moment_trigger"
down_revision = "pg0016_moment_likes"
branch_labels = None
depends_on = None


def upgrade():
    # No KEEPSAKE moment was ever written; the planned value is superseded by MEMORY.
    op.execute("""DO $$
DECLARE name text;
BEGIN
    SELECT c.conname INTO STRICT name FROM pg_constraint AS c
    WHERE c.conrelid = 'moments'::regclass AND c.contype = 'c'
    AND pg_get_constraintdef(c.oid) LIKE '%KEEPSAKE%';
    EXECUTE format('ALTER TABLE moments DROP CONSTRAINT %I', name);
END $$""")
    op.execute("UPDATE moments SET trigger = 'MEMORY' WHERE trigger = 'KEEPSAKE'")
    op.execute("ALTER TABLE moments ADD CONSTRAINT moments_trigger_check "
               "CHECK (trigger IN ('LIFE','MEMORY'))")


def downgrade():
    raise RuntimeError("CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE")
