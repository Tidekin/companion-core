"""Reader upgrades, restored attempt custody, and durable source erasure."""

import hashlib
import json
from contextlib import closing

import pytest
from alembic import command
from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.config import ObservationAdapterConfig
from companion.contracts import CoreError
from companion.core import Core
from companion.reading import ReadingService
from companion.reading_protocol import ReadObservation
from companion.source_grants import ReaderReadValidation
from companion.store import Store, after, encode, migrate
from tests.conftest import FakeModel
from tests.support.exploration import configure
from tests.support.input_recall import incoming, recall
from tests.support.input_reply_migration import config_for
from tests.support.reading_protocol import envelope
from tests.support.release_migration import (
    TIMES,
    assert_old_columns_unchanged,
    seed_legacy,
    table_snapshot,
)
from tests.support.source_grants import prepare

READING_REVISION = "0024_reading_attempts"
PREVIOUS = "0023_meme_discovery_reservations"


def test_empty_reading_migration_is_reversible_and_repeated_upgrade_is_safe(tmp_path):
    database = tmp_path / "empty.db"
    migrate(str(database), READING_REVISION)
    migrate(str(database), READING_REVISION)
    with closing(Store(str(database))) as store:
        assert store.one("SELECT version_num FROM alembic_version")["version_num"] == (
            READING_REVISION)
        assert store.rows("SELECT * FROM reading_attempts") == []
        assert store.one("PRAGMA integrity_check")["integrity_check"] == "ok"
        assert store.rows("PRAGMA foreign_key_check") == []
    command.downgrade(config_for(database), PREVIOUS)
    with closing(Store(str(database))) as store:
        assert not store.one("SELECT 1 FROM sqlite_master WHERE name='reading_attempts'")
    migrate(str(database))
    with closing(Store(str(database))) as store:
        assert store.rows("SELECT * FROM reading_attempts") == []
        assert store.schema_capabilities()


