"""Index image-review source adjacency for same-transaction proof validation."""

from alembic import op

revision = "0068_image_review_target"
down_revision = "0067_relationship_standing"


def upgrade():
    op.execute(
        "CREATE INDEX idx_memory_image_review_target ON runtime_meta "
        "(json_extract(value,'$.receipt.target_event_id')) "
        "WHERE key GLOB 'memory_reference_input:*' AND json_valid(value) "
        "AND json_extract(value,'$.kind')='image_review'"
    )


def downgrade():
    op.execute("DROP INDEX idx_memory_image_review_target")
