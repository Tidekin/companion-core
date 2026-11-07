import json
from contextlib import closing

import httpx
import pytest
from alembic import command
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError

from companion.api import create_app
from companion.backup import backup, restore
from companion.config import ObservationAdapterConfig
from companion.contracts import CoreError
from companion.privacy import epoch
from companion.source_grants import SourceGrantRegistration, SourceGrantSnapshot, grant_tombstone
from companion.store import Store, migrate
from source_reader.accounts import source_grant
from source_reader.heybox import HeyboxReader
from source_reader.heybox_source import NativeHeyboxSource
from source_reader.network import ReadFailure
from source_reader.worker import read_result, tick
from tests.conftest import run
from tests.support.exploration import configure
from tests.support.heybox_reader import mock_pool
from tests.support.historical_schema import historical_database
from tests.support.input_reply_migration import config_for
from tests.support.observation_capture import observation
from tests.support.plugin_readonly import NOW_DT, grant
from tests.support.source_grants import (
    GRANT_ID,
    URL,
    accepted,
    authorization,
    derived_records,
    lease,
    prepare,
    receipt,
)


@pytest.mark.parametrize("stop", ["expiry", "read_revoke"])
def test_expiry_and_future_read_revocation_retain_existing_history(core, stop):
    snapshot = prepare(core)
    event_id = accepted(core, snapshot)
    derived_records(core, event_id)
    before_epoch = epoch(core.store)
    if stop == "expiry":
        core.clock.advance(3601)
    else:
        result = core.observations.grants.revoke(GRANT_ID)
        assert result["history_erased"] is False
    with pytest.raises(CoreError, match="SOURCE_GRANT_(EXPIRED|REVOKED)"):
        lease(core, snapshot, "read-2")
    event = core.store.one('SELECT * FROM events WHERE event_id=%s', (event_id,))
    assert not event["redacted"] and "private fixture" in event["body_json"]
    assert epoch(core.store) == before_epoch
    assert len(core.ledger.read()) == 1
    assert core.store.one("SELECT content FROM memories")["content"] == "Private derived memory"
    assert core.store.one("SELECT body_md FROM diary_entries")["body_md"] == "Private derived diary"
    assert core.store.one("SELECT state FROM social_candidates")["state"] == "CANDIDATE"


@pytest.mark.parametrize("change", ["expiry", "read_revoke", "rotation", "forget"])
@pytest.mark.parametrize("hot_reload", [False, True])
def test_late_result_cannot_commit_after_authorization_changes(core, change, hot_reload):
    snapshot = prepare(core)
    if hot_reload:
        from companion import operator_config, runtime_config

        core.settings.operator_hot_reload = True
        core.settings.control_token = SecretStr("synthetic-grant-config-" * 3)
        runtime_config.boundary(core.store, core.settings, core.clock, worker="content-worker")
    item = lease(core, snapshot)
    if hot_reload:
        operator_config.update(core.store, core.settings,
            operator_config.ConfigurationChange(expected_version=0,
                                                changes={"chat_time_labels": True}),
            "source-grant-config-save", core.clock())
        assert runtime_config.boundary(core.store, core.settings, core.clock,
                                       worker="content-worker").version == 1
    if change == "expiry":
        core.clock.advance(3601)
    elif change == "read_revoke":
        core.observations.grants.revoke(GRANT_ID)
    elif change == "forget":
        core.observations.grants.forget(GRANT_ID, core.ledger)
    else:
        updated = authorization(core, epoch=2, fingerprint="b" * 64)
        core.observations.grants.register(
            SourceGrantRegistration(adapter_id="heybox", **updated.snapshot())
        )
    with pytest.raises(CoreError):
        core.exploration.complete("heybox", item["request_id"], receipt(item, snapshot))
    assert not core.store.rows("SELECT * FROM observations")
    row = core.store.one(
        'SELECT * FROM exploration_requests WHERE request_id=%s', (item["request_id"],)
    )
    assert row["reserved_requests"] == 1 and row["used_requests"] is None


