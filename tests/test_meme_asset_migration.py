"""Offline real CLI, original provenance and independent erasure replay."""

import base64
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from companion import meme_asset_migration as migration
from companion import meme_assets
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.legacy_import import import_sqlite_backup, migrate_sqlite
from companion.meme_core import CaptionBody, MemeCaptionService
from companion.postgres import Database, migrate
from companion.privacy import ErasureLedger
from companion.store import Store, encode
from companion_meme_protocol import digest
from companion_onebot_bridge.meme_catalog import MemeCatalog
from companion_onebot_bridge.meme_library import MemeLibrary
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.postgres_fixture import isolated_database
from tests.support.historical_schema import historical_database
from tests.support.meme_asset_migration import (
    ROOT,
    check_entrypoint_import_isolation,
    cli,
    config_file,
    logical_database,
    original_work,
)
from tests.support.meme_asset_migration import (
    bundle as bundle,
)
from tests.support.meme_core import worker


def core_cli(bundle, operation, **kwargs):
    return cli("companion.meme_asset_migration", "--config", bundle.core_config,
               operation, bundle.manifest, **kwargs)


def make_export(bundle):
    return cli("companion_onebot_bridge.meme_export", "--config", bundle.bridge_config,
               "--output", bundle.manifest)


def rewrite(bundle, change):
    value = json.loads(bundle.manifest.read_text("utf-8"))
    change(value)
    for item in value["assets"]:
        item["sha256"] = digest({k: v for k, v in item.items() if k != "sha256"})
    value["sha256"] = digest({k: v for k, v in value.items() if k != "sha256"})
    bundle.manifest.write_text(encode(value), "utf-8")


def test_real_export_dry_run_import_twice_and_reconcile_leave_original_work_unchanged(bundle):
    core = bundle.core
    before = original_work(core.store)
    source = Path(bundle.bridge.data_dir) / "spool.sqlite3"
    source_before = logical_database(source)
    assert make_export(bundle)["assets"] == 1
    raw = bundle.manifest.read_text("utf-8")
    assert "UNRELATED_PRIVATE_CANARY" not in raw
    assert bundle.bridge.capture_signing_key not in raw
    assert bundle.bridge.bridge_token not in raw
    target_before = logical_database(core.settings.database)
    dry = core_cli(bundle, "dry-run")
    assert dry["items"][0]["state"] == "STAGED" and not dry["matches"]
    assert logical_database(core.settings.database) == target_before
    assert logical_database(source) == source_before
    first = core_cli(bundle, "import")
    assert first["items"][0]["state"] == "STAGED"
    saved = core.store.one("SELECT * FROM meme_assets")
    assert saved["data"] == bundle.data
    assert json.loads(saved["metadata_json"])["license"] == bundle.metadata.license
    assert core_cli(bundle, "import")["matches"]
    assert core_cli(bundle, "reconcile")["matches"]
    assert original_work(core.store) == before
    assert logical_database(source) == source_before


@pytest.mark.parametrize("change", ["owner", "epoch", "public_key", "source", "no_capture"])
def test_unverified_identity_or_source_never_copies_content(bundle, change):
    make_export(bundle)
    if change == "no_capture":
        bundle.core.store.execute('DELETE FROM runtime_meta WHERE key=%s',
                                  ("owner_input:" + bundle.event["event_id"],))
    else:
        def alter(value):
            if change == "owner":
                value["custody"]["identity"][0] = "30001"
                value["custody"]["identity"][2] = "onebot-direct:FriendMessage:30001"
            elif change == "epoch":
                value["custody"]["identity"][3] = "other-epoch"
            elif change == "public_key":
                value["custody"]["capture_public_key"] = "f" * 64
            else:
                item = value["assets"][0]
                item["metadata"]["license"] = "different-license"
                item["source_sha256"] = meme_assets.source_digest(item["metadata"])
        rewrite(bundle, alter)
    if change == "owner":
        assert core_cli(bundle, "import", ok=False) == {"error": "MEME_MIGRATION_REFUSED"}
        assert not bundle.core.store.rows("SELECT * FROM meme_assets")
    else:
        assert core_cli(bundle, "import")["items"][0]["state"] == "QUARANTINED"
        row = bundle.core.store.one("SELECT * FROM meme_assets")
        assert row["data"] is row["metadata_json"] is row["mime"] is None
        assert not bundle.core.store.rows("SELECT * FROM meme_asset_sources")


