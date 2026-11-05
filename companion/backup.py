import os
import sqlite3
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from companion.contracts import CoreError
from companion.executive import restore_executive
from companion.privacy import ErasureLedger, advance_epoch
from companion.store import Store, migrate, uid, utcnow


def backup(database, destination, key, *, immutable_source=False):
    """Encrypt a SQLite snapshot; immutable_source asserts all writers stay stopped."""
    target = Path(destination)
    if target.exists():
        raise CoreError("BACKUP_DESTINATION_EXISTS")
    source_path = Path(database).resolve()
    if immutable_source and any(os.path.lexists(str(source_path) + suffix)
                                for suffix in ("-wal", "-shm", "-journal")):
        raise CoreError("BACKUP_IMMUTABLE_SOURCE_HAS_SIDECARS")
    # Immutable is an explicit caller assertion: no process may change the source.
    source_uri = source_path.as_uri() + "?mode=ro"
    if immutable_source:
        source_uri += "&immutable=1"
    target.parent.mkdir(parents=True, exist_ok=True)
    cipher = Fernet(key)
    # Stdlib backup API includes committed WAL pages. Temporary directory is OS-private.
    with tempfile.TemporaryDirectory() as directory:
        snapshot = Path(directory) / "snapshot.sqlite3"
        source = sqlite3.connect(source_uri, uri=True)
        dest = sqlite3.connect(snapshot)
        try:
            source.backup(dest)
        finally:
            source.close()
            dest.close()
        with target.open("xb") as stream:
            stream.write(cipher.encrypt(snapshot.read_bytes()))
            stream.flush()
            os.fsync(stream.fileno())