def test_owner_registry_cannot_be_forged_by_reader_or_bridge_and_contains_no_credentials(core):
    configure(core)
    core.settings.control_token = SecretStr("owner-control-" * 4)
    app = create_app(core.settings)
    app.state.core = core
    body = {"adapter_id": "heybox", **authorization(core).snapshot()}

    async def scenario():
        async with httpx.AsyncClient(
            base_url="http://core", transport=httpx.ASGITransport(app)
        ) as client:
            for token in [
                core.settings.bridge_token,
                core.settings.observation_adapters["heybox"].token,
            ]:
                headers = {"Authorization": "Bearer " + token.get_secret_value()}
                assert (
                    await client.post("/internal/v2/source-grants", json=body, headers=headers)
                ).status_code == 401
                assert (
                    await client.post(
                        f"/internal/v2/source-grants/{GRANT_ID}/forget", headers=headers
                    )
                ).status_code == 401
            headers = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
            response = await client.post("/internal/v2/source-grants", json=body, headers=headers)
            assert response.status_code == 201
            listed = await client.get("/internal/v2/source-grants", headers=headers)
            assert listed.status_code == 200
            assert listed.json()["items"][0]["grant_id"] == GRANT_ID
            assert "cookies" not in listed.text
            rejected = await client.post(
                "/internal/v2/source-grants",
                json={**body, "cookies": {"SESSDATA": "PRIVATE"}},
                headers=headers,
            )
            assert rejected.status_code == 400 and "PRIVATE" not in rejected.text

    run(scenario())
    columns = {row["name"] for row in core.store.rows("PRAGMA table_info(source_grant_revisions)")}
    assert columns == {"grant_id", "epoch", "fingerprint", "expires_at", "created_at"}


def test_open_ended_expiry_requires_explicit_registered_bilibili_authority(core, tmp_path):
    bounded = prepare(core).snapshot()
    unbounded = {**bounded, "expires_at": None}
    registry = core.observations.grants
    with pytest.raises(CoreError, match="SOURCE_GRANT_EXPIRY_REQUIRED"):
        registry.register(SourceGrantRegistration(adapter_id="heybox", **unbounded))
    with pytest.raises(CoreError, match="SOURCE_GRANT_REVISION_CHANGED"):
        registry.require("heybox", SourceGrantSnapshot(**unbounded))
    authority = {key: value for key, value in bounded.items() if key != "expires_at"}
    with pytest.raises(ValidationError):
        SourceGrantRegistration(adapter_id="bili", **authority)
    for source, extra in (("heybox", {"authorization": authority}), ("bilibili", {})):
        path = grant(tmp_path, source, expires_at=None, **extra)
        with pytest.raises(ReadFailure, match="OWNER_ACCOUNT_AUTHORIZATION_REQUIRED"):
            source_grant(path, NOW_DT, source)


def test_grant_migration_preserves_historical_revision_and_event_binding(core, tmp_path):
    snapshot = prepare(core)
    event_id = accepted(core, snapshot)
    prior = "0064_life_topic_items"
    db = historical_database(core.store, tmp_path / "history.db", prior)
    revision = core.store.one("SELECT * FROM source_grant_revisions")
    for target in ("head", prior, "head"):
        if target == prior:
            command.downgrade(config_for(db), prior)
        else:
            migrate(db)
        with closing(Store(db)) as store:
            assert store.rows("SELECT * FROM source_grant_revisions") == [revision]
            assert store.one("SELECT * FROM source_grant_events") == {
                "event_id": event_id, "grant_id": snapshot.grant_id, "grant_epoch": snapshot.epoch,
            }
            assert not store.rows("PRAGMA foreign_key_check")
    with closing(Store(db)) as store:
        store.insert("source_grants", grant_id="grant_" + "2" * 32, source="bilibili",
                     adapter_id="bili", created_at=core.clock())
        store.insert("source_grant_revisions", grant_id="grant_" + "2" * 32, epoch=1,
                     fingerprint="b" * 64, expires_at=None, created_at=core.clock())
    with pytest.raises(RuntimeError, match="BILI_GRANT_DOWNGRADE_REQUIRES"):
        command.downgrade(config_for(db), prior)
    with closing(Store(db)) as store:
        assert store.one("SELECT count(*) n FROM source_grant_revisions")["n"] == 2
        assert not store.rows("PRAGMA foreign_key_check")


