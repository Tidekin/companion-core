"""Allow the model-judged reason for being pressed after a refusal."""

from alembic import op

revision = "pg0011_mood_refusal"
down_revision = "pg0010_care_plans"
branch_labels = None
depends_on = None

REASONS = ("'OWNER_AFFECTION','OWNER_HURT_BY_REPLY','OWNER_DISTRESS','OWNER_EXPECTATION',"
           "'OWNER_PLAYFUL','OWNER_PRESSED_AFTER_REFUSAL'")


def upgrade():
    # The baseline check is unnamed; replace whichever constraint limits the reason column.
    op.execute("""DO $$
DECLARE name text;
BEGIN
    SELECT c.conname INTO STRICT name FROM pg_constraint AS c
    WHERE c.conrelid = 'mood_appraisals'::regclass AND c.contype = 'c'
    AND pg_get_constraintdef(c.oid) LIKE '%OWNER_PLAYFUL%';
    EXECUTE format('ALTER TABLE mood_appraisals DROP CONSTRAINT %I', name);
END $$""")
    op.execute("ALTER TABLE mood_appraisals ADD CONSTRAINT mood_appraisals_reason_check "
               f"CHECK (reason IN ({REASONS}))")


def downgrade():
    raise RuntimeError("CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE")
