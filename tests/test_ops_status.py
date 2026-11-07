import json
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from fastapi.testclient import TestClient

from companion import __main__, ops_status
from companion.api import create_app
from companion.api_store import ApiStore
from companion.core import Core
from companion.privacy import ErasureLedger
from companion.runtime import Runtime
from companion.store import Store, migrate
from tests.conftest import ingest, run
from tests.postgres_fixture import logical_state
from tests.support.historical_schema import assert_schema_metadata
from tests.support.ops_status import settings


def test_missing_database_diagnostic_never_creates_or_initializes_state(tmp_path, monkeypatch):
    cfg = settings(tmp_path / "absent")

    def forbidden(*args, **kwargs):
        raise AssertionError("diagnostics must not initialize services")

    for cls in (Core, Runtime, Store):
        monkeypatch.setattr(cls, "__init__", forbidden)
    result = ops_status.snapshot(cfg)
    assert "DATABASE_MISSING" in result["blockers"]
    assert not Path(cfg.database).parent.exists()
    assert not Path(cfg.erasure_ledger).exists()


def test_uninitialized_migrated_database_is_reported_without_bootstrap(
    tmp_path, migrated_database, monkeypatch,
):
    cfg = settings(tmp_path)
    shutil.copyfile(migrated_database, cfg.database)
    before = Path(cfg.database).read_bytes()
    monkeypatch.setattr(ErasureLedger, "initialize", lambda *_: (_ for _ in ()).throw(
        AssertionError("no ledger initialization")))
    result = ops_status.snapshot(cfg)
    assert "OWNER_IDENTITY_UNINITIALIZED_OR_MISMATCH" in result["blockers"]
    assert "ERASURE_IDENTITY_UNINITIALIZED" in result["blockers"]
    assert Path(cfg.database).read_bytes() == before
    assert not Path(cfg.erasure_ledger).exists()
    with sqlite3.connect(cfg.database) as db:
        assert_schema_metadata(db)


def test_snapshot_sees_committed_wal_and_preserves_unknown_and_private_content(core, monkeypatch):
    ingest(core, text="SYNTHETIC_PRIVATE_MESSAGE_NEVER_IN_STATUS")
    assert run(core.run_once("synthetic-worker"))
    core.store.execute("UPDATE outbox SET state='DELIVERY_UNCERTAIN'")
    core.store.execute("UPDATE model_usage SET state='UNKNOWN'")
    core.store.execute("INSERT INTO runtime_meta VALUES ('model_budget_blocked','private detail')")
    before = logical_state(core.store)
    ledger = Path(core.settings.erasure_ledger).read_bytes()
    calls = len(core.model.calls)
    original = ops_status.read_view
    observed = []

    @contextmanager
    def readonly(settings):
        with original(settings) as view:
            observed.append(view.one("SHOW transaction_read_only")["transaction_read_only"])
            yield view

    monkeypatch.setattr(ops_status, "read_view", readonly)
    # Resource counts and fractional wall time can coincidentally contain the
    # synthetic owner ID. Keep this privacy check's ambient inputs deterministic.
    monkeypatch.setattr(ops_status, "datetime", SimpleNamespace(
        now=lambda _: core.clock.now, fromisoformat=datetime.fromisoformat))
    monkeypatch.setattr(ops_status.shutil, "disk_usage",
                        lambda _: SimpleNamespace(free=2 ** 30))
    result = ops_status.snapshot(core.settings)
    assert result["observed_at"] == core.clock.now.isoformat()
    assert result["resources"]["disk_free_bytes"] == 2 ** 30
    assert result["database"]["bytes"] > 0
    assert result["deliveries"]["states"] == {"DELIVERY_UNCERTAIN": 1}
    assert result["budget"]["unknown_calls"] == 1
    assert result["budget"]["calibration"]["calibrated_calls"] == 0
    assert result["budget"]["calibration"]["currency"] is None
    assert result["budget"]["calibration"]["billing_basis"] == "configured_rates_not_invoice"
    assert {"MODEL_BUDGET_BLOCKED", "DELIVERY_UNCERTAIN", "MODEL_USAGE_UNSETTLED"} <= set(
        result["blockers"]
    )
    assert result["worker_progress"]["state"] == "UNOBSERVED"
    assert result["database"]["integrity"] == "NOT_CHECKED"
    assert observed == ["on"]
    assert logical_state(core.store) == before
    assert Path(core.settings.erasure_ledger).read_bytes() == ledger
    assert len(core.model.calls) == calls
    encoded = json.dumps(result)
    for private in ("SYNTHETIC_PRIVATE", "private detail", core.settings.owner_qq_id,
                    core.settings.bridge_token.get_secret_value(),
                    core.settings.database.get_secret_value()):
        assert private not in encoded