def test_registered_revision_and_adapter_are_exact_and_monotonic(core):
    snapshot = prepare(core)
    assert core.observations.grants.register(
        SourceGrantRegistration(adapter_id="heybox", **snapshot.snapshot())
    )["duplicate"]
    for forged in [authorization(core, fingerprint="f" * 64), authorization(core, epoch=2)]:
        with pytest.raises(CoreError, match="REVISION_CHANGED"):
            core.exploration.lease("heybox", forged)
    with pytest.raises(CoreError):
        core.exploration.lease("bili", snapshot)
    with pytest.raises(CoreError, match="REQUIRED"):
        core.exploration.lease("heybox")
    with pytest.raises(ValidationError):
        SourceGrantSnapshot.model_validate({**snapshot.snapshot(), "epoch": True})


def test_forget_erases_derived_closure_and_newest_ledger_prevents_backup_resurrection(
    core, tmp_path
):
    key = Fernet.generate_key()
    before = tmp_path / "before-registration.enc"
    backup(core.settings.database, before, key)
    snapshot = prepare(core)
    event_id = accepted(core, snapshot)
    core.settings.observation_adapters["derived"] = ObservationAdapterConfig(
        source="simulation", token="derived-test-" * 4
    )
    derived = observation(core, 99, source_event_ids=[event_id])
    core.observations.append("derived", derived)
    derived_records(core, derived.event_id)
    after = tmp_path / "after-source.enc"
    backup(core.settings.database, after, key)
    core.observations.grants.forget(GRANT_ID, core.ledger)
    assert len(core.ledger.read()) == 2
    core.observations.grants.forget(GRANT_ID, core.ledger)
    assert len(core.ledger.read()) == 2
    for path, archive in [(tmp_path / "old-empty.db", before), (tmp_path / "old-source.db", after)]:
        restore(archive, path, key, core.settings.erasure_ledger)
        with closing(Store(str(path))) as restored:
            assert restored.one(
                "SELECT 1 FROM runtime_meta WHERE key=?", (grant_tombstone(GRANT_ID),)
            )
            assert not restored.rows("SELECT * FROM observations WHERE redacted=0")
            assert all(row["body_json"] == "{}" for row in restored.rows("SELECT * FROM events"))
            assert not restored.rows("PRAGMA foreign_key_check")
            if archive == after:
                assert restored.one("SELECT status,content FROM memories") == {
                    "status": "FORGOTTEN",
                    "content": "",
                }
                assert not restored.rows("SELECT * FROM memory_vectors")
                assert restored.one("SELECT status,body_md FROM diary_entries") == {
                    "status": "REDACTED",
                    "body_md": "",
                }
                assert restored.one("SELECT state,comparison_text FROM social_candidates") == {
                    "state": "ERASED",
                    "comparison_text": "",
                }
    assert core.store.one('SELECT redacted FROM events WHERE event_id=%s', (derived.event_id,))[
        "redacted"
    ]
    with pytest.raises(CoreError, match="FORGOTTEN"):
        core.observations.grants.register(
            SourceGrantRegistration(adapter_id="heybox", **snapshot.snapshot())
        )


def test_restoring_backup_revokes_future_reads_without_erasing_history(core, tmp_path):
    snapshot = prepare(core)
    event_id = accepted(core, snapshot)
    archive, path, key = tmp_path / "history.enc", tmp_path / "restored.db", Fernet.generate_key()
    backup(core.settings.database, archive, key)
    core.observations.grants.revoke(GRANT_ID)
    restore(archive, path, key, core.settings.erasure_ledger)
    with closing(Store(str(path))) as restored:
        assert restored.one("SELECT revoked_at FROM source_grants")["revoked_at"]
        assert not restored.one("SELECT redacted FROM events WHERE event_id=?", (event_id,))[
            "redacted"
        ]


