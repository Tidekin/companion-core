"""Admit first-party native replies while retaining original delivery states and evidence."""

from alembic import op

revision = 'pg0003_native_chat'
down_revision = 'pg0002_relationship_standing'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('ck_outbox_action_type', 'outbox', type_='check')
    op.create_check_constraint('ck_outbox_action_type', 'outbox',
        "action_type IN ('qq.private.reply','qq.private.proactive','qzone.publish',"
        "'qzone.comment','qq.profile.nickname','qq.profile.signature','qq.profile.avatar',"
        "'qq.private.poke','native.private.reply','native.private.proactive')")


def downgrade():
    raise RuntimeError('CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE')
