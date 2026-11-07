"""Encrypted native PostgreSQL backups and offline restore with erasure/authority fences."""

import os
import re
import subprocess
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet
from pydantic import SecretStr

from companion.contracts import CoreError
from companion.executive import restore_executive
from companion.postgres import Database, migrate
from companion.privacy import ErasureLedger, advance_epoch
from companion.store import Store, uid, utcnow


@contextmanager
def _client(database, directory):
    if not isinstance(database, SecretStr):
        raise TypeError("PostgreSQL backup requires SecretStr connection information")
    with psycopg.connect(database.get_secret_value(), autocommit=True, connect_timeout=5) as db:
        parameters = db.info.get_parameters()
        parameters.pop("service", None)
        parameters.pop("passfile", None)
        parameters.update(host=db.info.host, port=str(db.info.port), user=db.info.user,
                          dbname=db.info.dbname)
        password = db.info.password
        if password:
            if "\n" in password or "\r" in password:
                raise CoreError("POSTGRES_PASSWORD_FILE_VALUE_INVALID", 503)
            secret = Path(directory) / "pgpass"
            escaped = password.replace("\\", "\\\\").replace(":", "\\:")
            with open(
                secret, "x", opener=lambda path, flags: os.open(path, flags, 0o600),
            ) as stream:
                stream.write("*:*:*:*:" + escaped + "\n")
            parameters["passfile"] = str(secret)
        if any(value != value.strip() or "\n" in value or "\r" in value
               for value in parameters.values()):
            raise CoreError("POSTGRES_SERVICE_FILE_VALUE_INVALID", 503)
        service = Path(directory) / "pg_service.conf"
        with open(service, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            stream.write("[companion_backup]\n" + "".join(
                name + "=" + value + "\n" for name, value in parameters.items()))
        environment = dict(os.environ, PGSERVICEFILE=str(service))
        environment.pop("PGPASSWORD", None)
        yield db, environment


def _run(tool, arguments, environment):
    # Credentials are in native private files, never in argv, output or error messages.
    result = subprocess.run([tool, "--no-password", "--dbname=service=companion_backup",
                             *arguments], env=environment, capture_output=True, timeout=1200)
    if result.returncode:
        raise CoreError("POSTGRES_BACKUP_TOOL_FAILED", 503)
    return result.stdout


def backup(database, destination, key):
    """Encrypt one consistent custom-format pg_dump; keep independent journals separately."""
    target = Path(destination)
    if target.exists():
        raise CoreError("BACKUP_DESTINATION_EXISTS")
    cipher = Fernet(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as directory:
        snapshot = Path(directory) / "snapshot.dump"
        with _client(database, directory) as (db, environment):
            schema = db.execute("SELECT current_schema()").fetchone()[0]
            if not schema or schema in {"pg_catalog", "information_schema"}:
                raise CoreError("POSTGRES_BACKUP_SCHEMA_REQUIRED", 503)
            _run("pg_dump", ["--format=custom", "--no-owner", "--no-privileges",
                             "--schema=" + schema, "--file=" + str(snapshot)], environment)
        with target.open("xb") as stream:
            stream.write(cipher.encrypt(snapshot.read_bytes()))
            stream.flush()
            os.fsync(stream.fileno())


def restore(source, destination, key, ledger_path, *, state_directory):
    """Restore offline into an empty database, then replay newest erasures and revoke authority.

    The operator keeps the target disconnected from API/workers until this completes.
    pg_restore recreates data before constraints in one transaction; existing databases
    with user tables are refused. No provider request or delivery is replayed.
    """
    ledger = ErasureLedger(ledger_path)
    ledger.read()
    payload = Fernet(key).decrypt(Path(source).read_bytes())
    root = Path(state_directory)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        snapshot = Path(directory) / "restored.dump"
        snapshot.write_bytes(payload)
        with _client(destination, directory) as (db, environment):
            if db.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                          "WHERE c.relkind IN ('r','p') AND n.nspname NOT IN "
                          "('pg_catalog','information_schema') LIMIT 1").fetchone():
                raise CoreError("RESTORE_REQUIRES_EMPTY_DATABASE")
            # Schema-filtered dumps omit extension dependencies; this is the native baseline's
            # sole extension. Index definitions and original data are restored unchanged.
            db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
            # template0 already contains public, and the extension needs that schema.
            # Native pg_restore's TOC selection skips only its duplicate CREATE SCHEMA;
            # every table, index, datum and constraint remains in the atomic restore.
            listing = _run("pg_restore", ["--list", str(snapshot)], environment)
            selection = Path(directory) / "restore.list"
            selection.write_bytes(b"\n".join(
                line for line in listing.splitlines()
                if not re.match(rb"^\d+; \d+ \d+ SCHEMA - public(?:\s|$)", line)
            ) + b"\n")
            _run("pg_restore", ["--exit-on-error", "--single-transaction", "--no-owner",
                                "--no-privileges", "--use-list=" + str(selection),
                                str(snapshot)], environment)
    migrate(destination)
    with Database(destination) as database, closing(Store(database, root)) as store:
        _fence_restored(store, ledger)
        if not store.schema_capabilities() or store.one(
            "SELECT 1 FROM pg_constraint WHERE connamespace=current_schema()::regnamespace "
            "AND NOT convalidated LIMIT 1"
        ):
            raise CoreError("BACKUP_INTEGRITY_FAILED")
        from companion import image_spool

        image_spool.discard(store, (row["item_id"] for row in store.rows(
            "SELECT item_id FROM content_items WHERE kind IN ('qq_image','qq_file') "
            "AND (redacted=1 OR state='ERASED')")))


def _fence_restored(store, ledger):
    identity = store.one("SELECT value FROM runtime_meta WHERE key = 'ledger_id'")
    if not identity or identity["value"] != ledger.read()[0]["ledger_id"]:
        raise CoreError("ERASURE_LEDGER_MISMATCH", 503)
    ledger.replay(store)
    # A snapshot cannot authorize a device session that existed before restoration.
    with store.transaction(control_write=True):
        from companion.owner_authority import restore as restore_owner_authority

        restore_owner_authority(store)
        from companion.gateway import restore as restore_gateway

        restore_gateway(store)
        store.execute(
            "UPDATE outbox SET state = 'DELIVERY_UNCERTAIN', lease_token = NULL, "
            "last_error_code = 'RESTORED_DELIVERY_REQUIRES_RECONCILIATION' WHERE state IN "
            "('PENDING', 'LEASED', 'SENDING', 'RETRY_WAIT')"
        )
        store.execute("UPDATE model_usage SET state = 'UNKNOWN' WHERE state = 'RESERVED'")
        store.execute(
            "UPDATE jobs SET state = 'CANCELLED', lease_token = NULL, lease_until = NULL, "
            "updated_at = %s, last_error_code = 'RESTORED_EVALUATION_REQUIRES_REVIEW' "
            "WHERE kind = 'evaluation' AND state IN ('QUEUED', 'RUNNING', 'RETRY_WAIT')",
            (utcnow(),),
        )
        store.execute(
            "UPDATE jobs SET state = 'CANCELLED', lease_token = NULL, lease_until = NULL, "
            "updated_at = %s, last_error_code = 'RESTORED_POKE_REQUIRES_RECONCILIATION' "
            "WHERE kind = 'poke' AND state IN ('QUEUED', 'RUNNING', 'RETRY_WAIT')",
            (utcnow(),),
        )
        store.execute("UPDATE meme_discovery_reservations SET state = 'UNKNOWN', "
            "updated_at = %s WHERE state = 'RESERVED'", (utcnow(),))
        store.execute(
            "UPDATE jobs SET state = 'CANCELLED', lease_token = NULL, lease_until = NULL, "
            "updated_at = %s, last_error_code = 'RESTORED_DISCOVERY_REQUIRES_RECONCILIATION"
            "' WHERE kind IN ('meme_discovery', 'meme_caption') AND state IN ('QUEUED', "
            "'RUNNING', 'RETRY_WAIT')",
            (utcnow(),),
        )
        # A backup cannot re-authorize a consumed stopped memory-control grant.
        # This marker is deliberately independent of model/window counters.
        store.execute('INSERT INTO runtime_meta (key, value) VALUES (%s, %s) ON '
            'CONFLICT(key) DO UPDATE SET value = excluded.value, record_sequence = DEFAULT',
                      ("memory_control_restore_generation", uid("restore")))
        # The backup cannot prove completeness of model, ASR or tool
        # charges incurred after it. No cloud admission until an
        # independent complete accounting baseline is recovered.
        store.execute('INSERT INTO runtime_meta (key, value) VALUES '
            "('cloud_budget_restore_required', '1') ON CONFLICT(key) DO UPDATE SET value = "
            'excluded.value, record_sequence = DEFAULT')
        if store.one('SELECT 1 FROM model_admissions LIMIT 1') or store.one(
            "SELECT 1 FROM runtime_meta WHERE key IN ('live_text_window', "
            "'model_budget_live_lifecycle') LIMIT 1"
        ):
            store.execute(
                'INSERT INTO runtime_meta (key, value) VALUES '
                "('model_budget_restore_required', '1') ON CONFLICT(key) DO UPDATE SET "
                'value = excluded.value, record_sequence = DEFAULT'
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
            "UPDATE media_sessions SET state = 'CLOSED', closed_at = %s, close_reason = "
            "'RESTORED_SESSION_REVOKED' WHERE state = 'OPEN'",
            (utcnow(),),
        )
        store.execute("UPDATE media_requests SET state = 'UNKNOWN' WHERE state = "
            "'RESERVED'")
        store.execute(
            "UPDATE content_items SET state = 'UNKNOWN', lease_token = NULL, lease_until = "
            "NULL, error_code = 'RESTORED_CONTENT_REQUIRES_RECONCILIATION' WHERE state IN "
            "('WAITING', 'QUEUED', 'FETCHING', 'RUNNING')"
        )
        store.execute(
            "UPDATE exploration_requests SET state = 'UNKNOWN', lease_token = NULL, "
            "last_error_code = 'RESTORED_READ_REQUIRES_RECONCILIATION' WHERE state IN "
            "('QUEUED', 'RUNNING')"
        )
        store.execute(
            "UPDATE reading_attempts SET state = 'UNKNOWN', updated_at = %s WHERE state = "
            "'RUNNING'", (utcnow(),),
        )
        if store.execute(
            'UPDATE consents SET revoked_at = %s WHERE revoked_at IS NULL', (utcnow(),)
        ):
            advance_epoch(store)
        # A restored DB cannot restore authority for future account reads.
        store.execute(
            'UPDATE source_grants SET revoked_at = COALESCE(revoked_at, %s)', (utcnow(),)
        )