def test_complete_projection_rebuilds_grant_tombstone_and_clears_late_stale_content(core):
    snapshot = prepare(core)
    event_id = accepted(core, snapshot)
    core.observations.grants.forget(GRANT_ID, core.ledger)
    with core.store.transaction():
        core.store.execute('DELETE FROM runtime_meta WHERE key=%s', (grant_tombstone(GRANT_ID),))
        core.store.execute(
            'UPDATE events SET redacted=0,body_json=%s WHERE event_id=%s',
            ('{"text":"PRIVATE_STALE_CONTENT"}', event_id),
        )
    core.ledger.replay(core.store)
    assert core.store.one(
        'SELECT body_json,redacted FROM events WHERE event_id=%s', (event_id,)
    ) == {"body_json": "{}", "redacted": 1}
    assert core.store.one('SELECT 1 FROM runtime_meta WHERE key=%s', (grant_tombstone(GRANT_ID),))
    assert len(core.ledger.read()) == 2


def test_empty_grant_forget_preserves_unrelated_history(core):
    prepare(core)
    core.settings.observation_adapters["public"] = ObservationAdapterConfig(
        source="simulation", token="public-test-" * 4
    )
    event = observation(core)
    core.observations.append("public", event)
    core.observations.grants.forget(GRANT_ID, core.ledger)
    assert not core.store.one('SELECT redacted FROM events WHERE event_id=%s', (event.event_id,))[
        "redacted"
    ]


def test_missing_attribution_stops_account_worker_before_core_lease_or_public_io(tmp_path):
    async def scenario():
        pool, backend = mock_pool()
        async with NativeHeyboxSource(grant(tmp_path), pool, clock=lambda: NOW_DT) as source:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: pytest.fail("Core must not be contacted")
                )
            ) as client:
                with pytest.raises(ReadFailure, match="METADATA_REQUIRED"):
                    await tick(client, HeyboxReader(None, source), "heybox")
        assert not backend.requests

    run(scenario())


def test_core_cannot_silently_drop_account_lease_binding_before_public_io(tmp_path):
    authority = {"grant_id": GRANT_ID, "epoch": 1, "fingerprint": "d" * 64}

    async def scenario():
        pool, backend = mock_pool()
        async with NativeHeyboxSource(
            grant(tmp_path, authorization=authority), pool, clock=lambda: NOW_DT
        ) as source:
            async with httpx.AsyncClient(
                base_url="http://core",
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"item": {"request_id": "old_core"}})
                ),
            ) as client:
                with pytest.raises(ReadFailure, match="LEASE_MISMATCH"):
                    await tick(client, HeyboxReader(None, source), "heybox")
        assert not backend.requests

    run(scenario())


def test_metadata_excludes_cookie_fingerprint_and_late_local_change_discards_items(tmp_path):
    authority = {"grant_id": GRANT_ID, "epoch": 1, "fingerprint": "d" * 64}
    path = grant(tmp_path, authorization=authority)
    metadata = source_grant(path, NOW_DT, "heybox", required=True)
    assert metadata["fingerprint"] == "d" * 64 and "cookies" not in json.dumps(metadata)

    class ChangingReader:
        def __init__(self):
            self.current = metadata

        def grant_snapshot(self, required=False):
            return self.current

        async def read(self, item, budget):
            budget.reserve()
            self.current = {**metadata, "epoch": 2}
            return [{"text": "PRIVATE_LATE_BODY"}]

    result = run(
        read_result(
            ChangingReader(),
            {"lease_token": "test_lease", "max_bytes": 100, "source_grant": metadata},
        )
    )
    assert result["status"] == "FAILED" and result["items"] == []
    assert "PRIVATE_LATE_BODY" not in json.dumps(result)


def test_legacy_migration_inventory_preserves_unknown_records_without_invented_attribution(
    core, tmp_path,
):
    configure(core)
    event = observation(core, source="heybox")
    event.payload.url = URL
    core.observations.append("heybox", event)
    before = core.store.one('SELECT body_json FROM events WHERE event_id=%s', (event.event_id,))
    database = historical_database(core.store, tmp_path / "grants.db", "0015_article_images")
    migrate(database, "0016_source_grants")
    with closing(Store(database)) as store:
        assert (
            store.one("SELECT body_json FROM events WHERE event_id=?", (event.event_id,)) == before
        )
        assert store.one("SELECT event_id FROM source_grant_legacy_review") == {
            "event_id": event.event_id
        }
        assert not store.rows("SELECT * FROM source_grant_events")
        assert not store.rows("SELECT * FROM source_grants")
        assert not store.rows("PRAGMA foreign_key_check")
