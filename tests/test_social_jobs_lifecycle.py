"""Native social jobs preserve cancellation, fencing, display and schema evidence."""

from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from companion import private_life, social_plan_jobs
from companion.cloud_budget import CloudBudgetConfig
from companion.console_media_views import _plans
from companion.contracts import CoreError
from companion.legacy_import import migrate_sqlite
from companion.social_autonomy import restore_social_autonomy
from companion.social_autonomy_runtime import _expire, run_once
from tests.conftest import run
from tests.support.release_migration import TIMES, insert, one, open_historical, rows, seed_legacy
from tests.support.social_autonomy import background, planned


def deferred(core):
    module, ref, _ = background(core, plan_ttl_seconds=600)
    core.settings.cloud_budget = CloudBudgetConfig(daily_micro_cny=0)
    with pytest.raises(CoreError, match="CLOUD_DAILY_BUDGET_EXHAUSTED"):
        planned(module, ref)
    core.settings.cloud_budget = CloudBudgetConfig()
    plan = core.store.one("SELECT * FROM social_autonomy_plans")
    return module, ref, social_plan_jobs.get(core.store, plan["plan_id"])


@pytest.mark.parametrize("reason", ["stop", "restore", "erase"])
def test_cancelled_native_retry_never_rearms(core, reason):
    module, ref, job = deferred(core)
    with core.store.transaction(control_write=True):
        if reason == "stop":
            private_life.stop_work(core.store, core.clock())
        elif reason == "restore":
            restore_social_autonomy(core.store, core.clock())
        else:
            core.ledger.expire_events(core.store, [ref], core.clock())
    core.clock.advance(60)
    run(run_once(core.store, core.settings, module.model, core.clock))
    assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (job["job_id"],)) == {
        "state": "CANCELLED"}
    assert not module.model.calls and not core.store.rows("SELECT * FROM model_usage")
    assert not core.store.rows("SELECT * FROM outbox")


def test_reclaimed_job_fences_original_worker_before_native_reservation(core):
    module, _, original = deferred(core)
    core.clock.advance(60)
    stale = social_plan_jobs.claim(module, "stale")
    core.clock.advance(core.settings.lease_seconds + 1)
    assert _expire(core.store, core.clock())
    core.clock.advance(60)
    current = social_plan_jobs.claim(module, "current")
    assert current["job_id"] == original["job_id"]
    assert current["lease_token"] != stale["lease_token"]
    with pytest.raises(CoreError, match="SOCIAL_RETRY_JOB_FENCED"):
        module.check_retry_job(stale)
    with core.store.transaction():
        social_plan_jobs.fail(module, stale, "CLOUD_DAILY_BUDGET_EXHAUSTED")
    actual = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (current["job_id"],))
    assert {key: value for key, value in actual.items() if key != "record_sequence"} == current
    assert module.check_retry_job(current)
    assert not core.store.rows("SELECT * FROM model_usage")


def test_console_exposes_native_retry_due_time(core):
    _, _, job = deferred(core)
    item = _plans(core, core.clock())[0]
    assert item["status"] == "QUEUED"
    assert item["detail"]["retry_job_id"] == job["job_id"]
    assert item["detail"]["next_attempt_at"] == job["next_attempt_at"]


def test_mapping_migration_preserves_native_rows_and_blocks_populated_downgrade(tmp_path):
    # Exercise this migration on its actual predecessor. Later custody migrations
    # may intentionally prohibit downgrading a current production schema.
    database, _, _ = seed_legacy(tmp_path, "0004_diary")
    migrate_sqlite(str(database), "0035_token_measurement")
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    config.set_main_option("sqlalchemy.url", "sqlite:///" + Path(database).as_posix())
    with closing(open_historical(str(database))) as store:
        before = rows(store, "SELECT rowid,* FROM jobs")
        migrate_sqlite(str(database), "0036_social_plan_jobs")
        command.downgrade(config, "0035_token_measurement")
        migrate_sqlite(str(database), "0036_social_plan_jobs")
        assert rows(store, "SELECT rowid,* FROM jobs") == before
        now = TIMES["keep"]
        insert(store, "jobs", job_id="job_social_migration", event_id="evt_keep",
                     kind="activity", state="QUEUED", idempotency_key="social-migration",
                     created_at=now, updated_at=now)
        insert(store, "social_autonomy_plans", plan_id="plan_migration",
                     idempotency_key="plan-migration", request_sha256="a" * 64,
                     candidate_sha256="b" * 64, state="PLANNING", policy_version=1,
                     policy_sha256="c" * 64, authorization_sha256="d" * 64,
                     binding_sha256="e" * 64, timezone="UTC", policy_json="{}",
                     input_snapshot_json="{}", source_snapshot_sha256="f" * 64,
                     created_at=now, expires_at=now, updated_at=now)
        insert(store, "social_autonomy_jobs", plan_id="plan_migration",
                     job_id="job_social_migration")
        job = one(store, "SELECT * FROM jobs WHERE job_id='job_social_migration'")
        with pytest.raises(
            RuntimeError, match="SOCIAL_JOB_DOWNGRADE_REQUIRES_EXPLICIT_DATA_REVIEW",
        ):
            command.downgrade(config, "0035_token_measurement")
        assert one(store, "SELECT * FROM jobs WHERE job_id='job_social_migration'") == job
        assert one(store, "SELECT version_num FROM alembic_version") == {
            "version_num": "0036_social_plan_jobs"}
        assert rows(store, "PRAGMA foreign_key_check") == []
