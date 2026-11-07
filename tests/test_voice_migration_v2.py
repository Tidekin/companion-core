"""Upgrade real v1 rows only after validating the original signed delivery proof."""

import hashlib
import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature

import companion_delivery_protocol as wire
from companion import cumulative, paid_budget
from companion import delivery_reconciliation as reconciliation
from companion.contracts import CoreError
from companion.store import Store, encode, migrate
from companion.voice_jobs import VoiceJobs
from companion.voice_protocol import VoiceLeaseRef
from tests.support.delivery_reconciliation import decide, prepare, put, signed
from tests.support.delivery_reconciliation import native as current_fixture
from tests.support.voice_jobs import claim, create_tts, prepared

OLD = "0033_model_usage_job_index"
NEW = "0044_voice_owner_authority"


def native(core, tmp_path, **options):
    """Construct a pre-0035 reserve before hashing, using its frozen field set.

    The migration source predates measurement_json. New measurement evidence
    must never be written and then dropped while copying that historical schema.
    Financial reservation, settlement, signature and reconciliation stay real.
    """
    insert, journal = core.store.insert, paid_budget.journal

    def historical_insert(table, **values):
        if table == "model_admissions":
            values.pop("measurement_json", None)
        return insert(table, **values)

    def historical_journal(store, approval, record=None, **kwargs):
        if record and record.get("kind") == "reserve":
            record = {k: v for k, v in record.items() if k != "measurement"}
        return journal(store, approval, record, **kwargs)

    with pytest.MonkeyPatch.context() as frozen_schema:
        frozen_schema.setattr(core.store, "insert", historical_insert)
        frozen_schema.setattr(paid_budget, "journal", historical_journal)
        return current_fixture(core, tmp_path, **options)


def old_database(core, tmp_path):
    path = tmp_path / "v1.sqlite3"
    migrate(path, OLD)
    with sqlite3.connect(path) as dest:
        tables = [
            r[0]
            for r in dest.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'"
            )
        ]
        for table in tables:
            if table == "voice_claims" or table.startswith("memories_fts"):
                continue
            columns = [r[1] for r in dest.execute(f'PRAGMA table_info("{table}")')]
            for row in core.store.rows(f'SELECT * FROM "{table}"'):
                if table == "runtime_meta" and row["key"] in {
                    "voice_incarnation", "voice_restore_required"}:
                    continue
                if table == "voice_jobs" and row["lease_json"]:
                    lease = json.loads(row["lease_json"])
                    for field in ("stop_token", "incarnation", "claim_sequence", "privacy_epoch"):
                        lease.pop(field, None)
                    lease["schema_version"] = 1
                    row["lease_json"] = encode(lease)
                if table == "voice_jobs" and row["window_sha256"] is None:
                    # Frozen v1 schema material, never a current execution permission.
                    row["window_sha256"] = "a" * 64
                dest.execute(f'INSERT INTO "{table}" ({",".join(columns)}) '
                             f'VALUES ({",".join("?" for _ in columns)})',
                             tuple(row[c] for c in columns))
    return path


def dump(path):
    with closing(sqlite3.connect(path)) as connection:
        return list(connection.iterdump())


def test_v1_running_job_becomes_content_free_unknown_with_original_stop_capability(
    core, tmp_path, monkeypatch,
):
    jobs, message_id, _, _ = prepared(core, monkeypatch)
    create_tts(core, jobs, message_id)
    _, response = claim(jobs)
    jobs.input("local", VoiceLeaseRef.from_lease(response.lease))
    path = old_database(core, tmp_path)
    migrate(path, NEW)
    with closing(Store(path)) as upgraded:
        row = upgraded.one("SELECT * FROM voice_jobs")
        assert upgraded.one("SELECT version_num FROM alembic_version")["version_num"] == NEW
        assert row["state"] == "UNKNOWN" and row["content_retired"] == 1
        assert (
            row["stop_token_sha256"]
            == hashlib.sha256(response.lease.lease_token.encode()).hexdigest()
        )
        assert row["input_sha256"] is None and row["lease_json"] is None
        state = upgraded.one("SELECT * FROM voice_node_state")
        assert state["active_job_id"] == row["job_id"] and state["recovery_required"] == 1
        assert row["cleanup_state"] == "PENDING"
        assert upgraded.rows("PRAGMA foreign_key_check") == []
        assert VoiceJobs(upgraded, core.settings, core.clock).session("local").recovery_required


