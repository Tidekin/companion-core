"""Bounded public native preview beside the original SDK conversation checkpoint."""

import sqlalchemy as sa
from alembic import op

revision = 'pg0004_native_stream'
down_revision = 'pg0003_native_chat'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('agent_conversations', sa.Column('preview_json', sa.Text(), nullable=True))
    op.add_column('agent_conversations', sa.Column('preview_version', sa.BigInteger(),
                                                  server_default='0', nullable=False))
    op.create_check_constraint('ck_native_preview_bound', 'agent_conversations',
        "preview_version >= 0 AND (preview_json IS NULL OR octet_length(preview_json) <= 262144)")


def downgrade():
    raise RuntimeError('CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE')