def test_equal_bytes_keep_distinct_custody_and_source_records(bundle):
    make_export(bundle)
    first = core_cli(bundle, "import")["items"][0]["asset_id"]
    rewrite(bundle, lambda v: v["custody"]["identity"].__setitem__(3, "different-epoch"))
    second = core_cli(bundle, "import")["items"][0]["asset_id"]
    def source_change(value):
        item = value["assets"][0]
        item["metadata"]["usage_basis"] = "another source basis"
        item["source_sha256"] = meme_assets.source_digest(item["metadata"])
    rewrite(bundle, source_change)
    third = core_cli(bundle, "import")["items"][0]["asset_id"]
    assert len({first, second, third}) == 3
    assert bundle.core.store.one("SELECT COUNT(*) AS n FROM meme_assets")["n"] == 3


def test_same_bytes_with_two_original_licensed_sources_remain_independent(bundle):
    make_export(bundle)
    first = core_cli(bundle, "import")["items"][0]
    payload = json.loads(json.dumps(bundle.payload))
    proof = payload["source_evidence"]
    proof["fetched_at"] = proof["acquisition"]["fetch"]["fetched_at"] = (
        "2026-10-25T00:00:01+00:00")
    payload["asset"]["source_evidence_sha256"] = digest(proof)
    service = MemeCaptionService(bundle.core.store, bundle.core.settings, lambda: None,
                                 bundle.core.clock)
    original = service.submit(CaptionBody.model_validate(payload))
    assert original["job_id"] != bundle.job
    assert bundle.core.store.one('SELECT reuse_job_id FROM meme_captions WHERE job_id=%s',
                                 (original["job_id"],))["reuse_job_id"] == bundle.job
    def new_source(value):
        item = value["assets"][0]
        item["metadata"]["fetched_at"] = proof["fetched_at"]
        item["metadata"]["acquisition_json"] = encode(proof["acquisition"])
        item["source_sha256"] = digest(proof)
    rewrite(bundle, new_source)
    second = core_cli(bundle, "import")["items"][0]
    assert first["state"] == second["state"] == "STAGED"
    assert first["asset_id"] != second["asset_id"]
    assert len(bundle.core.store.rows("SELECT * FROM meme_assets")) == 2
    assert len(bundle.core.store.rows("SELECT * FROM meme_asset_sources")) == 3
    with bundle.core.store.transaction(control_write=True):
        bundle.core.ledger.expire_events(bundle.core.store, [bundle.event["event_id"]],
                                         bundle.core.clock())
    assert all(r["state"] == "ERASED" and r["data"] is None and r["metadata_json"] is None
               for r in bundle.core.store.rows("SELECT * FROM meme_assets"))


@pytest.mark.parametrize("change", ["item_hash", "envelope_hash", "bytes", "mime", "unknown_field",
                                    "duplicate", "metadata_path", "version", "removed_bytes"])
def test_corrupt_or_unbounded_manifest_refused_before_import(bundle, change):
    make_export(bundle)
    def alter(value):
        item = value["assets"][0]
        if change == "bytes":
            item["data_base64"] = base64.b64encode(b"not image").decode()
        elif change == "mime":
            item["mime"] = "image/gif"
        elif change == "unknown_field":
            item["file"] = "../../private"
        elif change == "duplicate":
            value["assets"].append(dict(item))
        elif change == "metadata_path":
            item["metadata"]["category"] = "../private"
        elif change == "version":
            value["schema_version"] = True
        elif change == "removed_bytes":
            item["active"] = False
    rewrite(bundle, alter)
    if change in {"item_hash", "envelope_hash"}:
        value = json.loads(bundle.manifest.read_text("utf-8"))
        target = value if change == "envelope_hash" else value["assets"][0]
        target["sha256"] = "0" * 64
        bundle.manifest.write_text(encode(value), "utf-8")
    before = logical_database(bundle.core.settings.database)
    assert core_cli(bundle, "import", ok=False)["error"] == "MEME_MIGRATION_REFUSED"
    assert logical_database(bundle.core.settings.database) == before


