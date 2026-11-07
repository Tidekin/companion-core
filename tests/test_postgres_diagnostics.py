"""Native operational reads preserve business state and independent journals."""

import json
from pathlib import Path

import httpx
import psycopg
import pytest

from companion import beta_observation, budget, ops_status
from companion.contracts import CoreError
from companion.postgres import Database
from tests.conftest import run
from tests.postgres_fixture import isolated_database, logical_state
from tests.support.core_http import real_core_http


def test_native_status_and_trace_read_without_repair(core):
    before = core.store.rows("SELECT key,value FROM runtime_meta ORDER BY key")
    ledger = Path(core.settings.erasure_ledger).read_bytes()
    result = ops_status.snapshot(core.settings, integrity=True)
    assert result["database"]["storage"] == "postgresql"
    assert result["database"]["schema"] == ops_status.schema_head()
    assert result["database"]["integrity"] == "CATALOG_VALID", result
    assert result["database"]["integrity_basis"] == "catalog_constraints_and_indexes"
    assert result["resources"]["erasure_ledger"]["checked"] == "FULL_LEDGER"
    trace = beta_observation.inspect_context(core.settings, "synthetic-no-trace")
    assert trace["read_only"] and not trace["blockers"], trace
    assert core.store.rows("SELECT key,value FROM runtime_meta ORDER BY key") == before
    assert Path(core.settings.erasure_ledger).read_bytes() == ledger
    assert budget.usage_diagnostics(core.store)["calibrated_calls"] == 0
    material = json.dumps(result)
    assert core.settings.bridge_token.get_secret_value() not in material


def test_native_status_on_empty_database_never_migrates_or_creates_state(core, tmp_path):
    with isolated_database() as target:
        state = tmp_path / "uninitialized-state"
        settings = core.settings.model_copy(update={"database": target,
                                                     "state_directory": str(state)})
        result = ops_status.snapshot(settings)
        assert "MIGRATION_REQUIRED" in result["blockers"]
        assert not state.exists()
        with Database(target) as database, database.session() as session:
            assert session.one("SELECT count(*) AS n FROM information_schema.tables "
                               "WHERE table_schema='public'") == {"n": 0}


def test_native_diagnostic_scope_cannot_write_or_fallback(core):
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction), ops_status.read_view(
        core.settings,
    ) as view:
        view.execute("INSERT INTO runtime_meta(key,value) VALUES ('forbidden-read-write','x')")
    assert core.store.one("SELECT 1 FROM runtime_meta WHERE key='forbidden-read-write'") is None


@pytest.mark.parametrize("shape,blocker", [
    ("head", None), ("future", None), ("legacy", "MIGRATION_REQUIRED"),
    ("missing_version_table", "MIGRATION_REQUIRED"),
    ("no_version", "MIGRATION_REQUIRED"), ("two_versions", "MIGRATION_REQUIRED"),
    ("missing_column", "MIGRATION_REQUIRED"),
    ("missing_slot_constraint", "MIGRATION_REQUIRED"),
    ("missing_head", "SCHEMA_HEAD_UNAVAILABLE"),
])
def test_native_readiness_http_and_diagnostics_share_version_and_shape_guards(
    core, monkeypatch, shape, blocker,
):
    with core.store.transaction():
        if shape in {"future", "legacy"}:
            core.store.execute("UPDATE alembic_version SET version_num=%s", (
                "future_compatible" if shape == "future" else "0063_mood_appraisal_targets",
            ))
        elif shape == "missing_version_table":
            core.store.execute("DROP TABLE alembic_version")
        elif shape == "no_version":
            core.store.execute("DELETE FROM alembic_version")
        elif shape == "two_versions":
            core.store.execute("INSERT INTO alembic_version VALUES ('another_head')")
        elif shape == "missing_column":
            core.store.execute("ALTER TABLE captured_images DROP COLUMN proof_json")
        elif shape == "missing_slot_constraint":
            from psycopg import sql

            for row in core.store.rows(
                "SELECT conname FROM pg_constraint WHERE conrelid='content_items'::regclass "
                "AND contype='c' AND pg_get_constraintdef(oid) LIKE '%%slot%%'"
            ):
                core.store.execute(sql.SQL("ALTER TABLE content_items DROP CONSTRAINT {}").format(
                    sql.Identifier(row["conname"])))
    if shape == "missing_head":
        def unavailable():
            raise CoreError("SCHEMA_HEAD_UNAVAILABLE", 503)

        monkeypatch.setattr(ops_status, "schema_head", unavailable)
    before = logical_state(core.store)
    journal = Path(core.settings.erasure_ledger).read_bytes()

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=15,
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as client:
            response = await client.get("/internal/v1/health/ready")
            assert response.status_code == (503 if blocker else 200), response.text
            if blocker:
                assert response.json() == {"error": blocker}
            else:
                assert response.json()["schema"] == (
                    "future_compatible" if shape == "future" else ops_status.schema_head())
            assert (await client.get("/internal/v1/health/live")).status_code == 200

    run(scenario())
    result = ops_status.snapshot(core.settings)
    trace = beta_observation.inspect_context(core.settings, "synthetic-no-trace")
    if blocker:
        assert result["blockers"] == [blocker], result
        assert trace["blockers"] == [blocker], trace
    else:
        assert not result["blockers"], result
        assert not trace["blockers"], trace
    assert logical_state(core.store) == before
    assert Path(core.settings.erasure_ledger).read_bytes() == journal
