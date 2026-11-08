"""Native pg_dump/restore: newest erasures, unknown costs and delivery fences."""

import re
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet, InvalidToken
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import SecretStr

from companion.backup import backup, restore
from companion.config import Settings
from companion.contracts import Candidate, CoreError
from companion.core import Core
from companion.transport import enqueue
from tests.conftest import Clock, FakeModel, ingest
from tests.postgres_fixture import isolated_native


@contextmanager
def empty_restore_database(source):
    values = conninfo_to_dict(source.get_secret_value())
    name = "restore_" + uuid4().hex
    with psycopg.connect(source.get_secret_value(), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    try:
        yield SecretStr(make_conninfo(**{**values, "dbname": name}))
    finally:
        assert re.fullmatch("restore_[a-f0-9]{32}", name)
        with psycopg.connect(source.get_secret_value(), autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def settings(info, directory, ledger):
    return Settings(
        owner_qq_id="10001",
        bot_qq_id="10002",
        bridge_token="test-token-" * 4,
        database=info,
        state_directory=str(directory),
        erasure_ledger=str(ledger),
        memory_enabled=False,
        operator_hot_reload=False,
        proactive_enabled=False,
    )


def add_fact(core, number, content):
    source = ingest(core, number, content)
    with core.store.transaction(control_write=True):
        identity = core.memory.add(
            Candidate(
                subject="project",
                kind="fact",
                fact_key=str(number),
                content=content,
                evidence_event_id=source["event_id"],
                evidence_quote=content,
            ),
            core.clock(),
            confirmed=True,
        )
    return identity, source


def test_native_restore_replays_newer_erasure_and_preserves_unknown_responsibility(tmp_path):
    with isolated_native() as (_, source_info):
        ledger = tmp_path / "erasures.jsonl"
        source_settings = settings(source_info, tmp_path / "source-state", ledger)
        archive = tmp_path / "before.enc"
        key = Fernet.generate_key()
        with Core(source_settings, FakeModel(), Clock()) as core:
            core.settings.memory_enabled = True
            target, original_event = add_fact(core, 1, "测试行李放在青色柜子")
            independent, event = add_fact(core, 2, "独立收纳盒放在三楼")
            with core.store.transaction():
                erased_file = "content_" + "a" * 32
                core.store.insert(
                    "content_items", item_id=erased_file, owner_event_id=original_event["event_id"],
                    kind="qq_file", slot=0, state="ERASED", captured_privacy_epoch=0,
                    redacted=1, deadline=core.clock(), created_at=core.clock(),
                    updated_at=core.clock(),
                )
                core.store.insert(
                    "model_usage",
                    usage_id="usage",
                    request_id="reserved",
                    scenario="chat",
                    provider="deepseek",
                    model="deepseek-flash",
                    state="RESERVED",
                    billed_micro_usd=73,
                    occurred_at=core.clock(),
                )
                message = enqueue(
                    core.store,
                    event["event_id"],
                    core.store.one(
                        "SELECT conversation_id FROM events WHERE event_id=%s", (event["event_id"],)
                    )["conversation_id"],
                    "合成待发回复",
                    core.clock(),
                    key="restore-test-message",
                )
                core.store.execute(
                    "UPDATE outbox SET state='SENDING',attempt_count=1,delivery_key=%s "
                    "WHERE message_id=%s",
                    ("native-original-delivery-key", message),
                )
            backup(source_info, archive, key)
            core.memory.decide(target, "forget", core.clock())
        with empty_restore_database(source_info) as destination:
            target_settings = settings(destination, tmp_path / "target-state", ledger)
            originals = tmp_path / "target-state" / "owner-images"
            originals.mkdir(mode=0o700, parents=True)
            forgotten = originals / (erased_file + ".raw")
            forgotten.write_bytes(b"synthetic earlier attachment")
            unrelated = originals / ("content_" + "b" * 32 + ".raw")
            unrelated.write_bytes(b"synthetic independent attachment")
            restore(
                archive, destination, key, ledger, state_directory=target_settings.state_directory
            )
            assert not forgotten.exists()
            assert unrelated.read_bytes() == b"synthetic independent attachment"
            with Core(target_settings, FakeModel(), Clock()) as core:
                core.settings.memory_enabled = True
                assert [row["memory_id"] for row in core.memory.search("*", now=core.clock())] == [
                    independent
                ]
                assert core.store.one(
                    "SELECT status FROM memories WHERE memory_id=%s", (target,)
                ) == {"status": "FORGOTTEN"}
                assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
                    "state": "UNKNOWN",
                    "billed_micro_usd": 73,
                }
                assert core.store.one(
                    "SELECT state,attempt_count,delivery_key FROM outbox WHERE message_id=%s",
                    (message,),
                ) == {
                    "state": "DELIVERY_UNCERTAIN",
                    "attempt_count": 1,
                    "delivery_key": "native-original-delivery-key",
                }
                assert core.store.one(
                    "SELECT value FROM runtime_meta WHERE key='cloud_budget_restore_required'"
                ) == {"value": "1"}


def test_restore_refuses_existing_native_database_and_bad_key(tmp_path):
    with isolated_native() as (_, info):
        ledger = tmp_path / "erasures.jsonl"
        configuration = settings(info, tmp_path / "state", ledger)
        with Core(configuration, FakeModel(), Clock()):
            archive = tmp_path / "snapshot.enc"
            key = Fernet.generate_key()
            backup(info, archive, key)
            with pytest.raises(CoreError, match="RESTORE_REQUIRES_EMPTY_DATABASE"):
                restore(archive, info, key, ledger, state_directory=tmp_path / "target")
            with pytest.raises(InvalidToken):
                restore(
                    archive,
                    info,
                    Fernet.generate_key(),
                    ledger,
                    state_directory=tmp_path / "target",
                )


def test_restore_keeps_target_private_until_fenced_and_retries_atomically(
    core, tmp_path, empty_database, monkeypatch,
):
    from companion import backup as native

    archive = tmp_path / "before.enc"
    key = Fernet.generate_key()
    ledger = core.settings.erasure_ledger
    state = tmp_path / "restored-state"
    ingest(core, text="独立恢复测试")
    backup(core.settings.database, archive, key)
    original = native._fence_restored
    stages = []

    with psycopg.connect(empty_database.get_secret_value(), autocommit=True) as observer:
        # An existing view conflicts only at the final pg_restore, after staging succeeds.
        observer.execute("CREATE VIEW events AS SELECT 17 AS sentinel")

        def observe(store, current_ledger):
            assert store.one("SELECT count(*) AS n FROM events")["n"] == 1
            stage = store.one("SELECT current_database() AS name")["name"]
            assert re.fullmatch(r"companion_restore_[0-9a-f]{32}", stage)
            stages.append(stage)
            assert not store.one(
                "SELECT 1 FROM pg_database d, aclexplode(d.datacl) a "
                "WHERE d.datname=current_database() AND a.grantee=0 AND a.privilege_type='CONNECT'"
            )
            assert observer.execute("SELECT sentinel FROM events").fetchone() == (17,)
            assert observer.execute("SELECT to_regclass('jobs')").fetchone() == (None,)
            with pytest.raises(CoreError, match="RESTORE_TARGET_BUSY"):
                restore(archive, empty_database, key, ledger, state_directory=state)
            original(store, current_ledger)

        with monkeypatch.context() as patch:
            patch.setattr(native, "_fence_restored", observe)
            with pytest.raises(CoreError, match="POSTGRES_BACKUP_TOOL_FAILED"):
                restore(archive, empty_database, key, ledger, state_directory=state)
        assert len(stages) == 1
        assert not observer.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (stages[0],)
        ).fetchone()
        assert observer.execute("SELECT sentinel FROM events").fetchone() == (17,)
        assert observer.execute("SELECT to_regclass('jobs')").fetchone() == (None,)
        assert not observer.execute(
            "SELECT 1 FROM pg_extension WHERE extname='pg_trgm'"
        ).fetchone()
        assert list(state.iterdir()) == []
        observer.execute("DROP VIEW events")
        restore(archive, empty_database, key, ledger, state_directory=state)
        assert observer.execute("SELECT event_id FROM events").fetchone() == ("evt_test_1",)