def test_explicit_integrity_checks_erasure_replay_without_applying_it(core):
    path = Path(core.settings.erasure_ledger)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"seq": 1, "erasure_id": "erase_synthetic", "memory_id": "none",
                                 "memory_ids": [], "event_ids": [], "at": core.clock()}) + "\n")
    before = path.read_bytes()
    light = ops_status.snapshot(core.settings)
    assert light["resources"]["erasure_ledger"]["checked"] == "HEADER_ONLY"
    assert "ERASURE_LEDGER_RECONCILIATION_REQUIRED" not in light["blockers"]
    result = ops_status.snapshot(core.settings, integrity=True)
    assert result["database"]["integrity"] == "OK"
    assert "ERASURE_LEDGER_RECONCILIATION_REQUIRED" in result["blockers"]
    assert path.read_bytes() == before
    assert core.store.one("SELECT COUNT(*) n FROM privacy_erasure_ledger")["n"] == 0


def test_missing_or_old_schema_fails_closed_without_migrating(tmp_path):
    cfg = settings(tmp_path)
    with sqlite3.connect(cfg.database) as db:
        db.execute("CREATE TABLE alembic_version(version_num TEXT)")
        db.execute("INSERT INTO alembic_version VALUES ('0001_m0')")
    before = Path(cfg.database).read_bytes()
    assert "MIGRATION_REQUIRED" in ops_status.snapshot(cfg)["blockers"]
    assert Path(cfg.database).read_bytes() == before
    assert not Path(cfg.erasure_ledger).exists()


def test_cli_status_avoids_core_and_semantic_initialization(core, monkeypatch, capsys):
    monkeypatch.setattr(__main__.Settings, "load", lambda: core.settings.model_copy(
        update={"memory_enabled": True}
    ))
    monkeypatch.setattr(Core, "__init__", lambda *_: (_ for _ in ()).throw(
        AssertionError("no Core construction")))
    monkeypatch.setattr(sys, "argv", ["companion", "status"])
    __main__.main()
    result = json.loads(capsys.readouterr().out)
    assert result["read_only"] is True
    assert result["resources"]["embedding_cache"]["model_loaded"] is False


def test_ops_summary_and_liveness_do_not_claim_worker_health(core, monkeypatch):
    app = create_app(core.settings)
    app.state.core = core
    monkeypatch.setattr(core, "status", lambda: (_ for _ in ()).throw(
        AssertionError("summary must not run full live window/journal guard")))
    client = TestClient(app)
    assert client.get("/internal/v1/ops/status?summary_only=true").status_code == 401
    response = client.get("/internal/v1/ops/status?summary_only=true", headers={
        "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value(),
    })
    assert response.headers["Cache-Control"] == "no-store"
    result = response.json()
    assert result["operations"]["worker_progress"]["state"] == "UNOBSERVED"
    assert result["observation"]["worker_health_proven"] is False
    assert len(result["observation"]["api_instance"]) == 32
    assert client.get("/internal/v1/health/live").json()["worker_health_proven"] is False


def test_missing_budget_journal_identity_is_never_reinitialized(core):
    core.store.execute(
        "INSERT INTO runtime_meta VALUES ('model_budget_journal_id','budget_unknown')"
    )
    before = logical_state(core.store)
    result = ops_status.snapshot(core.settings, integrity=True)
    assert "BUDGET_JOURNAL_RECONCILIATION_REQUIRED" in result["blockers"]
    assert logical_state(core.store) == before


