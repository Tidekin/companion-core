"""Bind a hurt appraisal to the delivered reply it is about."""

from alembic import op

revision = "0063_mood_appraisal_targets"
down_revision = "0062_generated_images"
branch_labels = None
depends_on = None


def upgrade():
    for column in ("target_message_id TEXT", "target_sha256 TEXT"):
        op.get_bind().exec_driver_sql("ALTER TABLE mood_appraisals ADD COLUMN " + column)


def downgrade():
    for column in ("target_sha256", "target_message_id"):
        op.get_bind().exec_driver_sql("ALTER TABLE mood_appraisals DROP COLUMN " + column)