def test_conflicting_metadata_refuses_atomic_import_and_removal_never_reactivates(bundle):
    make_export(bundle)
    core_cli(bundle, "import")
    saved = json.loads(bundle.manifest.read_text("utf-8"))
    rewrite(bundle, lambda v: v["assets"][0]["metadata"].update(description="changed content"))
    before = logical_database(bundle.core.settings.database)
    core_cli(bundle, "import", ok=False)
    assert logical_database(bundle.core.settings.database) == before
    bundle.manifest.write_text(encode(saved), "utf-8")
    rewrite(bundle, lambda v: v["assets"][0].update(active=False, data_base64=None, mime=None))
    assert core_cli(bundle, "import")["items"][0]["state"] == "REMOVED"
    bundle.manifest.write_text(encode(saved), "utf-8")
    assert core_cli(bundle, "import")["items"][0]["state"] == "REMOVED"
    assert bundle.core.store.one("SELECT data,metadata_json FROM meme_assets") == {
        "data": None, "metadata_json": None}


def test_erasure_before_import_and_old_backup_restore_reimport_cannot_revive(
    bundle, tmp_path, empty_database
):
    make_export(bundle)
    archive = tmp_path / "old.enc"
    state = tmp_path / "restored-state"
    key = Fernet.generate_key()
    legacy = tmp_path / "pre-catalog-0046.db"
    historical_database(bundle.core.store, str(legacy), "0046_capability_delegation")
    with closing(sqlite3.connect(legacy)) as destination:
        assert destination.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0046_capability_delegation",)
        assert not destination.execute("SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('meme_assets','meme_asset_sources','meme_vectors')").fetchall()
        destination.execute("DELETE FROM runtime_meta WHERE key='derived_erasure_contract'")
        destination.commit()
    migrate_sqlite(legacy)
    with isolated_database() as converted:
        migrate(converted)
        import_sqlite_backup(legacy, converted)
        backup(converted, archive, key)
    before = original_work(bundle.core.store)
    with bundle.core.store.transaction(control_write=True):
        bundle.core.ledger.expire_events(bundle.core.store, [bundle.event["event_id"]],
                                         bundle.core.clock())
    result = core_cli(bundle, "import")
    assert result["items"][0]["state"] == "ERASED"
    assert bundle.core.store.one("SELECT data,metadata_json FROM meme_assets") == {
        "data": None, "metadata_json": None}
    restore(archive, empty_database, key, bundle.core.settings.erasure_ledger,
            state_directory=state)
    settings = bundle.core.settings.model_copy(update={"database": empty_database,
                                                      "state_directory": str(state)})
    assert migration.run(settings, bundle.manifest, operation="import")["items"][0]["state"] == (
        "ERASED")
    with Database(empty_database) as database, closing(Store(database, state)) as store:
        assert store.one("SELECT data,metadata_json FROM meme_assets") == {
            "data": None, "metadata_json": None}
        assert store.rows("SELECT * FROM model_usage") == before["model_usage"]
        assert store.rows("SELECT * FROM model_admissions") == before["model_admissions"]


def test_old_complete_repair_clears_catalog_without_rewriting_original_receipts(bundle):
    make_export(bundle)
    core_cli(bundle, "import")
    asset = bundle.core.store.one("SELECT * FROM meme_assets")
    with bundle.core.store.transaction(control_write=True):
        bundle.core.ledger.expire_events(bundle.core.store, [bundle.event["event_id"]],
                                         bundle.core.clock())
    assert bundle.core.store.one("SELECT state FROM meme_assets")["state"] == "ERASED"
    # Model an old COMPLETE snapshot that omitted this newly registered projection.
    bundle.core.store.execute('UPDATE meme_assets SET state=%s,data=%s,metadata_json=%s,mime=%s',
        (asset["state"], asset["data"], asset["metadata_json"], asset["mime"]))
    bundle.core.store.execute("DELETE FROM runtime_meta WHERE key='derived_erasure_contract'")
    before, ledger = original_work(bundle.core.store), bundle.core.ledger.path.read_bytes()
    bundle.core.ledger.replay(bundle.core.store)
    assert bundle.core.store.one("SELECT state,data,metadata_json,mime FROM meme_assets") == {
        "state": "ERASED", "data": None, "metadata_json": None, "mime": None}
    assert original_work(bundle.core.store) == before
    assert bundle.core.ledger.path.read_bytes() == ledger
    assert core_cli(bundle, "import")["items"][0]["state"] == "ERASED"


