"""Index native source-to-job custody lookups without changing their authority."""

from alembic import op

revision = "pg0006_job_event_lookup"
down_revision = "pg0005_fact_key_ascii"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE INDEX idx_jobs_event_kind ON jobs (event_id,kind)")


def downgrade():
    op.execute("DROP INDEX idx_jobs_event_kind")
