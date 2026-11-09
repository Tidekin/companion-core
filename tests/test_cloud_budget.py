"""Shared CNY reservations retain native accounting, FX snapshots and task identity."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier

import pytest

from companion import budget, cloud_budget, paid_budget
from companion.cloud_budget import CloudBudgetConfig
from companion.contracts import CoreError
from companion.executive import erase_executive
from companion.executor_gateway import receipt
from companion.model import ModelResponse
from companion.store import Store, after
from tests.conftest import run
from tests.owner_model_fixtures import authorized_job
from tests.postgres_fixture import logical_state
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.support.cloud_budget import MESSAGES, change, charge, currency_blocks, native, options
from tests.support.cumulative_budget import policy as cumulative_policy
from tests.support.executive import FakeExecutor, configured, proposal
from tests.support.media import chunk, process, service, start
from tests.support.native_consumer_entry import environment
from tests.support.paid_model import approved_live
from tests.support.video_accounting import core as historical_video_core
from tests.support.video_accounting import correct, failed_video

__all__ = ["failed_video", "historical_video_core"]


def test_defaults_and_versioned_patch_are_idempotent_without_resetting_usage(core):
    original = cloud_budget.policy(core.store, core.settings, core.clock())
    assert original["config"] == CloudBudgetConfig().model_dump()
    assert original["config"]["usd_to_cny_micro"] == 8_000_000
    assert original["fx_valid_until"] == after(core.clock(), 30 * 86400)
    charge(core, "paid-one", 100)
    request = {"expected_version": 1, "changes": {"usd_to_cny_micro": 9_000_000}}
    first = cloud_budget.configure(core.store, core.settings, request,
                                   idempotency_key="set-budget-1", now=core.clock())
    repeated = cloud_budget.configure(core.store, core.settings, request,
                                      idempotency_key="set-budget-1", now=core.clock())
    assert first == repeated and first["version"] == 2
    assert first["config"]["usd_to_cny_micro"] == 9_000_000
    assert cloud_budget.summary(core.store, core.settings, core.clock())["used_micro_cny"] == 800
    with pytest.raises(CoreError, match="VERSION_CONFLICT"):
        cloud_budget.configure(core.store, core.settings, request,
                               idempotency_key="set-budget-2", now=core.clock())
    with pytest.raises(CoreError, match="IDEMPOTENCY_CONFLICT"):
        cloud_budget.configure(core.store, core.settings,
                               {"expected_version": 1, "changes": {"usd_to_cny_micro": 7_000_000}},
                               idempotency_key="set-budget-1", now=core.clock())
    assert len(core.store.rows("SELECT * FROM cloud_budget_policies")) == 2


@pytest.mark.parametrize("patch", [{"usd_to_cny_micro": 0},
                                    {"fx_source": ""}, {"fx_source": "  "},
                                    {"fx_validity_seconds": 0},
                                    {"unlimited": True}])


def test_invalid_cloud_configuration_never_changes_policy(core, patch):
    original = cloud_budget.policy(core.store, core.settings, core.clock())
    with pytest.raises(CoreError, match="CLOUD_BUDGET_CONFIG_INVALID"):
        change(core, **patch)
    assert cloud_budget.policy(core.store, core.settings, core.clock()) == original


def test_two_independent_connections_record_charges_beyond_advisory_daily_amount(core):
    options(core)
    barrier = Barrier(2)

    def attempt(number):
        store = Store(core.database, core.settings.state_directory)
        try:
            barrier.wait(timeout=5)
            try:
                charge(core, f"parallel-{number}", 70_000, store=store)
                return "RESERVED"
            except CoreError as error:
                return error.code
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(attempt, [1, 2]))
    assert sorted(results) == ["RESERVED", "RESERVED"]
    assert len(core.store.rows("SELECT * FROM model_usage")) == 2
    assert len(core.store.rows("SELECT * FROM cloud_cost_snapshots")) == 2
    view = cloud_budget.summary(core.store, core.settings, core.clock())
    assert view["used_micro_cny"] == 1_120_000
    assert "over_limit" not in view


@pytest.mark.parametrize("kind", ["video", "asr"])
def test_task_identity_and_original_fx_cover_all_rounds(core, kind):
    options(core)
    first = 31_250
    charge(core, "round-one", first, task_kind=kind, task_id="trusted-item-one")
    charge(core, "round-two", first + 1, task_kind=kind, task_id="trusted-item-one")
    assert {row["request_id"] for row in core.store.rows("SELECT request_id FROM model_usage")} == {
        "round-one", "round-two",
    }
    assert len(core.store.rows("SELECT * FROM cloud_cost_snapshots")) == 2
    change(core, usd_to_cny_micro=9_000_000)
    charge(core, "round-three", first + 1, task_kind=kind, task_id="trusted-item-one")


    rows = core.store.rows("SELECT * FROM cloud_cost_snapshots ORDER BY reference_id")
    assert len(rows) == 3 and {row["task_id"] for row in rows} == {"trusted-item-one"}
    assert sorted(row["amount_micro_cny"] for row in rows) == [250_000, 250_008, 281_259]


def test_fractional_fx_is_rounded_up_for_each_atomic_reservation(core):
    options(core,  usd_to_cny_micro=1_000_001)
    first = charge(core, "fraction-one", 1)
    assert first["reserved_micro_cny"] == 2
    charge(core, "fraction-two", 1)


def test_legacy_models_and_dispatched_tools_are_included_before_fx_changes(core):
    options(core)
    native(core.store, "old-model", 60_000, core.clock(), state="SETTLED")
    executive = configured(core)
    task = executive.submit(proposal())
    core.store.execute((
        "UPDATE tool_tasks SET state='RUNNING',reserved_micro_usd=50000,bille"
        'd_micro_usd=50000,dispatched_at=%s WHERE task_id=%s'
    ),
                       (core.clock(), task["task_id"]))
    first = cloud_budget.summary(core.store, core.settings, core.clock())
    assert first["used_micro_cny"] == 880_000 and first["charge_count"] == 2
    charge(core, "over-original-fx", 20_000)
    change(core, usd_to_cny_micro=4_000_000, fx_source="synthetic-rate-v2")
    charge(core, "new-fx", 20_000)
    assert cloud_budget.summary(core.store, core.settings, core.clock())["used_micro_cny"] == (
        1_120_000
    )
    rows = core.store.rows("SELECT amount_micro_cny FROM cloud_cost_snapshots WHERE legacy=1")
    assert sorted(row["amount_micro_cny"] for row in rows) == [400_000, 480_000]
    change(core, key="cloud-change-2", usd_to_cny_micro=16_000_000)
    assert cloud_budget.summary(core.store, core.settings, core.clock())["used_micro_cny"] == (
        1_120_000
    )


@pytest.mark.parametrize("kind", ["model", "tool"])
def test_legacy_native_reservation_is_not_lost_when_billed_projection_lags(core, tmp_path, kind):
    if kind == "model":
        approved_live(core, tmp_path)
        reference = budget.reserve(core.store, core.settings, authorized_job(core),
                                   "chat", MESSAGES, core.clock())
        reservation = core.store.one("SELECT reserved_micro_usd FROM model_admissions")
        held = reservation["reserved_micro_usd"]
        core.store.execute("DELETE FROM cloud_cost_snapshots")
        core.store.execute("UPDATE model_usage SET billed_micro_usd=0")
    else:
        executive = configured(core)
        reference = executive.submit(proposal())["task_id"]
        held = 100
        core.store.execute((
            "UPDATE tool_tasks SET state='RUNNING',reserved_micro_usd=%s,bill"
            'ed_micro_usd=0,dispatched_at=%s WHERE task_id=%s'
        ),
                           (held, core.clock(), reference))
    view = cloud_budget.summary(core.store, core.settings, core.clock())
    assert held > 0 and view["reserved_micro_cny"] == held * 8
    row = core.store.one('SELECT * FROM cloud_cost_snapshots WHERE reference_id=%s', (reference,))
    assert row["accounted_micro_usd"] == row["reserved_micro_usd"] == held


def test_unknown_liability_is_retained_while_new_explicit_charge_continues(core):
    options(core)
    charge(core, "uncertain", 80, task_kind="video", task_id="same-video")
    core.store.execute("UPDATE model_usage SET state='UNKNOWN' WHERE request_id='uncertain'")
    assert cloud_budget.summary(core.store, core.settings, core.clock())["unknown_micro_cny"] == 640
    change(core, usd_to_cny_micro=1_000_000)
    charge(core, "second-payment", 1, task_kind="video", task_id="same-video")
    core.store.execute("UPDATE model_usage SET state='SETTLED',billed_micro_usd=1 "
                       "WHERE request_id='uncertain'")
    view = cloud_budget.summary(core.store, core.settings, core.clock())
    assert view["unknown_micro_cny"] == 640 and view["used_micro_cny"] == 641
    assert len(core.store.rows("SELECT * FROM model_usage")) == 2


def test_native_settlement_uses_reserved_usd_rates_and_fx_snapshot(core):
    request_id = budget.reserve(core.store, core.settings, None, "chat", MESSAGES, core.clock())
    original_cost = budget.cost(core.settings, 100, 20)
    change(core, usd_to_cny_micro=1_000_000, fx_source="synthetic-new-fx")
    core.settings.input_rate *= 10
    core.settings.output_rate *= 10
    budget.settle(core.store, core.settings, request_id, ModelResponse("done", 100, 20))
    assert core.store.one("SELECT billed_micro_usd FROM model_usage")["billed_micro_usd"] == (
        original_cost
    )
    view = cloud_budget.summary(core.store, core.settings, core.clock())
    assert view["settled_micro_cny"] == original_cost * 8


def test_cloud_currency_cap_is_advisory_for_original_paid_reservation(core, tmp_path):
    approved_live(core, tmp_path)
    options(core)
    job_id = authorized_job(core)
    path = Path(core.settings.paid_model.journal_path)
    before = path.read_bytes()
    request_id = budget.reserve(core.store, core.settings, job_id, "chat", MESSAGES, core.clock())
    assert core.store.one("SELECT request_id FROM model_usage")["request_id"] == request_id
    assert core.store.one("SELECT request_id FROM model_admissions")["request_id"] == request_id
    assert path.read_bytes() != before
    assert cloud_budget.summary(core.store, core.settings, core.clock())["reserved_micro_cny"] > 0


def test_advisory_cloud_cap_commits_original_cumulative_reservation_atomically(core, tmp_path):
    approved_live(core, tmp_path)
    options(core)
    cumulative_policy(core)
    job_id = authorized_job(core)
    request_id = budget.reserve(core.store, core.settings, job_id, "chat", MESSAGES, core.clock())
    with core.store.transaction():
        custody = paid_budget.journal(core.store, core.settings.paid_model)
    assert request_id in custody["cumulative"]["models"]
    assert core.store.one("SELECT reference_id FROM cloud_cost_snapshots") == {
        "reference_id": request_id,
    }


def test_midnight_settlement_stays_in_original_day_and_timezone_is_not_reset(core):
    core.clock.now = datetime.fromisoformat("2026-10-19T15:59:59+00:00")
    options(core)
    charge(core, "before-midnight", 80)
    with core.store.transaction():
        cloud_budget.mark_dispatched(core.store, "model", "before-midnight", core.clock())
    core.clock.advance(2)
    before = cloud_budget.summary(core.store, core.settings, core.clock())
    assert before["carried_micro_cny"] == before["used_micro_cny"] == 640
    core.store.execute("UPDATE model_usage SET billed_micro_usd=100,state='SETTLED' "
                       "WHERE request_id='before-midnight'")
    core.settings.timezone = "America/Los_Angeles"
    charge(core, "after-midnight", 80)
    view = cloud_budget.summary(core.store, core.settings, core.clock())
    assert view["local_day"] == "2026-10-20" and view["used_micro_cny"] == 640
    assert view["timezone"] == "Asia/Shanghai"
    assert core.store.one("SELECT local_day,amount_micro_cny FROM cloud_cost_snapshots "
                          "WHERE reference_id='before-midnight'") == {
        "local_day": "2026-10-19", "amount_micro_cny": 800,
    }


def test_reservation_from_yesterday_counts_today_until_late_dispatch_and_settlement(core):
    core.clock.now = datetime.fromisoformat("2026-10-19T15:59:59+00:00")
    options(core)
    charge(core, "pending", 80)
    core.clock.advance(2)
    charge(core, "midnight-bypass", 80)
    with core.store.transaction():
        cloud_budget.mark_dispatched(core.store, "model", "pending", core.clock())
        core.store.execute("UPDATE model_usage SET billed_micro_usd=100,state='SETTLED' "
                           "WHERE request_id='pending'")
        cloud_budget.sync_reference(core.store, "model", "pending")
    current = cloud_budget.summary(core.store, core.settings, core.clock())
    assert current["settled_micro_cny"] == 800 and current["carried_micro_cny"] == 0
    assert {row["local_day"] for row in core.store.rows(
        "SELECT local_day FROM cloud_cost_snapshots")} == {"2026-10-20"}


def test_fx_expiry_is_visible_while_new_reservations_and_old_settlement_continue(core):
    options(core, fx_validity_seconds=60)
    request_id = budget.reserve(core.store, core.settings, None, "chat", MESSAGES, core.clock())
    core.clock.advance(61)
    charge(core, "expired-fx", 1)
    budget.settle(core.store, core.settings, request_id, ModelResponse("done", 100, 20))
    view = cloud_budget.summary(core.store, core.settings, core.clock())
    assert view["fx_expired"] and view["settled_micro_cny"] > 0


def test_recorded_fx_window_is_unchanged_until_explicit_fx_review(core):
    options(core, fx_validity_seconds=60)
    original = cloud_budget.policy(core.store, core.settings, core.clock())
    core.clock.advance(30)
    updated = cloud_budget.policy(core.store, core.settings, core.clock())
    assert updated["fx_valid_from"] == original["fx_valid_from"]
    assert updated["fx_valid_until"] == original["fx_valid_until"]
    core.clock.advance(31)
    charge(core, "expired-rate", 1)
    change(core, key="renew-reviewed-fx", usd_to_cny_micro=8_000_000)
    charge(core, "reviewed-rate", 1)


def test_unpriced_legacy_unknown_remains_visible_without_blocking_new_charge(core):
    native(core.store, "legacy-unpriced", 0, core.clock(), state="UNKNOWN")
    core.store.execute("UPDATE model_usage SET pricing_known=0")
    summary = cloud_budget.summary(core.store, core.settings, core.clock())
    assert summary["unpriced_unknown_count"] == 1
    assert "admission_blocked" not in summary
    charge(core, "new-spend", 1)


def test_cloud_asr_and_tool_paths_use_the_same_daily_pool(core):
    options(core)
    media = service(core)
    session = start(media)
    process(media, session, chunk())
    assert cloud_budget.summary(core.store, core.settings, core.clock())["used_micro_cny"] == 800
    process(media, session, chunk(key="second-chunk"))
    assert len(media.provider.calls) == 2
    executive = configured(core)
    executive.submit(proposal())
    task = executive.claim()
    executive.begin(task)
    assert core.store.one("SELECT state FROM tool_tasks")["state"] == "RUNNING"


def test_observed_cost_overrun_is_retained_and_reported_instead_of_rolled_back(core):
    options(core)
    charge(core, "estimate", 80, task_kind="video", task_id="video-one")
    core.store.execute("UPDATE model_usage SET billed_micro_usd=200,state='SETTLED'")
    summary = cloud_budget.summary(core.store, core.settings, core.clock())
    assert summary["used_micro_cny"] == summary["settled_micro_cny"] == 1600
    assert "task_over_limit_count" not in summary
    assert core.store.one("SELECT billed_micro_usd FROM model_usage")["billed_micro_usd"] == 200
    charge(core, "next-estimate", 1)


class TestHistoricalCorrection:
    @pytest.fixture
    def core(self, historical_video_core):
        # Relocate the fixture's journal file without changing its signed accounting bytes.
        historical_video_core.store.execute('UPDATE model_admissions SET journal_path=%s',
            (historical_video_core.settings.paid_model.journal_path,))
        return historical_video_core

    def test_verified_native_correction_releases_unknown_classification_without_repricing(
        self, core, failed_video,
    ):
        request, _, _ = failed_video
        cloud_budget.summary(core.store, core.settings, core.clock())
        before = core.store.one('SELECT * FROM cloud_cost_snapshots WHERE reference_id=%s',
                                (request["request_id"],))
        assert before["state"] == "UNKNOWN"
        receipt = correct(core, request)
        cloud_budget.summary(core.store, core.settings, core.clock())
        after_row = core.store.one('SELECT * FROM cloud_cost_snapshots WHERE reference_id=%s',
                                   (request["request_id"],))
        assert after_row["state"] == "SETTLED"
        assert after_row["amount_micro_cny"] == before["amount_micro_cny"]
        assert after_row["reconciliation_sha256"] == receipt["receipt_sha256"]


def test_http_and_independent_model_worker_continue_beyond_advisory_cloud_amount(core, tmp_path):
    options(core)
    provider = Provider(answer("合成预算回复一"), answer("合成预算回复二"))

    async def scenario():
        async with environment(core, tmp_path, purposes=("chat",)) as (entry, _), worker_for(
            core, provider,
        ) as worker:
            first = await entry.admit(1, "第一条预算测试")
            assert await worker.run_once("independent-cloud-worker")
            first_usage = core.store.one("SELECT * FROM model_usage")
            assert len(provider.requests) == 1 and first_usage["state"] == "SETTLED"
            assert first_usage["job_id"] == entry.accepted[first["event_id"]]["job_id"]
            current = cloud_budget.summary(core.store, core.settings, core.clock())
            assert all(type(current[field]) is int for field in (
                "used_micro_cny", "reserved_micro_cny", "settled_micro_cny",
                "unknown_micro_cny", "carried_micro_cny",
            ))
            change(core, usd_to_cny_micro=9_000_000)
            second = await entry.admit(2, "第二条超额仍运行并记录原费用")
            assert await worker.run_once("independent-cloud-worker")
            job_id = entry.accepted[second["event_id"]]["job_id"]
            assert core.store.one('SELECT state FROM jobs WHERE job_id=%s',
                                  (job_id,))["state"] == "SUCCEEDED"
            assert len(provider.requests) == 2
            assert core.store.one("SELECT * FROM model_usage WHERE request_id=%s",
                                  (first_usage["request_id"],)) == first_usage
            assert len(core.store.rows("SELECT * FROM cloud_cost_snapshots")) == 2
    run(scenario())


def test_capacity_read_is_pure_before_binding_and_reports_known_legacy_exhaustion(core):
    options(core)
    native(core.store, "legacy-paid", 100, core.clock(), state="SETTLED")
    with core.store.read_snapshot():
        before = logical_state(core.store)
        assert currency_blocks(core.store, core.settings, core.clock()) == []
        assert logical_state(core.store) == before
    assert not core.store.rows("SELECT * FROM cloud_budget_policies")
    assert not core.store.rows("SELECT * FROM cloud_cost_snapshots")


def test_capacity_read_uses_frozen_fx_and_observed_usage_without_writing(core):
    options(core,  fx_validity_seconds=60)
    charge(core, "one-hold", 100)
    with core.store.read_snapshot():
        before = logical_state(core.store)
        assert currency_blocks(core.store, core.settings, core.clock()) == []
        assert logical_state(core.store) == before
    core.store.execute("UPDATE model_usage SET billed_micro_usd=50,state='SETTLED'")
    with core.store.read_snapshot():
        before = logical_state(core.store)
        assert currency_blocks(core.store, core.settings, core.clock()) == []
        assert logical_state(core.store) == before
        # This diagnostic neither settles the snapshot nor renews an expiring rate.
        assert core.store.one("SELECT state FROM cloud_cost_snapshots")["state"] == "RESERVED"
        core.clock.advance(61)
        assert currency_blocks(core.store, core.settings, core.clock()) == []
        assert logical_state(core.store) == before


def test_erasing_a_dispatched_tool_keeps_unknown_cost_across_midnight(core):
    options(core)
    fake = FakeExecutor()
    executive = configured(core, fake)
    task = executive.submit(proposal())
    fake.callback = lambda: erase_executive(core.store, ["evt_test_1"])
    assert run(executive.run_one())
    native_row = core.store.one('SELECT * FROM tool_tasks WHERE task_id=%s', (task["task_id"],))
    assert native_row["state"] == "CANCELLED" and native_row["billed_micro_usd"] == 0
    assert len(fake.calls) == 1
    core.clock.advance(86400)
    with core.store.read_snapshot():
        before = logical_state(core.store)
        assert currency_blocks(core.store, core.settings, core.clock()) == []
        assert logical_state(core.store) == before
    view = cloud_budget.summary(core.store, core.settings, core.clock())
    assert view["unknown_micro_cny"] == view["carried_micro_cny"] == 0
    assert view["unpriced_unknown_count"] == 1
    charge(core, "post-erase-midnight", 1)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED"])
def test_erasing_a_completed_tool_keeps_validated_fee_and_settlement(core, legacy, status):
    executive = configured(core)
    task = executive.submit(proposal())
    leased = executive.claim()
    _, request = executive.begin(leased)
    with core.store.transaction():
        cloud_budget.mark_dispatched(core.store, "tool", task["task_id"], core.clock())
    text = "synthetic tool result" if status == "SUCCEEDED" else ""
    executive.finish(leased, receipt(request, status, text, billed=125))
    if legacy:
        core.store.execute("DELETE FROM cloud_cost_snapshots")
    erase_executive(core.store, ["evt_test_1"])
    view = cloud_budget.summary(core.store, core.settings, core.clock())
    assert view["settled_micro_cny"] == 1000 and view["unknown_micro_cny"] == 0
    assert core.store.one("SELECT billed_micro_usd FROM tool_tasks")["billed_micro_usd"] == 125
    core.clock.advance(86400)
    view = cloud_budget.summary(core.store, core.settings, core.clock())
    assert view["used_micro_cny"] == view["carried_micro_cny"] == 0


def test_erasing_a_reserved_but_unattempted_tool_releases_only_the_unused_charge(core):
    executive = configured(core)
    task = executive.submit(proposal())
    executive.begin(executive.claim())
    # A diagnostic sync between begin and dispatch must not invent an I/O attempt.
    cloud_budget.summary(core.store, core.settings, core.clock())
    assert core.store.one("SELECT dispatched_at FROM cloud_cost_snapshots")["dispatched_at"] is None
    erase_executive(core.store, ["evt_test_1"])
    native_row = core.store.one("SELECT * FROM tool_tasks")
    assert native_row["state"] == "CANCELLED" and native_row["reserved_micro_usd"] == 0
    assert native_row["billed_micro_usd"] == 0
    view = cloud_budget.summary(core.store, core.settings, core.clock())
    assert view["used_micro_cny"] == view["unknown_micro_cny"] == 0
    with pytest.raises(CoreError, match="CLOUD_BUDGET_ALREADY_DISPATCHED"):
        with core.store.transaction():
            cloud_budget.mark_dispatched(core.store, "tool", task["task_id"], core.clock())


def test_legacy_tool_cancellation_without_a_completion_receipt_stays_unknown(core):
    options(core)
    executive = configured(core)
    executive.submit(proposal())
    executive.begin(executive.claim())
    # Reproduce an original pre-upgrade tool estimate, without changing current admission.
    core.store.execute("UPDATE tool_tasks SET reserved_micro_usd=1000,billed_micro_usd=1000")
    core.store.execute("DELETE FROM cloud_cost_snapshots")
    erase_executive(core.store, ["evt_test_1"])
    core.clock.advance(86400)
    view = cloud_budget.summary(core.store, core.settings, core.clock())
    assert view["unknown_micro_cny"] == view["carried_micro_cny"] == 8000