@pytest.mark.parametrize("problem", ["missing", "mismatch", "truncated"])
def test_latest_independent_ledger_required_without_creating_a_replacement(bundle, problem):
    make_export(bundle)
    path = bundle.core.ledger.path
    if problem == "missing":
        path.rename(path.with_suffix(".saved"))
    elif problem == "mismatch":
        path.write_text(encode({"ledger_id": "different"}) + "\n", "utf-8")
    else:
        with bundle.core.store.transaction(control_write=True):
            bundle.core.ledger.expire_events(bundle.core.store, [bundle.event["event_id"]],
                                             bundle.core.clock())
        path.write_text(path.read_text("utf-8").splitlines()[0] + "\n", "utf-8")
    before = logical_database(bundle.core.settings.database)
    core_cli(bundle, "import", ok=False)
    assert logical_database(bundle.core.settings.database) == before
    assert path.exists() == (problem != "missing")


def test_sql_failure_rolls_back_whole_import_and_restart_is_idempotent(bundle):
    make_export(bundle)
    bundle.core.store.execute("ALTER TABLE meme_asset_sources ADD CONSTRAINT fail_asset "
                              "CHECK (false) NOT VALID")
    before = logical_database(bundle.core.settings.database)
    core_cli(bundle, "import", ok=False)
    assert logical_database(bundle.core.settings.database) == before
    bundle.core.store.execute("ALTER TABLE meme_asset_sources DROP CONSTRAINT fail_asset")
    assert core_cli(bundle, "import")["items"][0]["state"] == "STAGED"


