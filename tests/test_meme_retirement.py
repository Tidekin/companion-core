"""Independent offline diagnostics retain original content and all liabilities."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import companion_delivery_protocol as wire
from companion import delivery_reconciliation as reconciliation
from companion_onebot_bridge import meme_retirement
from companion_onebot_bridge.instance_lock import acquire
from companion_onebot_bridge.meme_runtime import MemeRuntime
from companion_onebot_bridge.meme_semantic import SemanticLibrary
from tests.support.meme_asset_migration import config_file
from tests.support.native_meme import NativeCase
from tests.support.next_memes_catalog import metadata, png
from tests.support.next_memes_semantic import EMBEDDING, IDENTITY
from tests.support.owner_delivery_reconciliation import pending

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 64
JOB = "job_" + "b" * 32
SOURCE = "c" * 64
REQUEST = "d" * 64


@pytest.fixture
def stopped(core, tmp_path):
    fixture = NativeCase(core, tmp_path, rules=(), semantic=True)
    runtime = MemeRuntime(fixture.owner)
    SemanticLibrary(runtime.catalog, IDENTITY, EMBEDDING)
    fixture.owner.stop()
    config = fixture.spool.config
    fixture.spool.close()
    path = config_file(tmp_path / "bridge.toml", {
        key: getattr(config, key) for key in (
            "owner_qq_id", "bot_qq_id", "bridge_token", "onebot_token", "protocol_epoch",
            "data_dir", "capture_signing_key", "owner_authority", "meme_enabled",
            "meme_semantics_enabled")})
    return SimpleNamespace(config=config, path=path, directory=Path(config.data_dir),
                           database=Path(config.data_dir) / "spool.sqlite3")


def snapshot(directory):
    return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_size,
                     p.stat().st_mtime_ns, p.stat().st_ctime_ns)
            for p in directory.iterdir() if p.is_file()}


def cli(stopped, *extra, clear=False):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPANION_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.run([sys.executable, "-B", "-m",
        "companion_onebot_bridge.meme_retirement", "--config", str(stopped.path),
        *map(str, extra)], cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=30)
    assert process.returncode == (0 if clear else 2), process.stdout + process.stderr
    assert process.stderr == ""
    value = json.loads(process.stdout)
    assert value["cutover_authorized"] is False and value["state"] == "BLOCKED"
    return value


def sql(stopped, query, parameters=()):
    with closing(sqlite3.connect(stopped.database)) as db, db:
        db.execute(query, parameters)


def test_real_cli_existing_custody_and_empty_known_schema_is_zero_write(stopped):
    before = snapshot(stopped.directory)
    report = cli(stopped, clear=True)
    assert report["local_state"] == "CLEAR" and report["reasons"] == {}
    assert len(report["tables"]) == len(meme_retirement.SCHEMA)
    assert report["external_gates"] == list(meme_retirement.EXTERNAL)
    assert report["wait_until"] is None
    assert snapshot(stopped.directory) == before


def test_explicit_copy_is_hashed_without_content_or_directory_walk(stopped, tmp_path):
    copy = tmp_path / "explicit-private.json"
    copy.write_text('PRIVATE_CAPTION_CANARY https://private.invalid/secret', "utf-8")
    other = tmp_path / "operator-owned"
    other.mkdir()
    (other / "untouched").write_text("OPERATOR_PRIVATE_CANARY", "utf-8")
    before = snapshot(stopped.directory), snapshot(other), snapshot(tmp_path)
    report = cli(stopped, "--managed-copy", copy, clear=True)
    assert report["managed_copies"][0]["sha256"] == hashlib.sha256(copy.read_bytes()).hexdigest()
    assert "PRIVATE" not in json.dumps(report) and "private.invalid" not in json.dumps(report)
    assert (snapshot(stopped.directory), snapshot(other), snapshot(tmp_path)) == before
    assert cli(stopped, "--managed-copy", other)["local_state"] == "BLOCKED"


@pytest.mark.parametrize("change", ["missing", "extra", "type", "unknown_table"])
def test_old_or_unknown_schema_is_never_assumed_closed(stopped, change):
    query = {
        "missing": "DROP TABLE meme_caption_bindings",
        "extra": "ALTER TABLE meme_assets ADD COLUMN unknown TEXT",
        "type": "ALTER TABLE meme_assets RENAME COLUMN data TO unknown",
        "unknown_table": "CREATE TABLE meme_future_projection(secret TEXT)",
    }[change]
    sql(stopped, query)
    before = snapshot(stopped.directory)
    assert cli(stopped)["reasons"]
    assert snapshot(stopped.directory) == before


@pytest.mark.parametrize("state", ["PREPARED", "SENDING", "UNKNOWN", "invalid"])
def test_unprojected_pin_is_a_separate_blocker_without_release(stopped, state):
    sql(stopped, "INSERT INTO meme_pins VALUES (?,?,?,NULL)", (SHA, SOURCE, state))
    before = snapshot(stopped.directory)
    report = cli(stopped)
    reason = "PIN_STATE_UNVERIFIED" if state == "invalid" else "PIN_UNPROJECTED_OR_UNSETTLED"
    assert report["reasons"][reason] == 1
    assert snapshot(stopped.directory) == before


@pytest.mark.parametrize("sent_at", [None, -1, 99999999999, 1.0])
def test_sent_missing_future_or_unbound_time_never_uses_current_cooldown(stopped, sent_at):
    sql(stopped, "INSERT INTO meme_pins VALUES (?,?,?,?)", (SHA, SOURCE, "SENT", sent_at))
    report = cli(stopped)
    assert report["reasons"]["ORIGINAL_COOLDOWN_UNVERIFIED"] == 1
    assert report["reasons"]["SENT_TIME_UNVERIFIED"] == 1
    assert report["wait_until"] is None


def caption_binding(stopped, *, usage_state="SETTLED", mutate=None):
    usage = {"usage_request_id": "original-caption", "state": usage_state,
             "billed_micro_usd": 1250 if usage_state == "SETTLED" else None}
    outcome = {"schema_version": 1, "job_id": JOB, "state": "FAILED", "asset_sha256": SHA,
        "source_evidence_sha256": SOURCE, "request_sha256": REQUEST,
        "caption_model_version": IDENTITY.model_version, "prompt_version": IDENTITY.prompt_version,
        "result": None, "usage": usage, "cache_hit": False}
    if mutate:
        mutate(outcome)
    sql(stopped, "INSERT INTO meme_caption_bindings VALUES (?,?,?,?,?,?,?,?,?)",
        (REQUEST, SHA, SOURCE, "cache", '{"private":"CONTEXT_CANARY"}', JOB, "FAILED",
         json.dumps(outcome), json.dumps(usage)))


@pytest.mark.parametrize("state", ["RESERVED", "UNKNOWN"])
def test_original_caption_accounting_stays_unsettled(stopped, state):
    caption_binding(stopped, usage_state=state)
    before = snapshot(stopped.directory)
    report = cli(stopped)
    assert report["reasons"]["CAPTION_ACCOUNTING_UNSETTLED"] == 1
    assert "CONTEXT_CANARY" not in json.dumps(report)
    assert snapshot(stopped.directory) == before


@pytest.mark.parametrize("field,value", [("job_id", "job_" + "f" * 32),
    ("request_sha256", "f" * 64), ("asset_sha256", "f" * 64),
    ("source_evidence_sha256", "f" * 64), ("usage", None)])
def test_corrupt_original_caption_receipts_block(stopped, field, value):
    caption_binding(stopped, mutate=lambda outcome: outcome.update({field: value}))
    assert cli(stopped)["reasons"]["CAPTION_RECEIPT_UNVERIFIED"] == 1


def test_settled_caption_retains_original_cost_and_content(stopped):
    caption_binding(stopped)
    before = snapshot(stopped.directory)
    assert cli(stopped, clear=True)["tables"]["meme_caption_bindings"]["rows"] == 1
    assert snapshot(stopped.directory) == before


@pytest.mark.parametrize("state,outcome", [("PENDING", "SETTLED"), ("UNKNOWN", "UNKNOWN"),
                                          ("SETTLED", "UNKNOWN")])
def test_original_discovery_unknown_and_unaccepted_result_remain_blocked(stopped, state, outcome):
    body = {"lease_token": "lease_" + "1" * 32, "attempt_key": "original-discovery",
        "outcome": outcome, "usage": {"queries": 1, "requests": 2, "download_bytes": 300,
                                      "storage_bytes": 100, "model_cost": 0}}
    sql(stopped, "INSERT INTO meme_runtime_results VALUES (?,?,?,?)",
        ("original-discovery", JOB, json.dumps(body), state))
    before = snapshot(stopped.directory)
    assert cli(stopped)["reasons"]["DISCOVERY_ACCOUNTING_UNSETTLED"] == 1
    assert snapshot(stopped.directory) == before


def test_independent_cli_competes_for_original_existing_lock(stopped):
    before = snapshot(stopped.directory)
    with acquire(stopped.directory / "instance.lock", existing_only=True):
        assert cli(stopped)["local_state"] == "BLOCKED"
    assert snapshot(stopped.directory) == before


@pytest.mark.parametrize("kind", ["missing", "empty"])
def test_lock_is_never_created_or_initialized(stopped, kind):
    lock = stopped.directory / "instance.lock"
    lock.unlink() if kind == "missing" else lock.write_bytes(b"")
    before = snapshot(stopped.directory)
    assert cli(stopped)["local_state"] == "BLOCKED"
    assert snapshot(stopped.directory) == before


def test_changed_original_journal_refuses_without_echoing_private_parser_error(stopped):
    journal = stopped.directory / "owner-journal.jsonl"
    journal.write_text('PRIVATE_BODY_SECRET_KEY_NOT_JSON\n', "utf-8")
    before = snapshot(stopped.directory)
    report = cli(stopped)
    assert "PRIVATE" not in json.dumps(report)
    assert snapshot(stopped.directory) == before


def test_committed_wal_is_refused_in_real_process_without_ignoring_marker(stopped):
    script = """