def test_reading_upgrade_preserves_legacy_reservations_and_blocks_lossy_downgrade(tmp_path):
    database, _, _ = seed_legacy(tmp_path, "0003_memory_index")
    migrate(str(database), PREVIOUS)
    tables = ("events", "jobs", "exploration_requests", "exploration_evidence")
    with closing(Store(str(database))) as store:
        store.insert(
            "exploration_requests", request_id="read_legacy", request_key="legacy-reserved",
            adapter_id="bili", operation="read",
            url="https://www.bilibili.com/video/BV1xx411c7mD", state="RUNNING",
            lease_token="reader_lease_legacy", lease_until=after(TIMES["keep"], 30),
            captured_privacy_epoch=3, reserved_requests=3, reserved_bytes=4096,
            started_at=TIMES["keep"], created_at=TIMES["keep"], updated_at=TIMES["keep"],
        )
        store.insert("exploration_evidence", request_id="read_legacy", event_id="evt_keep")
        before = table_snapshot(store, tables)
    migrate(str(database), READING_REVISION)
    migrate(str(database), READING_REVISION)
    with closing(Store(str(database))) as store:
        assert_old_columns_unchanged(store, before)
        # A legacy lease must not acquire the new protocol's authority implicitly.
        assert store.rows("SELECT * FROM reading_attempts") == []
        row = store.one("SELECT * FROM exploration_requests WHERE request_id='read_legacy'")
        store.insert(
            "reading_attempts", request_id=row["request_id"], adapter_id=row["adapter_id"],
            lease_token=row["lease_token"], lease_until=row["lease_until"],
            context_json=encode({"root_task_id": "job_keep", "source": "bilibili",
                                 "source_event_ids": ["evt_keep"], "privacy_epoch": 3}),
            allowed_hosts_json="[]", original_url_sha256=hashlib.sha256(
                row["url"].encode(),
            ).hexdigest(), state="RUNNING", created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        protected = table_snapshot(store, (*tables, "reading_attempts"))
    with pytest.raises(RuntimeError, match="READING_DOWNGRADE_REQUIRES_EXPLICIT_DATA_REVIEW"):
        command.downgrade(config_for(database), PREVIOUS)
    with closing(Store(str(database))) as store:
        assert store.one("SELECT version_num FROM alembic_version")["version_num"] == (
            READING_REVISION
        )
        assert table_snapshot(store, (*tables, "reading_attempts")) == protected
        assert store.rows("PRAGMA foreign_key_check") == []


def reading_fixture(core, source="web"):
    grant = prepare(core) if source == "heybox" else None
    if source == "web":
        configure(core)
        core.settings.web_reading_enabled = True
        core.settings.web_reading_hosts = ("articles.example",)
        core.settings.observation_adapters["web"] = ObservationAdapterConfig(
            source="web", token="web-reader-test-" * 4,
        )
    owner_id = incoming(core, text="请阅读我分享的资料。")
    service = ReadingService(core.store, core.settings, core.exploration, core.clock)

    def lease(key):
        body = envelope(source, grant=grant.snapshot() if grant else None)
        with core.store.transaction():
            request_id = core.exploration.create(
                source, "read", key, body["observations"][0]["url"],
                source_event_ids=[owner_id],
            )
        item = service.lease(source, grant)
        assert item is not None and item["request_id"] == request_id
        body["lease_token"] = item["lease_token"]
        observation = body["observations"][0]
        observation["provenance"] = item["context"] | (
            {"source_grant": item["source_grant"]} if grant else {}
        )
        observation["fetched_at"] = observation["evidence"]["fetched_at"] = core.clock()
        return item, ReadObservation.model_validate(body)

    return owner_id, service, lease


def test_restore_quarantines_running_read_but_keeps_late_metering_identity(core, tmp_path):
    _, service, lease = reading_fixture(core)
    item, result = lease("restore-running")
    before = core.store.one('SELECT * FROM exploration_requests WHERE request_id=%s',
                            (item["request_id"],))
    attempt = core.store.one('SELECT * FROM reading_attempts WHERE request_id=%s',
                             (item["request_id"],))
    validation = ReaderReadValidation(lease_token=item["lease_token"])
    assert service.validate_read("web", item["request_id"], validation) == {"valid": True}
    archive, destination = tmp_path / "running.enc", tmp_path / "restored.db"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    restore(archive, destination, key, core.settings.erasure_ledger)
    restored = Core(core.settings.model_copy(update={"database": str(destination)}),
                    FakeModel(), core.clock)
    try:
        s = restored.store
        row = s.one("SELECT * FROM exploration_requests WHERE request_id=?", (item["request_id"],))
        retained = s.one("SELECT * FROM reading_attempts WHERE request_id=?", (item["request_id"],))
        assert row["state"] == retained["state"] == "UNKNOWN"
        assert row["lease_token"] is None
        assert retained["lease_token"] == attempt["lease_token"]
        assert retained["lease_until"] == attempt["lease_until"]
        assert row["reserved_requests"] == before["reserved_requests"] > 0
        assert row["reserved_bytes"] == before["reserved_bytes"] > 0
        assert row["used_requests"] is row["used_bytes"] is None
        reader = ReadingService(s, restored.settings, restored.exploration, restored.clock)
        with pytest.raises(CoreError, match="STALE_READER_LEASE"):
            reader.validate_read("web", item["request_id"], validation)
        with pytest.raises(CoreError, match="STALE_READER_LEASE"):
            reader.complete("web", item["request_id"], result.model_copy(
                update={"lease_token": "reader_lease_wrong"},
            ))
        outcome = reader.complete("web", item["request_id"], result)
        assert outcome == {"state": "CANCELLED", "duplicate": False, "event_ids": []}
        assert reader.complete("web", item["request_id"], result) == {
            **outcome, "duplicate": True,
        }
        metered = s.one("SELECT * FROM exploration_requests WHERE request_id=?",
                        (item["request_id"],))
        assert metered["reserved_requests"] == before["reserved_requests"]
        assert metered["reserved_bytes"] == before["reserved_bytes"]
        assert (metered["used_requests"], metered["used_bytes"]) == (
            result.used_requests, result.used_bytes,
        )
        assert s.rows("SELECT * FROM observations") == []
        assert s.rows("SELECT * FROM outbox") == []
        assert not restored.model.calls
    finally:
        restored.store.close()


def assert_reading_erased(store, owner_id, output_id, items, reservations):
    for event_id in (owner_id, output_id):
        assert store.one("SELECT body_json,redacted FROM events WHERE event_id=?", (event_id,)) == {
            "body_json": "{}", "redacted": 1,
        }
    assert store.one("SELECT evidence_json,canonical_url,redacted FROM observations "
                     "WHERE event_id=?", (output_id,)) == {
        "evidence_json": "{}", "canonical_url": "", "redacted": 1,
    }
    for item in items:
        attempt = store.one("SELECT * FROM reading_attempts WHERE request_id=?",
                            (item["request_id"],))
        assert attempt["context_json"] == "{}"
        assert attempt["allowed_hosts_json"] == "[]"
        assert attempt["source_grant_json"] == "null"
        assert attempt["original_url_sha256"] == ""
        assert attempt["result_event_ids_json"] == "[]"
        assert attempt["lease_token"] == item["lease_token"]
        row = store.one("SELECT * FROM exploration_requests WHERE request_id=?",
                        (item["request_id"],))
        assert row["url"] == "" and row["redacted"] == 1
        assert row["lease_token"] is None
        assert (row["reserved_requests"], row["reserved_bytes"]) == reservations[item["request_id"]]
    assert store.rows("SELECT * FROM exploration_evidence") == []
    assert store.rows("PRAGMA foreign_key_check") == []


@pytest.mark.parametrize(("erasure", "source"), [("recall", "web"), ("forget", "heybox")])
def test_source_erasure_and_backup_replay_clear_reading_authority_and_late_content(
    core, tmp_path, erasure, source,
):
    owner_id, service, lease = reading_fixture(core, source)
    finished, receipt = lease("finished")
    accepted = service.complete(source, finished["request_id"], receipt)
    output_id, = accepted["event_ids"]
    assert accepted["state"] == "DONE"
    pending, late_receipt = lease("pending")
    items = (finished, pending)
    reservations = {row["request_id"]: (row["reserved_requests"], row["reserved_bytes"])
                    for row in core.store.rows("SELECT * FROM exploration_requests")}
    before = core.store.one('SELECT * FROM reading_attempts WHERE request_id=%s',
                            (finished["request_id"],))
    assert json.loads(before["result_event_ids_json"]) == [output_id]
    assert before["context_json"] != "{}" and before["original_url_sha256"]
    if source == "web":
        assert before["allowed_hosts_json"] != "[]"
    else:
        assert before["source_grant_json"] != "null"
    archive, destination = tmp_path / "before-erasure.enc", tmp_path / "erased.db"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    if erasure == "recall":
        recall(core)
    else:
        with core.store.transaction():
            core.ledger.expire_events(core.store, [owner_id], core.clock())
    assert_reading_erased(core.store, owner_id, output_id, items, reservations)
    # Repeated successful receipts must not return the erased output IDs either.
    assert service.complete(source, finished["request_id"], receipt)["event_ids"] == []
    late = service.complete(source, pending["request_id"], late_receipt)
    assert late == {"state": "CANCELLED", "duplicate": False, "event_ids": []}
    assert_reading_erased(core.store, owner_id, output_id, items, reservations)
    restore(archive, destination, key, core.settings.erasure_ledger)
    restored = Core(core.settings.model_copy(update={"database": str(destination)}),
                    FakeModel(), core.clock)
    try:
        s = restored.store
        assert_reading_erased(s, owner_id, output_id, items, reservations)
        reader = ReadingService(s, restored.settings, restored.exploration, restored.clock)
        assert reader.complete(source, finished["request_id"], receipt)["event_ids"] == []
        assert reader.complete(source, pending["request_id"], late_receipt)["event_ids"] == []
        snapshot = table_snapshot(s, ("events", "observations", "reading_attempts"))
        restored.ledger.replay(s)
        assert table_snapshot(s, ("events", "observations", "reading_attempts")) == snapshot
        assert_reading_erased(s, owner_id, output_id, items, reservations)
        assert not restored.model.calls
    finally:
        restored.store.close()


def test_forgetting_grant_without_observations_erases_pending_attempt_and_replays(core, tmp_path):
    owner_id, _, lease = reading_fixture(core, "heybox")
    item, receipt = lease("pending-grant-only")
    assert not core.store.rows("SELECT * FROM source_grant_events")
    before = core.store.one('SELECT * FROM reading_attempts WHERE request_id=%s',
                            (item["request_id"],))
    assert before["context_json"] != "{}" and before["source_grant_json"] != "null"
    assert before["original_url_sha256"]
    archive, destination, key = (
        tmp_path / "grant-before-result.enc", tmp_path / "grant-erased.db", Fernet.generate_key(),
    )
    backup(core.settings.database, archive, key)
    core.observations.grants.forget(receipt.source_grant.grant_id, core.ledger)

    def assert_erased(store):
        attempt = store.one("SELECT * FROM reading_attempts WHERE request_id=?",
                            (item["request_id"],))
        assert attempt["context_json"] == "{}"
        assert attempt["allowed_hosts_json"] == "[]"
        assert attempt["source_grant_json"] == "null"
        assert attempt["original_url_sha256"] == ""
        assert attempt["result_event_ids_json"] == "[]"
        assert attempt["lease_token"] == item["lease_token"]
        assert store.one("SELECT redacted FROM events WHERE event_id=?", (owner_id,)) == {
            "redacted": 0,
        }
        row = store.one("SELECT * FROM exploration_requests WHERE request_id=?",
                        (item["request_id"],))
        assert row["url"] == "" and row["lease_token"] is None
        assert row["reserved_requests"] > 0 and row["reserved_bytes"] > 0
        assert not store.rows("SELECT * FROM observations")

    assert_erased(core.store)
    restore(archive, destination, key, core.settings.erasure_ledger)
    restored = Core(core.settings.model_copy(update={"database": str(destination)}),
                    FakeModel(), core.clock)
    try:
        assert_erased(restored.store)
        reader = ReadingService(restored.store, restored.settings,
                                restored.exploration, restored.clock)
        assert reader.complete("heybox", item["request_id"], receipt) == {
            "state": "CANCELLED", "duplicate": False, "event_ids": [],
        }
        assert_erased(restored.store)
    finally:
        restored.store.close()