def test_process_death_during_real_cli_transaction_rolls_back_on_restart(bundle):
    make_export(bundle)
    script = """
import os, sys
from companion import meme_asset_migration as migration
from companion.store import Store
class CrashStore(Store):
    def execute(self, sql, params=()):
        result = super().execute(sql, params)
        if isinstance(sql, str) and sql.startswith('INSERT INTO meme_asset_sources'):
            os._exit(73)
        return result
migration.Store = CrashStore
sys.argv = ['migration', '--config', sys.argv[1], 'import', sys.argv[2]]
migration.main()
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPANION_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    before = original_work(bundle.core.store)
    result = subprocess.run([sys.executable, "-B", "-c", script, str(bundle.core_config),
                             str(bundle.manifest)], cwd=ROOT, env=env, capture_output=True,
                            timeout=45)
    assert result.returncode == 73, result.stderr
    assert not bundle.core.store.rows("SELECT * FROM meme_assets")
    assert not bundle.core.store.rows("SELECT * FROM meme_asset_sources")
    assert original_work(bundle.core.store) == before
    assert core_cli(bundle, "import")["items"][0]["state"] == "STAGED"


def test_export_requires_stopped_original_lock_custody_and_is_read_only(bundle):
    before = logical_database(Path(bundle.bridge.data_dir) / "spool.sqlite3")
    with Spool(bundle.bridge):
        make = cli("companion_onebot_bridge.meme_export", "--config", bundle.bridge_config,
                   "--output", bundle.manifest, ok=False)
        assert make == {"error": "MEME_EXPORT_REFUSED"}
    assert not bundle.manifest.exists()
    assert logical_database(Path(bundle.bridge.data_dir) / "spool.sqlite3") == before
    journal = Path(bundle.bridge.data_dir) / "owner-journal.jsonl"
    journal.write_bytes(journal.read_bytes() + b'{"invalid":"custody"}\n')
    cli("companion_onebot_bridge.meme_export", "--config", bundle.bridge_config,
        "--output", bundle.manifest, ok=False)
    assert not bundle.manifest.exists()


def test_export_does_not_stop_an_inactive_process_with_active_custody(bundle, tmp_path):
    active = replace(bundle.bridge, data_dir=str(tmp_path / "active-bridge"))
    with Spool(active) as spool:
        runtime = OwnerRuntime(spool)
        MemeCatalog(spool.db, MemeLibrary(tmp_path, enabled=True)).add(
            bundle.data, bundle.metadata, check=lambda: None)
        journal_before = runtime.path.read_bytes()
    config = config_file(tmp_path / "active.toml", {
        key: getattr(active, key) for key in (
            "owner_qq_id", "bot_qq_id", "bridge_token", "onebot_token", "protocol_epoch",
            "data_dir", "capture_signing_key", "owner_authority", "meme_enabled")})
    source = Path(active.data_dir) / "spool.sqlite3"
    before = logical_database(source)
    cli("companion_onebot_bridge.meme_export", "--config", config,
        "--output", bundle.manifest, ok=False)
    assert logical_database(source) == before
    assert (Path(active.data_dir) / "owner-journal.jsonl").read_bytes() == journal_before
    assert not bundle.manifest.exists()


def test_unchanged_original_fee_receipt_and_no_model_or_delivery_work(bundle):
    calls = []
    assert run(worker(bundle.core, calls))
    before = original_work(bundle.core.store)
    assert len(before["model_usage"]) == len(calls) == 1
    make_export(bundle)
    core_cli(bundle, "import")
    core_cli(bundle, "import")
    assert original_work(bundle.core.store) == before


def test_erasure_handler_requires_original_transaction(core):
    with pytest.raises(RuntimeError, match="ERASURE_REQUIRES_TRANSACTION"):
        meme_assets.erase(core.store, {"missing"})


def test_dry_run_replays_pending_erasure_only_in_memory(bundle):
    make_export(bundle)
    core_cli(bundle, "import")
    original = bundle.core.ledger.apply
    def fail(*args):
        raise RuntimeError("synthetic database interruption after durable erase")
    bundle.core.ledger.apply = fail
    with pytest.raises(RuntimeError):
        with bundle.core.store.transaction(control_write=True):
            bundle.core.ledger.expire_events(bundle.core.store, [bundle.event["event_id"]],
                                             bundle.core.clock())
    bundle.core.ledger.apply = original
    before = logical_database(bundle.core.settings.database)
    assert core_cli(bundle, "dry-run")["items"][0]["state"] == "ERASED"
    assert logical_database(bundle.core.settings.database) == before
    assert not core_cli(bundle, "reconcile")["matches"]
    assert core_cli(bundle, "import")["items"][0]["state"] == "ERASED"


def test_ledger_change_before_commit_rolls_back_new_content(bundle, monkeypatch):
    make_export(bundle)
    original = migration._apply
    def changed(store, *args):
        results = original(store, *args)
        # Another process may append a fsynced record before losing its DB txn.
        records = ErasureLedger(bundle.core.settings.erasure_ledger).read()
        path = bundle.core.ledger.path
        with path.open("a", encoding="utf-8") as stream:
            stream.write(encode({"seq": len(records), "erasure_id": "synthetic",
                                 "event_ids": [], "memory_ids": []}) + "\n")
        return results
    monkeypatch.setattr(migration, "_apply", changed)
    with pytest.raises((ValueError, CoreError), match="MEME_MIGRATION_PRIVACY_CHANGED"):
        migration.run(bundle.core.settings, bundle.manifest, operation="import")
    assert not bundle.core.store.rows("SELECT * FROM meme_assets")


@pytest.mark.parametrize("module,blocked", [
    ("companion.meme_asset_migration", "companion_onebot_bridge"),
    ("companion_onebot_bridge.meme_export", "companion"),
])
def test_offline_entrypoints_do_not_import_the_opposite_application(module, blocked):
    return check_entrypoint_import_isolation(module, blocked)