@pytest.mark.parametrize("corruption", [
    "missing", "signature", "body", "message", "window", "job", "lease", "send_check",
])
def test_v1_unknown_preflight_failure_leaves_old_revision_and_all_data_untouched(
    core, tmp_path, corruption,
):
    env = native(core, tmp_path, action="qq.private.reply", voice=True, voice_v1=True)
    proof = env.proof
    if corruption == "signature":
        proof = proof | {"signature": "0" * 128}
    elif corruption == "body":
        proof = proof | {"fence": proof["fence"] + 1}
    elif corruption == "message":
        proof = signed(env, message_id="msg_" + "f" * 32)
    elif corruption == "window":
        proof = signed(env, plan_sha256="f" * 64)
    elif corruption == "job":
        proof = signed(env, voice_check=proof["voice_check"] | {"job_id": "voice_" + "f" * 32})
    elif corruption == "lease":
        proof = signed(env, lease_sha256="f" * 64)
    elif corruption == "send_check":
        proof = signed(env, send_check_sha256="f" * 64)
    if corruption != "missing":
        put(core.store, reconciliation.PREPARED + env.message_id, proof)
    path = old_database(core, tmp_path)
    before = dump(path)
    with pytest.raises((RuntimeError, ValueError, InvalidSignature, CoreError)):
        migrate(path, NEW)
    assert dump(path) == before
    with closing(Store(path)) as unchanged:
        assert unchanged.one("SELECT version_num FROM alembic_version")["version_num"] == OLD
        assert unchanged.one("SELECT input_sha256 FROM voice_jobs")["input_sha256"]


def test_v1_signed_unknown_converts_once_and_original_owner_resolution_still_works(core, tmp_path):
    env = native(core, tmp_path, action="qq.private.reply", voice=True, voice_v1=True)
    put(core.store, reconciliation.PREPARED + env.message_id, env.proof)
    path = old_database(core, tmp_path)
    migrate(path, NEW)
    original_store = core.store
    with closing(Store(path)) as upgraded:
        core.store = upgraded
        try:
            value = reconciliation._meta(upgraded, "voice_reply:" + env.message_id)
            assert "binding" not in value and "binding_sha256" not in value
            assert value["retired"]["kind"] == "legacy_signature"
            assert value["retired"]["verifier"] == hashlib.sha256(
                bytes.fromhex(env.proof["signature"])).hexdigest()
            before = upgraded.rows("SELECT * FROM model_usage")
            prepare(env)
            receipt = decide(env, "CANCELLED")
            assert decide(env, "CANCELLED") == receipt
            assert upgraded.rows("SELECT * FROM model_usage") == before
            changed = env.proof | {"voice_check": env.proof["voice_check"] | {"generation": 2}}
            with pytest.raises(CoreError):
                reconciliation.prepare(upgraded, core.settings, changed, core.clock())
        finally:
            core.store = original_store


