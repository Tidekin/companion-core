"""Original HTTP accounting survives stopped retirement, restart and stale copies."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion import owner_authority
from companion.owner_authority_protocol import AuthorityStop
from companion_onebot_bridge import journal, meme_export, meme_isolation, owner_handoff
from companion_onebot_bridge.meme_runtime import MemeRuntime
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.meme_asset_migration import cli, config_file
from tests.support.meme_semantic_runtime import SemanticCore
from tests.support.meme_semantic_runtime import make_runtime as make_runtime
from tests.support.native_meme import case


@pytest.fixture
def isolated_source(make_runtime, tmp_path):
    runtime, event, _ = make_runtime()
    upstream = SemanticCore(runtime, event)
    native = case(runtime)

    async def prepare():
        async with upstream.client() as client:
            result = await runtime.run_once(client)
            sha = result["pending_caption"][0]
            for expected in ("CAPTION_PENDING", "CAPTION_READY", "SEMANTIC_READY"):
                assert (await runtime.run_once(client))["status"] == expected
            # An accepted historical UNKNOWN download and old pin with missing timing
            # are fault fixtures. Retirement must preserve them, never guess a result.
            row = runtime.db.execute("SELECT * FROM meme_runtime_results").fetchone()
            body = json.loads(row["body_json"])
            body.update(attempt_key="old-uncertain-attempt", outcome="UNKNOWN")
            runtime.db.execute("INSERT INTO meme_runtime_results VALUES (?,?,?,?)",
                (body["attempt_key"], "job_" + "a" * 32, json.dumps(body), "UNKNOWN"))
            runtime.db.execute("INSERT INTO meme_pins VALUES (?,?,?,NULL)",
                               ("b" * 64, sha, "SENT"))
            runtime.db.commit()
            runtime.window.stop()
            owner_authority.stop(native.core.store, native.core.settings, native.core.clock,
                AuthorityStop(operation_id="isolation-stop", authority=runtime.authority))
            config = runtime.config
            runtime.spool.close()
            with Spool(config, reconciliation_only=True) as spool:
                native.core.clock.now = datetime.now(UTC)
                stopped = OwnerRuntime(spool, clock=lambda: native.core.clock.now)
                await owner_handoff.prepare(stopped, client)
            return config, sha

    config, sha = run(prepare())
    manifest = tmp_path / "exported-catalog.json"
    meme_export.export(config, manifest)
    path = config_file(tmp_path / "isolated-bridge.toml", {key: getattr(config, key) for key in (
        "owner_qq_id", "bot_qq_id", "bridge_token", "onebot_token", "protocol_epoch",
        "data_dir", "capture_signing_key", "owner_authority", "meme_enabled",
        "meme_semantics_enabled")})
    return SimpleNamespace(config=config, path=path, manifest=manifest, sha=sha,
        database=Path(config.data_dir) / "spool.sqlite3", native=native)


def rows(source, table):
    with closing(sqlite3.connect(source.database)) as db:
        return db.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()


def test_real_retirement_cli_keeps_original_unknown_usage_and_pins(isolated_source):
    c = isolated_source
    originals = {table: rows(c, table) for table in
                 ("meme_runtime_results", "meme_pins", "window_attempts", "results")}
    costs = c.native.core.store.rows("SELECT * FROM model_usage")
    report = cli("companion_onebot_bridge.meme_isolation", "--config", c.path,
                 "--manifest", c.manifest)
    assert report["state"] == "RETIRED" and report["excluded_sha256"] == [c.sha]
    assert all(rows(c, table) == before for table, before in originals.items())
    assert c.native.core.store.rows("SELECT * FROM model_usage") == costs
    assert rows(c, "meme_assets")[0][1:] == (None, "{}", 0)
    assert rows(c, "meme_caption_bindings")[0][7] is None
    with Spool(replace(c.config, meme_catalog_owner="core"), reconciliation_only=True) as spool:
        runtime = OwnerRuntime(spool)
        assert runtime.memes.__class__.__name__ == "CoreMemeTransport"
        with pytest.raises(ValueError, match="MEME_CATALOG_RETIRED"):
            MemeRuntime(runtime)
    again = meme_isolation.retire(c.config, c.manifest)
    assert again["duplicate"] and again["retained_sha256"] == report["retained_sha256"]


@pytest.mark.parametrize("change", ["bytes", "usage", "semantic_state", "legacy_mode"])
def test_retired_catalog_refuses_restored_content_or_responsibility_drift(isolated_source, change):
    c = isolated_source
    meme_isolation.retire(c.config, c.manifest)
    config = replace(c.config, meme_catalog_owner="core")
    if change == "legacy_mode":
        with Spool(c.config, reconciliation_only=True) as spool:
            runtime = OwnerRuntime(spool)
            with pytest.raises(ValueError, match="MEME_CATALOG_RETIRED"):
                _ = runtime.memes
        return
    with closing(sqlite3.connect(c.database)) as db, db:
        if change == "bytes":
            db.execute("UPDATE meme_assets SET data=X'ff',active=1")
        elif change == "semantic_state":
            db.execute("UPDATE meme_semantic_assets SET state='QUARANTINED'")
        else:
            db.execute("UPDATE meme_runtime_results SET state='SETTLED' WHERE state='UNKNOWN'")
    with Spool(config, reconciliation_only=True) as spool:
        with pytest.raises(ValueError, match="MEME_RETIRED_"):
            OwnerRuntime(spool)


def test_retired_responsibilities_allow_new_core_transport_pins(isolated_source):
    c = isolated_source
    meme_isolation.retire(c.config, c.manifest)
    with closing(sqlite3.connect(c.database)) as db, db:
        db.execute("INSERT INTO meme_pins VALUES (?,?,'SENT',1234)", ("c" * 64, "d" * 64))
        db.execute("INSERT INTO meme_runtime_bindings VALUES (?,?,?,?,?,?)",
                   ("new-delivery", "new-offer", "new-event", "d" * 64, "happy", "e" * 64))
    with Spool(replace(c.config, meme_catalog_owner="core"), reconciliation_only=True) as spool:
        assert OwnerRuntime(spool).memes.__class__.__name__ == "CoreMemeTransport"


def test_changed_export_does_not_retire_original_catalog(isolated_source):
    c = isolated_source
    value = json.loads(c.manifest.read_text("utf-8"))
    value["custody"]["journal_head"] = "f" * 64
    value["sha256"] = journal.digest({k: v for k, v in value.items() if k != "sha256"})
    c.manifest.write_text(json.dumps(value), "utf-8")
    before = rows(c, "meme_assets")
    with pytest.raises(ValueError, match="MEME_RETIREMENT_CUSTODY_CHANGED"):
        meme_isolation.retire(c.config, c.manifest)
    assert rows(c, "meme_assets") == before


def test_journal_commit_before_sql_failure_cannot_reactivate_old_catalog(
    isolated_source, monkeypatch,
):
    c = isolated_source

    def fail(db):
        raise sqlite3.OperationalError("synthetic commit failure")

    monkeypatch.setattr(meme_isolation, "_clear", fail)
    with pytest.raises(sqlite3.OperationalError):
        meme_isolation.retire(c.config, c.manifest)
    with Spool(replace(c.config, meme_catalog_owner="core"), reconciliation_only=True) as spool:
        with pytest.raises(ValueError, match="OWNER_JOURNAL_RECONCILIATION_REQUIRED"):
            OwnerRuntime(spool)