import sqlite3, sys
db=sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=WAL')
db.execute('CREATE TABLE committed_wal_marker(value TEXT)')
db.execute("INSERT INTO committed_wal_marker VALUES ('PRIVATE_WAL_MARKER')")
db.commit()
print('READY', flush=True)
sys.stdin.readline()
db.close()
"""
    with subprocess.Popen([sys.executable, "-B", "-c", script, str(stopped.database)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True) as writer:
        assert writer.stdout.readline().strip() == "READY"
        try:
            before = snapshot(stopped.directory)
            assert before["spool.sqlite3-wal"][1] > 0
            report = cli(stopped)
            assert report["reasons"] == {"WAL_RECOVERY_REQUIRED": 1}
            assert "tables" not in report and "PRIVATE_WAL_MARKER" not in json.dumps(report)
            assert snapshot(stopped.directory) == before
        finally:
            writer.communicate("DONE\n", timeout=10)


@pytest.mark.parametrize("change", ["database", "new_sidecar", "journal", "lock"])
def test_noncooperating_external_change_discards_snapshot(stopped, monkeypatch, change):
    original = meme_retirement._inventory

    def changed(db, issues):
        result = original(db, issues)
        paths = {"database": stopped.database, "new_sidecar": Path(str(stopped.database) + "-wal"),
                 "journal": stopped.directory / "owner-journal.jsonl",
                 "lock": stopped.directory / "instance.lock"}
        # Model an external writer which ignores the held OS lock. Do not alter
        # any valid SQLite page; appending still invalidates the whole-file proof.
        with paths[change].open("ab") as stream:
            stream.write(b"EXTERNAL_CHANGE")
        return result

    monkeypatch.setattr(meme_retirement, "_inventory", changed)
    report = meme_retirement.preflight(stopped.config)
    assert report["local_state"] == "BLOCKED"
    assert report["reasons"].get("FILES_CHANGED") or report["reasons"].get(
        "CUSTODY_PATH_SCHEMA_OR_RECEIPT_UNVERIFIED")
    assert "tables" not in report and "pins" not in report


def test_readonly_cli_does_not_import_core_or_call_write_constructors(stopped):
    script = """
