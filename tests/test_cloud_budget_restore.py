"""Old encrypted backups cannot create a fresh cloud spending allowance."""

from contextlib import closing

import pytest
from cryptography.fernet import Fernet

from companion import budget, cloud_budget
from companion.backup import backup, restore
from companion.core import Core
from companion.executive import Executive
from companion.executor_gateway import receipt
from companion.model import ModelResponse
from companion.postgres import Database
from companion.store import Store
from tests.postgres_fixture import isolated_database, logical_state
from tests.support.cloud_budget import MESSAGES, change, options
from tests.support.executive import FakeExecutor, configured, proposal
from tests.support.media import chunk, process, service, start

BLOCK = "CLOUD_BUDGET_RESTORE_RECONCILIATION_REQUIRED"


@pytest.fixture
def restore_target(tmp_path):
    with isolated_database() as database:
        yield database, tmp_path / "restored-state"


@pytest.mark.parametrize("bound_before_backup", [False, True])
def test_old_backup_marks_missing_tool_cost_history_while_new_work_continues(
    core, tmp_path, bound_before_backup, restore_target,
):
    options(core, daily_micro_cny=1000)
    executive = configured(core)
    executive.config.endpoints["windows"].max_cost_micro_usd = 100
    if bound_before_backup:
        cloud_budget.policy(core.store, core.settings, core.clock())
    dest, state = restore_target
    archive, key = tmp_path / "old.enc", Fernet.generate_key()
    backup(core.settings.database, archive, key)
    task = executive.submit(proposal())
    leased = executive.claim()
    _, request = executive.begin(leased)
    with core.store.transaction():
        cloud_budget.mark_dispatched(core.store, "tool", task["task_id"], core.clock())
    executive.finish(leased, receipt(request, "SUCCEEDED", "synthetic tool result", billed=100))
    executive.submit(proposal(2))
    executive.begin(executive.claim())
    restore(archive, dest, key, core.settings.erasure_ledger, state_directory=state)
    settings = core.settings.model_copy(update={"database": dest, "state_directory": str(state)})
    with Core(settings, clock=core.clock) as restored:
        with restored.store.read_snapshot():
            before = logical_state(restored.store)
            assert cloud_budget.capacity_blocks(restored.store, settings, core.clock()) == []
            assert logical_state(restored.store) == before
        # The backup has no trace of the newer charge. The incomplete-history
        # warning persists while independently admitted work records new costs.
        assert not restored.store.rows("SELECT * FROM tool_tasks")
        for turn in range(2):
            view = cloud_budget.summary(restored.store, settings, core.clock())
            assert view["reconciliation_required"] and not view["admission_blocked"]
            assert view["reconciliation_reasons"] == [BLOCK]
            assert view["restored_history_may_be_incomplete"]
            assert view["used_micro_cny"] >= 0 and view["remaining_micro_cny"] == 0
            fake = FakeExecutor()
            runner = Executive(restored.store, settings, core.clock, fake)
            runner.submit(proposal(turn + 10))
            leased = runner.claim()
            runner.begin(leased)
            assert fake.calls == []
            if not turn:
                change(restored, daily_micro_cny=20_000_000, usd_to_cny_micro=1_000_000)
                core.clock.advance(86400)
        assert len(restored.store.rows("SELECT * FROM cloud_cost_snapshots")) == 2
    # Restarting preserves the warning and does not add a currency fence.
    with Database(dest) as database, closing(Store(database, state)) as store:
        with store.read_snapshot():
            assert cloud_budget.capacity_blocks(store, settings, core.clock()) == []
            assert store.one("SELECT 1 FROM runtime_meta "
                             "WHERE key='cloud_budget_restore_required'")


def test_restore_cost_warning_allows_new_model_and_asr_with_original_accounting(
    core, tmp_path, restore_target,
):
    dest, state = restore_target
    archive, key = tmp_path / "old.enc", Fernet.generate_key()
    backup(core.settings.database, archive, key)
    restore(archive, dest, key, core.settings.erasure_ledger, state_directory=state)
    settings = core.settings.model_copy(update={"database": dest, "state_directory": str(state)})
    with Core(settings, clock=core.clock) as restored:
        request = budget.reserve(restored.store, settings, None, "chat", MESSAGES, core.clock())
        media = service(restored)
        session = start(media)
        process(media, session, chunk())
        assert len(media.provider.calls) == 1
        assert restored.store.one("SELECT request_id FROM model_usage")["request_id"] == request
        assert len(restored.store.rows("SELECT * FROM cloud_cost_snapshots")) == 2


def test_restore_retains_known_spend_pending_liability_and_incomplete_history_warning(
    core, tmp_path, restore_target,
):
    request = budget.reserve(core.store, core.settings, None, "chat", MESSAGES, core.clock())
    budget.settle(core.store, core.settings, request, ModelResponse("done", 100, 20))
    budget.reserve(core.store, core.settings, None, "chat", MESSAGES, core.clock())
    previous = cloud_budget.summary(core.store, core.settings, core.clock())
    dest, state = restore_target
    archive, key = tmp_path / "spent.enc", Fernet.generate_key()
    backup(core.settings.database, archive, key)
    restore(archive, dest, key, core.settings.erasure_ledger, state_directory=state)
    settings = core.settings.model_copy(update={"database": dest, "state_directory": str(state)})
    with Database(dest) as database, closing(Store(database, state)) as store:
        view = cloud_budget.summary(store, settings, core.clock())
        assert view["used_micro_cny"] == previous["used_micro_cny"] > 0
        assert view["settled_micro_cny"] == previous["settled_micro_cny"] > 0
        assert view["unknown_micro_cny"] == previous["reserved_micro_cny"] > 0
        assert view["remaining_micro_cny"] == 0 and view["reconciliation_required"]
        assert cloud_budget.capacity_blocks(store, settings, core.clock()) == []
        assert not view["admission_blocked"]
