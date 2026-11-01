"""Extend native voice custody with source-bound singing; preserve all old rows."""

import hashlib

from alembic import op
from sqlalchemy import Column, Integer, Text

revision = "0061_singing_voice"
down_revision = "0060_mood_appraisals"
branch_labels = None
depends_on = None


def upgrade():
    # Name reflected anonymous historical CHECKs so SQLite recreation retains
    # them alongside the three explicitly replaced constraints.
    convention = {
        "ck": "ck_voice_legacy_%(sql_hash)s",
        "sql_hash": lambda constraint, table: hashlib.sha256(
            str(constraint.sqltext).encode()).hexdigest()[:12],
    }
    with op.batch_alter_table("voice_jobs", naming_convention=convention) as batch:
        for name in ("ck_voice_operation", "ck_voice_mime", "ck_voice_source"):
            batch.drop_constraint(name, type_="check")
        batch.create_check_constraint("ck_voice_operation", "operation IN ('tts','asr','sing')")
        batch.create_check_constraint("ck_voice_mime",
                                      "input_mime IN ('text/plain','audio/wav','application/json')")
        batch.create_check_constraint("ck_voice_source",
            "(operation='tts' AND message_id IS NOT NULL AND source_segment_index IS NULL) OR "
            "(operation='asr' AND message_id IS NULL AND source_segment_index IS NOT NULL "
            "AND source_segment_index BETWEEN 0 AND 63) OR "
            "(operation='sing' AND message_id IS NULL AND source_segment_index IS NULL)")
        batch.add_column(Column("singing_context_json", Text, nullable=False, server_default="{}"))
        batch.add_column(Column("reserved_output_bytes", Integer, nullable=False,
                                server_default="0"))
        batch.add_column(Column("source_budget_at", Text))
        for name in ("source_reserved_requests", "source_reserved_bytes"):
            batch.add_column(Column(name, Integer, nullable=False, server_default="0"))
        for name in ("source_used_requests", "source_used_bytes"):
            batch.add_column(Column(name, Integer))
        batch.create_check_constraint("ck_voice_source_budget",
            "source_reserved_requests BETWEEN 0 AND 4 "
            "AND source_reserved_bytes BETWEEN 0 AND 35651584 "
            "AND (source_used_requests IS NULL OR source_used_requests BETWEEN 0 AND 4) "
            "AND (source_used_bytes IS NULL OR source_used_bytes BETWEEN 0 AND 35651584)")
        batch.create_check_constraint("ck_voice_singing_context",
            "json_valid(singing_context_json) AND json_type(singing_context_json)='object'")
        batch.create_check_constraint("ck_voice_output_reservation",
                                      "reserved_output_bytes BETWEEN 0 AND 58720256")
    op.execute("CREATE UNIQUE INDEX idx_voice_singing_source ON voice_jobs(event_id) "
               "WHERE operation='sing'")
    # Native nullable ADD COLUMN retains the historical inline job FK's CASCADE;
    # SQLite reflection during a batch rebuild loses that delete action.
    op.execute("ALTER TABLE agent_tool_calls ADD COLUMN voice_job_id TEXT "
        "CONSTRAINT fk_agent_tool_voice_job REFERENCES voice_jobs(job_id) "
        "CONSTRAINT ck_agent_tool_voice_binding CHECK "
        "(voice_job_id IS NULL OR (tool_name='sing_song' AND request_id IS NULL))")


def downgrade():
    raise RuntimeError("SINGING_DOWNGRADE_REQUIRES_EXPLICIT_CUSTODY_RECONCILIATION")