import importlib.abc, runpy, sys
class NoCore(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'companion' or fullname.startswith('companion.'):
            raise AssertionError('Core import refused')
sys.meta_path.insert(0, NoCore())
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
def forbidden(*args, **kwargs):
    raise AssertionError('writer constructor refused')
OwnerRuntime.__init__ = Spool.__init__ = forbidden
sys.argv=['meme_retirement', '--config', sys.argv[1]]
runpy.run_module('companion_onebot_bridge.meme_retirement', run_name='__main__')
"""
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-B", "-c", script, str(stopped.path)],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["local_state"] == "CLEAR"


def test_known_column_names_with_changed_primary_key_are_unknown_schema(stopped):
    sql(stopped, "DROP TABLE meme_assets")
    sql(stopped, "CREATE TABLE meme_assets(sha TEXT,data BLOB,metadata TEXT,active INTEGER)")
    assert cli(stopped)["reasons"]["MISSING_OR_UNKNOWN_SCHEMA"] == 1


def test_original_resolution_never_projects_unknown_pin_or_removes_blocker(
    stopped, core_factory, tmp_path
):
    directory = tmp_path / "original-resolution"
    directory.mkdir()
    with core_factory(directory) as core:
        proof, _ = pending(core, directory)
        assert reconciliation.prepare(core.store, core.settings, proof, core.clock())["state"] == (
            "READY")
        resolved = reconciliation.resolve(core.store, core.settings, proof["message_id"], "SENT",
            core.clock(), operation_id="meme-retirement-pin-resolution", operation_sha256="e" * 64)
        receipt = wire.validate_receipt(resolved["receipt"])
        record = {"seq": 1, "previous": "0" * 64, "kind": "OWNER_RECONCILED",
                  "at": core.clock(), "data": receipt}
    sql(stopped, "INSERT INTO meme_pins VALUES (?,?,?,NULL)", (SHA, SOURCE, "UNKNOWN"))
    before = snapshot(stopped.directory)
    issues = Counter()
    # The owner verifier is responsible for the original resolution's integrity.
    # Even after that verification this diagnosis must not infer a pin projection.
    with closing(sqlite3.connect(stopped.database)) as db:
        result = meme_retirement._pins(db, [record], datetime.now(UTC), issues)
    assert result["states"]["original_owner_resolutions"] == 1
    assert issues["PIN_UNPROJECTED_OR_UNSETTLED"] == 1
    assert issues["ORIGINAL_COOLDOWN_UNVERIFIED"] == 1
    assert result["verified_times"][0]["resolution_sha256"] == wire.digest(receipt)
    assert cli(stopped)["reasons"]["PIN_UNPROJECTED_OR_UNSETTLED"] == 1
    assert snapshot(stopped.directory) == before


def test_unattributed_asset_content_is_counted_but_never_printed_or_removed(stopped):
    from dataclasses import asdict

    data = png()
    sql(stopped, "INSERT INTO meme_assets VALUES (?,?,?,1)",
        (hashlib.sha256(data).hexdigest(), data, json.dumps(asdict(metadata()))))
    before = snapshot(stopped.directory)
    report = cli(stopped)
    assert report["reasons"]["ASSET_SOURCE_UNVERIFIED"] == 1
    assert report["tables"]["meme_assets"]["content_cells"] == {"data": 1, "metadata": 1}
    assert metadata().source_page not in json.dumps(report)
    assert snapshot(stopped.directory) == before


@pytest.mark.parametrize("kind", ["directory", "missing", "empty"])
def test_existing_only_lock_refuses_without_creation_or_permissions_change(tmp_path, kind):
    path = tmp_path / "instance.lock"
    if kind == "directory":
        path.mkdir()
    elif kind == "empty":
        path.write_bytes(b"")
    with pytest.raises((OSError, ValueError)):
        acquire(path, existing_only=True)
    assert path.exists() == (kind != "missing")
    if kind == "empty":
        assert path.read_bytes() == b""


def test_missing_original_offer_uniqueness_blocks_even_with_same_columns(stopped):
    sql(stopped, "DROP TABLE meme_runtime_bindings")
    sql(stopped, "CREATE TABLE meme_runtime_bindings(delivery_key TEXT PRIMARY KEY,"
        "offer_key TEXT NOT NULL,event_id TEXT NOT NULL,candidate_id TEXT NOT NULL,"
        "category TEXT NOT NULL,window_sha256 TEXT NOT NULL)")
    assert cli(stopped)["reasons"]["MISSING_OR_UNKNOWN_SCHEMA"] == 1


@pytest.mark.parametrize("at,recorded,valid", [
    ("2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00", True),
    ("2099-01-01T00:00:00+00:00", "2099-01-01T00:01:00+00:00", False),
    ("2026-01-01T00:02:00+00:00", "2026-01-01T00:01:00+00:00", False),
])
def test_original_resolution_times_are_diagnostic_and_never_release_pin(
    stopped, at, recorded, valid
):
    sql(stopped, "INSERT INTO meme_pins VALUES (?,?,?,NULL)", (SHA, SOURCE, "UNKNOWN"))
    issues = Counter()
    records = [{"kind": "OWNER_RECONCILED", "at": recorded,
                "data": {"outcome": "SENT", "at": at}}]
    with closing(sqlite3.connect(stopped.database)) as db:
        report = meme_retirement._pins(db, records, datetime.now(UTC), issues)
    assert issues["PIN_UNPROJECTED_OR_UNSETTLED"] == 1
    assert issues["ORIGINAL_COOLDOWN_UNVERIFIED"] == 1
    assert bool(report["verified_times"]) == valid
    assert bool(issues["RESOLUTION_TIME_UNVERIFIED"]) != valid


@pytest.mark.parametrize("empty_wal", [False, True])
def test_orphan_shared_memory_is_an_unknown_recovery_baseline(stopped, empty_wal):
    Path(str(stopped.database) + "-shm").write_bytes(b"UNPROVEN_RECOVERY")
    if empty_wal:
        Path(str(stopped.database) + "-wal").write_bytes(b"")
    before = snapshot(stopped.directory)
    assert cli(stopped)["reasons"] == {"WAL_RECOVERY_REQUIRED": 1}
    assert snapshot(stopped.directory) == before