def historical_migration():
    path = Path(__file__).parents[1] / "migrations/versions/0034_voice_retention_v2.py"
    spec = importlib.util.spec_from_file_location("historical_voice_v1", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v1_migration_independent_of_current_dto_and_service(core, tmp_path, monkeypatch):
    env = native(core, tmp_path, action="qq.private.reply", voice=True, voice_v1=True)
    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(reconciliation, "_voice_binding", historical_migration()._voice_binding)
        prepare(env)
        decide(env, "CANCELLED")
    path = old_database(core, tmp_path)

    def unavailable(*args, **kwargs):
        raise AssertionError("historical migration called current runtime")

    monkeypatch.setattr(reconciliation, "_binding", unavailable)
    monkeypatch.setattr("companion.voice_protocol.VoiceDeliveryBinding.model_validate", unavailable)
    monkeypatch.setattr(
        "companion.voice_protocol.LegacyVoiceDeliveryBinding.model_validate", unavailable)
    monkeypatch.setattr(cumulative, "fold", unavailable)
    monkeypatch.setattr(wire, "validate_receipt", unavailable)
    migrate(path, NEW)
    with closing(Store(path)) as upgraded:
        assert upgraded.one("SELECT version_num FROM alembic_version")["version_num"] == NEW
        assert reconciliation._meta(upgraded, "voice_reply:" + env.message_id)["retired"]


@pytest.mark.parametrize("quota", [False, True])
def test_v1_closed_resolution_keeps_original_financial_record(core, tmp_path, monkeypatch, quota):
    env = native(core, tmp_path, quota=quota, action="qq.private.reply", voice=True, voice_v1=True)
    # Build a real native v1 owner resolution using its frozen historical voice validator.
    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(reconciliation, "_voice_binding", historical_migration()._voice_binding)
        prepare(env)
        original_receipt = decide(env, "CANCELLED")
    assert core.store.one("SELECT lease_token FROM outbox WHERE message_id=?",
                          (env.message_id,))["lease_token"] is None
    path = old_database(core, tmp_path)
    migrate(path, NEW)
    original_store = core.store
    with closing(Store(path)) as upgraded:
        core.store = upgraded
        try:
            assert decide(env, "CANCELLED") == original_receipt
            actual = reconciliation.lookup(upgraded, core.settings, env.message_id)
            assert actual == original_receipt["receipt"]
        finally:
            core.store = original_store


@pytest.mark.parametrize("field,error", [
    ("source_sha256", "DELIVERY_RESOLUTION_RECEIPT_INVALID"),
    ("core_stop_sha256", "original stopped execution required"),
    ("prior_liability_sha256", "unbound owner send resolution"),
    ("policy_sha256", "unbound owner send resolution"),
    ("prior_quota_state", "unbound owner send resolution"),
])
def test_v1_rehashed_invalid_financial_record_fails_before_writes(
    core, tmp_path, monkeypatch, field, error,
):
    env = native(core, tmp_path, action="qq.private.reply", voice=True, voice_v1=True)
    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(reconciliation, "_voice_binding", historical_migration()._voice_binding)
        prepare(env)
        original = decide(env, "CANCELLED")["receipt"]
    journal = Path(core.settings.paid_model.journal_path)
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    previous, replacement = "0" * 64, None
    for row in rows:
        old_hash = row.pop("sha256")
        row["previous"] = previous
        if old_hash == original["record_sha256"]:
            row[field] = "RESERVED" if field == "prior_quota_state" else "f" * 64
        row["sha256"] = wire.digest(row)
        previous = row["sha256"]
        if old_hash == original["record_sha256"]:
            replacement = row["sha256"]
            assert row["quiescence"] == env.proof
    assert replacement and replacement != original["record_sha256"]
    journal.write_text("".join(encode(row) + "\n" for row in rows), encoding="utf-8")
    put(core.store, reconciliation.PROJECTION + env.message_id, replacement)
    path = old_database(core, tmp_path)
    before = dump(path)
    with pytest.raises(ValueError, match=error):
        migrate(path, NEW)
    assert dump(path) == before


def test_v1_upgrade_preserves_json_constraints_and_allows_retired_null_lease(
    core, tmp_path, monkeypatch,
):
    jobs, message_id, _, _ = prepared(core, monkeypatch)
    create_tts(core, jobs, message_id)
    path = old_database(core, tmp_path)
    migrate(path, NEW)
    with closing(Store(path)) as upgraded:
        for column in ("config_json", "lease_json"):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                upgraded.execute(f"UPDATE voice_jobs SET {column}=?", ("invalid-json",))
        upgraded.execute("UPDATE voice_jobs SET config_json='{}',lease_json=NULL")
        assert upgraded.one("SELECT lease_json,content_retired FROM voice_jobs") == {
            "lease_json": None, "content_retired": 1,
        }
