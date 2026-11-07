"""Converged snapshots are read-only without weakening native liability proofs."""

import pytest

from companion import cloud_budget
from companion.executive import erase_executive
from companion.executor_gateway import receipt
from tests.support.cloud_budget import charge, options
from tests.support.executive import configured, proposal


def tool(core, status=None):
    executive = configured(core)
    executive.config.endpoints["windows"].max_cost_micro_usd = 1000
    task = executive.submit(proposal())
    leased = executive.claim()
    _, request = executive.begin(leased)
    with core.store.transaction():
        cloud_budget.mark_dispatched(core.store, "tool", task["task_id"], core.clock())
    if status:
        executive.finish(leased, receipt(request, status, "synthetic" if status == "SUCCEEDED"
                                         else "", billed=125))
    return task["task_id"]


def snapshot(core):
    return core.store.one("SELECT * FROM cloud_cost_snapshots")


@pytest.mark.parametrize("status", [None, "SUCCEEDED", "FAILED"])
def test_converged_cancelled_tools_do_not_sync_reference_or_write(core, monkeypatch, status):
    tool(core, status)
    erase_executive(core.store, ["evt_test_1"])
    before = snapshot(core)
    assert before["state"] == ("SETTLED" if status else "UNKNOWN")
    calls = []
    original = cloud_budget.sync_reference

    def observe(*args):
        calls.append(args[2])
        return original(*args)

    monkeypatch.setattr(cloud_budget, "sync_reference", observe)
    changes = core.store.db.total_changes
    for _ in range(2):
        cloud_budget.sync(core.store, core.settings, core.clock())
    assert calls == []
    assert core.store.db.total_changes == changes
    assert snapshot(core) == before


def test_cancelled_without_completion_proof_still_becomes_unknown(core):
    task_id = tool(core)
    # Recovery/native projection change without the ordinary erasure sync hook.
    core.store.execute("UPDATE tool_tasks SET state='CANCELLED' WHERE task_id=?", (task_id,))
    assert snapshot(core)["state"] == "RESERVED"
    core.store.audit("tool.finished", task_id, task_id, core.clock(), status="UNKNOWN")
    cloud_budget.sync(core.store, core.settings, core.clock())
    before = snapshot(core)
    assert before["state"] == "UNKNOWN" and before["accounted_micro_usd"] == 1000
    # Later generic completion text is not a native tool UNKNOWN reconciliation protocol.
    core.store.audit("tool.finished", task_id, task_id, core.clock(), status="SUCCEEDED")
    core.store.execute("UPDATE tool_tasks SET billed_micro_usd=1 WHERE task_id=?", (task_id,))
    changes = core.store.db.total_changes
    cloud_budget.sync(core.store, core.settings, core.clock())
    assert snapshot(core) == before
    assert core.store.db.total_changes == changes


def test_direct_sync_preserves_sticky_model_charge_without_rewriting(core):
    charge(core, "model-unknown", 1000)
    core.store.execute("UPDATE model_usage SET state='UNKNOWN'")
    cloud_budget.sync(core.store, core.settings, core.clock())
    core.store.execute("UPDATE model_usage SET state='SETTLED',billed_micro_usd=1")
    before = snapshot(core)
    changes = core.store.db.total_changes
    with core.store.transaction():
        cloud_budget.sync_reference(core.store, "model", "model-unknown")
    assert snapshot(core) == before
    assert core.store.db.total_changes == changes
    assert before["state"] == "UNKNOWN" and before["accounted_micro_usd"] == 1000


def test_native_dispatch_day_change_uses_frozen_snapshot_then_converges(core):
    options(core, usd_to_cny_micro=8_000_000)
    charge(core, "legacy-dispatch", 1000)
    # Legacy entries derive dispatch from native custody; modern tools use mark_dispatched.
    core.store.execute("UPDATE cloud_cost_snapshots SET legacy=1")
    original = snapshot(core)
    core.clock.advance(86400)
    options(core, usd_to_cny_micro=1_000_000)
    core.settings.timezone = "America/Los_Angeles"
    task_id = tool(core)
    # Exercise the original tool dispatch fallback with its original frozen policy.
    core.store.execute("DELETE FROM cloud_cost_snapshots WHERE reference_id='legacy-dispatch'")
    core.store.execute("UPDATE cloud_cost_snapshots SET legacy=1,dispatched_at=NULL,local_day=?",
                       (original["local_day"],))
    with core.store.transaction():
        cloud_budget.sync_reference(core.store, "tool", task_id)
    settled = snapshot(core)
    assert settled["local_day"] != original["local_day"]
    assert settled["amount_micro_cny"] == 8000
    assert settled["dispatched_at"] == core.clock()
    changes = core.store.db.total_changes
    with core.store.transaction():
        cloud_budget.sync_reference(core.store, "tool", task_id)
    assert snapshot(core) == settled and core.store.db.total_changes == changes


@pytest.mark.parametrize("snapshot_state", ["RESERVED", "SETTLED", "UNKNOWN"])
@pytest.mark.parametrize("proof", [None, "SUCCEEDED", "UNKNOWN"])
@pytest.mark.parametrize("billed", [0, 1000])
def test_cancelled_selection_matches_direct_native_reconciliation(
    core, snapshot_state, proof, billed,
):
    task_id = tool(core)
    core.store.execute("UPDATE tool_tasks SET state='CANCELLED',billed_micro_usd=?", (billed,))
    if proof:
        core.store.audit("tool.finished", task_id, task_id, core.clock(), status=proof)
    core.store.execute("UPDATE cloud_cost_snapshots SET state=?", (snapshot_state,))
    original = snapshot(core)
    with core.store.transaction():
        cloud_budget.sync_reference(core.store, "tool", task_id)
    expected = snapshot(core)
    with core.store.transaction():
        core.store.execute("DELETE FROM cloud_cost_snapshots")
        core.store.insert("cloud_cost_snapshots", **original)
    cloud_budget.sync(core.store, core.settings, core.clock())
    assert snapshot(core) == expected
    if snapshot_state == "UNKNOWN":
        assert expected["state"] == "UNKNOWN"
        assert expected["accounted_micro_usd"] == original["accounted_micro_usd"]



def test_cancelled_unknown_still_imports_larger_native_hold_and_legacy_dispatch(core):
    task_id = tool(core)
    erase_executive(core.store, ["evt_test_1"])
    before = snapshot(core)
    core.store.execute("UPDATE tool_tasks SET reserved_micro_usd=2000 WHERE task_id=?", (task_id,))
    core.store.execute("UPDATE cloud_cost_snapshots SET legacy=1,dispatched_at=NULL")
    cloud_budget.sync(core.store, core.settings, core.clock())
    after = snapshot(core)
    assert after["state"] == "UNKNOWN"
    assert after["accounted_micro_usd"] == 2000
    assert after["amount_micro_cny"] == 16000
    assert after["dispatched_at"] == before["dispatched_at"]
    changes = core.store.db.total_changes
    cloud_budget.sync(core.store, core.settings, core.clock())
    assert snapshot(core) == after and core.store.db.total_changes == changes
