"""Old encrypted backups cannot create a fresh cloud spending allowance."""

from contextlib import closing

import pytest
from cryptography.fernet import Fernet

from companion import budget, cloud_budget
from companion.backup import backup, restore
from companion.contracts import CoreError
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
def test_old_backup_cannot_reset_tool_cost_or_reopen_pool_with_config_edit(
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
    with pytest.raises(CoreError, match="CLOUD_DAILY_BUDGET_EXHAUSTED"):
        executive.begin(executive.claim())
    restore(archive, dest, key, core.settings.erasure_ledger, state_directory=state)
    settings = core.settings.model_copy(update={"database": dest, "state_directory": str(state)})
    with Core(settings, clock=core.clock) as restored:
        with restored.store.read_snapshot():
            before = logical_state(restored.store)
            assert cloud_budget.capacity_blocks(restored.store, settings, core.clock()) == [BLOCK]
            assert logical_state(restored.store) == before
        # The backup has no trace of the newer charge. Its emptiness is not proof
        # of a complete ledger and must not present another spendable allowance.
        assert not restored.store.rows("SELECT * FROM tool_tasks")
        for turn in range(2):
            view = cloud_budget.summary(restored.store, settings, core.clock())
            assert view["reconciliation_required"] and view["admission_blocked"]
            assert view["reconciliation_reasons"] == [BLOCK]
            assert view["restored_history_may_be_incomplete"]
            assert view["used_micro_cny"] == view["remaining_micro_cny"] == 0
            fake = FakeExecutor()
            runner = Executive(restored.store, settings, core.clock, fake)
            runner.submit(proposal(turn + 10))
            leased = runner.claim()
            with pytest.raises(CoreError, match=BLOCK):
                runner.begin(leased)
            assert fake.calls == []
            if not turn:
                change(restored, daily_micro_cny=20_000_000, usd_to_cny_micro=1_000_000)
                core.clock.advance(86400)
        assert not restored.store.rows("SELECT * FROM cloud_cost_snapshots")
    # Restarting with a new connection cannot clear the restore barrier.
    with Database(dest) as database, closing(Store(database, state)) as store:
        with store.read_snapshot():
            assert BLOCK in cloud_budget.capacity_blocks(store, settings, core.clock())


def test_restore_blocks_model_and_asr_admission_before_provider_io(core, tmp_path, restore_target):
    dest, state = restore_target
    archive, key = tmp_path / "old.enc", Fernet.generate_key()
    backup(core.settings.database, archive, key)
    restore(archive, dest, key, core.settings.erasure_ledger, state_directory=state)
    settings = core.settings.model_copy(update={"database": dest, "state_directory": str(state)})
    with Core(settings, clock=core.clock) as restored:
        with pytest.raises(CoreError, match=BLOCK):
            budget.reserve(restored.store, settings, None, "chat", MESSAGES, core.clock())
        media = service(restored)
        session = start(media)
        with pytest.raises(CoreError, match=BLOCK):
            process(media, session, chunk())
        assert media.provider.calls == []
        assert not restored.store.rows("SELECT * FROM model_usage")
        assert not restored.store.rows("SELECT * FROM cloud_cost_snapshots")


def test_restore_keeps_known_spend_and_pending_liability_while_blocked(
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
        assert BLOCK in cloud_budget.capacity_blocks(store, settings, core.clock())