def test_schema_capabilities_reuse_existing_readiness_contract(core):
    core.store.execute("UPDATE alembic_version SET version_num='future_compatible'")
    result = ops_status.snapshot(core.settings)
    assert result["database"]["schema"] == "future_compatible"
    assert result["database"]["capabilities_present"] is True
    assert "MIGRATION_REQUIRED" not in result["blockers"]


def test_ready_and_diagnostics_require_upgrade_from_single_topic_schema(tmp_path):
    cfg = settings(tmp_path).model_copy(update={"memory_enabled": False})
    old = "0063_mood_appraisal_targets"
    migrate(cfg.database, old)
    headers = {"Authorization": "Bearer " + cfg.bridge_token.get_secret_value()}
    with TestClient(create_app(cfg)) as client:
        response = client.get("/internal/v1/health/ready", headers=headers)
        assert response.status_code == 503 and response.json() == {"error": "MIGRATION_REQUIRED"}
        result = ops_status.snapshot(cfg)
        assert result["database"]["schema"] == old and "MIGRATION_REQUIRED" in result["blockers"]
    migrate(cfg.database)
    with TestClient(create_app(cfg)) as client:
        response = client.get("/internal/v1/health/ready", headers=headers)
        assert response.status_code == 200 and response.json()["schema"] == ops_status.schema_head()
        assert "MIGRATION_REQUIRED" not in ops_status.snapshot(cfg)["blockers"]


@pytest.mark.parametrize("change", [
    "UPDATE alembic_version SET version_num='0067_relationship_standing'",
    "INSERT INTO alembic_version VALUES ('other_head')",
    "DELETE FROM alembic_version",
    "DROP INDEX idx_memory_image_review_target",
    "DROP INDEX idx_memories_relationship_standing",
])
def test_readiness_checks_revision_and_query_requirements_independently(core, change):
    core.store.execute(change)
    app = create_app(core.settings)
    app.state.core = core
    response = TestClient(app).get("/internal/v1/health/ready", headers={
        "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value(),
    })
    assert response.status_code == 503 and response.json() == {"error": "MIGRATION_REQUIRED"}
    assert "MIGRATION_REQUIRED" in ops_status.snapshot(core.settings)["blockers"]


@pytest.mark.parametrize("failure", ["empty_versions", "permission", "unavailable"])
def test_unavailable_migration_head_blocks_snapshot_and_ready_without_writes(
    core, tmp_path, monkeypatch, failure,
):
    scripts = tmp_path / "public-migrations"
    (scripts / "versions").mkdir(parents=True)
    # Alembic returns None for an empty/unlistable versions tree rather than proving a head.
    assert ScriptDirectory(str(scripts)).get_current_head() is None

    def directory(_):
        if failure == "permission":
            raise PermissionError("synthetic private path must not be exposed")
        if failure == "unavailable":
            raise CommandError("synthetic broken migration tree")
        return ScriptDirectory(str(scripts))

    before = logical_state(core.store)
    ledger = Path(core.settings.erasure_ledger).read_bytes()
    ops_status.schema_head.cache_clear()
    monkeypatch.setattr(ops_status, "ScriptDirectory", directory)
    try:
        result = ops_status.snapshot(core.settings)
        assert result["database"]["expected_schema"] is None
        assert result["blockers"] == ["SCHEMA_HEAD_UNAVAILABLE"]
        app = create_app(core.settings)
        app.state.core = core
        app.state.api_store = ApiStore(core.database, core.settings.state_directory)
        response = TestClient(app).get("/internal/v1/health/ready", headers={
            "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value(),
        })
        assert response.status_code == 503
        assert response.json() == {"error": "SCHEMA_HEAD_UNAVAILABLE"}
        assert logical_state(core.store) == before
        assert Path(core.settings.erasure_ledger).read_bytes() == ledger
    finally:
        ops_status.schema_head.cache_clear()
