"""Support an explicit open-ended Bilibili grant while retaining immutable revisions."""

from alembic import op

revision = "0065_bili_grant_expiry"
down_revision = "0064_life_topic_items"


def _rebuild(expiry_type):
    # Name both FK columns explicitly. SQLite's original grant_id FK used the
    # implicit parent PK, and the event table refers to this table's composite PK.
    db = op.get_bind()
    if not db.connection.driver_connection.in_transaction:
        db.exec_driver_sql("BEGIN")  # Keep SQLite DDL and the Alembic revision atomic.
    db.exec_driver_sql(
        "CREATE TABLE source_grant_revisions_new ("
        "grant_id TEXT NOT NULL REFERENCES source_grants(grant_id), "
        "epoch INTEGER NOT NULL CHECK(epoch>0), fingerprint TEXT NOT NULL, "
        f"expires_at {expiry_type}, created_at TEXT NOT NULL, PRIMARY KEY(grant_id,epoch))"
    )
    db.exec_driver_sql(
        "INSERT INTO source_grant_revisions_new "
        "SELECT grant_id,epoch,fingerprint,expires_at,created_at FROM source_grant_revisions"
    )
    db.exec_driver_sql("DROP TABLE source_grant_revisions")
    db.exec_driver_sql("ALTER TABLE source_grant_revisions_new RENAME TO source_grant_revisions")


def upgrade():
    _rebuild("TEXT")


def downgrade():
    if op.get_bind().exec_driver_sql(
        "SELECT 1 FROM source_grant_revisions WHERE expires_at IS NULL LIMIT 1"
    ).first():
        raise RuntimeError("BILI_GRANT_DOWNGRADE_REQUIRES_EXPLICIT_DATA_EXPORT")
    _rebuild("TEXT NOT NULL")