def restore(source, destination, key, ledger_path):
    dest = Path(destination)
    if dest.exists():
        raise CoreError("RESTORE_REQUIRES_NEW_PATH")
    # Never initialize a new ledger during restore: newest independent copy is mandatory.
    ledger = ErasureLedger(ledger_path)
    ledger.read()
    payload = Fernet(key).decrypt(Path(source).read_bytes())
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as directory:
        staged = Path(directory) / "restored.sqlite3"
        staged.write_bytes(payload)
        migrate(str(staged))
        store = Store(str(staged))
        try:
            identity = store.one("SELECT value FROM runtime_meta WHERE key='ledger_id'")
            if not identity or identity["value"] != ledger.read()[0]["ledger_id"]:
                raise CoreError("ERASURE_LEDGER_MISMATCH", 503)
            ledger.replay(store)
            # A snapshot cannot authorize a device session that existed before restoration.
            with store.transaction():
                from companion.owner_authority import restore as restore_owner_authority

                restore_owner_authority(store)
                from companion.gateway import restore as restore_gateway

                restore_gateway(store)
                store.execute(
                    "UPDATE outbox SET state='DELIVERY_UNCERTAIN',lease_token=NULL,"
                    "last_error_code='RESTORED_DELIVERY_REQUIRES_RECONCILIATION' "
                    "WHERE state IN ('PENDING','LEASED','SENDING','RETRY_WAIT')"
                )
                store.execute("UPDATE model_usage SET state='UNKNOWN' WHERE state='RESERVED'")
                store.execute(
                    "UPDATE jobs SET state='CANCELLED',lease_token=NULL,lease_until=NULL,"
                    "updated_at=?,last_error_code='RESTORED_EVALUATION_REQUIRES_REVIEW' "
                    "WHERE kind='evaluation' AND state IN ('QUEUED','RUNNING','RETRY_WAIT')",
                    (utcnow(),),
                )
                store.execute(
                    "UPDATE jobs SET state='CANCELLED',lease_token=NULL,lease_until=NULL,"
                    "updated_at=?,last_error_code='RESTORED_POKE_REQUIRES_RECONCILIATION' "
                    "WHERE kind='poke' AND state IN ('QUEUED','RUNNING','RETRY_WAIT')",
                    (utcnow(),),
                )
                store.execute("UPDATE meme_discovery_reservations SET state='UNKNOWN',"
                              "updated_at=? WHERE state='RESERVED'", (utcnow(),))
                store.execute(
                    "UPDATE jobs SET state='CANCELLED',lease_token=NULL,lease_until=NULL,"
                    "updated_at=?,last_error_code='RESTORED_DISCOVERY_REQUIRES_RECONCILIATION' "
                    "WHERE kind IN ('meme_discovery','meme_caption') "
                    "AND state IN ('QUEUED','RUNNING','RETRY_WAIT')",
                    (utcnow(),),
                )
                # A backup cannot re-authorize a consumed stopped memory-control grant.
                # This marker is deliberately independent of model/window counters.
                store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)",
                              ("memory_control_restore_generation", uid("restore")))
                # The backup cannot prove completeness of model, ASR or tool
                # charges incurred after it. No cloud admission until an
                # independent complete accounting baseline is recovered.
                store.execute("INSERT OR REPLACE INTO runtime_meta VALUES "
                              "('cloud_budget_restore_required','1')")
                if store.one("SELECT 1 FROM model_admissions LIMIT 1") or store.one(
                    "SELECT 1 FROM runtime_meta WHERE key IN "
                    "('live_text_window','model_budget_live_lifecycle') LIMIT 1"
                ):
                    store.execute(
                        "INSERT OR REPLACE INTO runtime_meta VALUES "
                        "('model_budget_restore_required','1')"
                    )
                restore_executive(store, utcnow())
                from companion.bili_video import restore_bili_video
                from companion.social_autonomy import restore_social_autonomy
                from companion.voice_jobs import restore_voice_jobs

                restore_social_autonomy(store, utcnow())
                from companion.delegation import discard_after_restore

                discard_after_restore(store, utcnow())
                restore_voice_jobs(store)
                restore_bili_video(store, utcnow())
                store.execute(
                    "UPDATE media_sessions SET state='CLOSED',closed_at=?,"
                    "close_reason='RESTORED_SESSION_REVOKED' WHERE state='OPEN'",
                    (utcnow(),),
                )
                store.execute("UPDATE media_requests SET state='UNKNOWN' WHERE state='RESERVED'")
                store.execute(
                    "UPDATE content_items SET state='UNKNOWN',lease_token=NULL,lease_until=NULL,"
                    "error_code='RESTORED_CONTENT_REQUIRES_RECONCILIATION' "
                    "WHERE state IN ('WAITING','QUEUED','FETCHING','RUNNING')"
                )
                store.execute(
                    "UPDATE exploration_requests SET state='UNKNOWN',lease_token=NULL,"
                    "last_error_code='RESTORED_READ_REQUIRES_RECONCILIATION' "
                    "WHERE state IN ('QUEUED','RUNNING')"
                )
                store.execute(
                    "UPDATE reading_attempts SET state='UNKNOWN',updated_at=? "
                    "WHERE state='RUNNING'", (utcnow(),),
                )
                if store.execute(
                    "UPDATE consents SET revoked_at=? WHERE revoked_at IS NULL", (utcnow(),)
                ).rowcount:
                    advance_epoch(store)
                # A restored DB cannot restore authority for future account reads.
                store.execute(
                    "UPDATE source_grants SET revoked_at=COALESCE(revoked_at,?)", (utcnow(),)
                )
            if store.one("PRAGMA integrity_check")["integrity_check"] != "ok":
                raise CoreError("BACKUP_INTEGRITY_FAILED")
            if store.rows("PRAGMA foreign_key_check"):
                raise CoreError("BACKUP_FOREIGN_KEYS_FAILED")
            from companion import image_spool

            # Replay operates on a staged DB, while copied originals live beside
            # the final path. Remove exactly its erased image/file IDs before
            # publishing it; ordinary worker startup must not scan this directory.
            image_spool.discard(store, (row["item_id"] for row in store.rows(
                "SELECT item_id FROM content_items WHERE kind IN ('qq_image','qq_file') "
                "AND (redacted=1 OR state='ERASED')")), database=dest)
            store.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # Compact erased cells out of the newly restored file before exposing it.
            store.execute("VACUUM")
        finally:
            store.close()
        os.replace(staged, dest)
