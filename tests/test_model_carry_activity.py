"""A closed independent activity keeps global liability outside QQ input receipts."""

import json
from pathlib import Path

import httpx
import pytest

from companion import budget, job_queue, model_carry, owner_authority, paid_budget
from companion.contracts import CoreError
from companion.model import ModelClient
from companion_delivery_protocol import digest
from tests.conftest import run
from tests.model_carry_support import MESSAGES, migrated
from tests.support.model_carry_lifecycle import current_unknown, stop_current
from tests.support.source_definitions import accepted_text


def mixed_unknowns(core, tmp_path, *, close_activity=True):
    core.settings.social_enabled = core.settings.proactive_enabled = True
    core.settings.proactive_idle_seconds = 0
    core.settings.quiet_start = core.settings.quiet_end = 0
    source = accepted_text(core)
    case = migrated(core, tmp_path, purposes=("chat", "proactive", "social"))
    input_request = current_unknown(core)
    input_job = core.store.one("SELECT job_id FROM model_usage WHERE request_id=%s",
                               (input_request,))["job_id"]
    job_queue.release(core.store, input_job, "FAILED", core.clock(), reason="MODEL_RETRYABLE")
    core.observations.append("reader", source)
    core.social.consider(source.event_id)
    assert core.social.flush()
    job = core.store.one("SELECT * FROM jobs WHERE kind='activity' AND state='QUEUED'")
    assert job and core.claim_job("native-activity", job_id=job["job_id"])
    request_id = budget.reserve(core.store, core.settings, job["job_id"], "activity",
                                MESSAGES, core.clock())

    def provider(request):
        raise httpx.ReadTimeout("synthetic interrupted activity", request=request)

    async def interrupted():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            with pytest.raises(CoreError, match="MODEL_RETRYABLE"):
                if close_activity:
                    await budget.complete(ModelClient(core.settings, client), core.store,
                        core.settings, request_id, MESSAGES, "activity", core.clock)
                else:
                    await budget._dispatch(ModelClient(core.settings, client), core.store,
                        core.settings, request_id, MESSAGES, "activity", core.clock, dispatch={})

    run(interrupted())
    # The provider call has returned through the original native close path.
    if not close_activity:
        budget.settle(core.store, core.settings, request_id)
    job_queue.release(core.store, job["job_id"], "FAILED", core.clock(),
                      reason="WORKER_LEASE_EXPIRED")
    stopped = stop_current(core, case)
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key=%s",
                              ("owner_input:" + source.event_id,))
    return case, request_id, input_request, job["job_id"], stopped


def receipt(core, case):
    return model_carry.read(core.store, core.settings, case.successor["authority"],
        case.receipt["carry_sha256"], digest(case.policy.model_dump(mode="json")))


@pytest.mark.parametrize("terminal", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_closed_native_activity_is_retained_globally_without_a_qq_input(core, tmp_path, terminal):
    case, activity, input_request, job_id, _ = mixed_unknowns(core, tmp_path)
    core.store.execute("UPDATE jobs SET state=%s WHERE job_id=%s", (terminal, job_id))
    before = {table: core.store.rows("SELECT * FROM " + table + " ORDER BY request_id")
              for table in ("model_usage", "model_admissions")}
    proof = receipt(core, case)
    assert proof["request_ids"] == [input_request]
    assert activity not in {request for item in proof["inputs"] for request in item["request_ids"]}
    assert proof["retained_micro_usd"] == core.store.one(
        "SELECT billed_micro_usd FROM model_usage WHERE request_id=%s",
        (input_request,))["billed_micro_usd"]
    with core.store.transaction():
        view = owner_authority.custody(core.store, core.settings, settlement=True)
    assert activity in view["model_closures"]
    assert view["model_latest"][activity]["state"] == "UNKNOWN"
    assert len(view["model_carries"]) == 1
    assert paid_budget.snapshot(core.settings)["window_id"] == "successor-window-1"
    from tests.support.owner_authority import activate

    activate(core, case.policy, revision=4, operation="resume-with-closed-unbound-activity")
    assert receipt(core, case) == proof
    for table, rows in before.items():
        assert core.store.rows("SELECT * FROM " + table + " ORDER BY request_id") == rows


@pytest.mark.parametrize("change", [
    "unclosed", "chat", "poke", "running", "authority", "malformed_authority", "logical",
    "scenario", "source", "owner", "corrupt_closure",
])
def test_unbound_activity_scope_never_retires_other_work_or_changed_evidence(
    core, tmp_path, change,
):
    case, activity, _, job_id, _ = mixed_unknowns(
        core, tmp_path, close_activity=change != "unclosed")
    if change in {"chat", "poke"}:
        core.store.execute("UPDATE jobs SET kind=%s WHERE job_id=%s", (change, job_id))
    elif change == "running":
        core.store.execute("UPDATE jobs SET state='RUNNING' WHERE job_id=%s", (job_id,))
    elif change in {"authority", "malformed_authority"}:
        changed = json.dumps(case.active["authority"] if change == "authority" else {"version": 2})
        core.store.execute("UPDATE jobs SET authority_json=%s WHERE job_id=%s", (changed, job_id))
    elif change == "logical":
        core.store.execute("UPDATE model_admissions SET logical_request_id='job_changed' "
                           "WHERE request_id=%s", (activity,))
    elif change == "scenario":
        core.store.execute("UPDATE model_usage SET scenario='chat' WHERE request_id=%s",
                           (activity,))
    elif change == "source":
        core.store.execute("UPDATE events SET source='qq.onebot' WHERE event_id="
                           "(SELECT event_id FROM jobs WHERE job_id=%s)", (job_id,))
    elif change == "owner":
        core.store.execute("UPDATE conversations SET owner_actor_id='system' WHERE conversation_id="
                           "(SELECT conversation_id FROM events WHERE event_id="
                           "(SELECT event_id FROM jobs WHERE job_id=%s))", (job_id,))
    elif change == "corrupt_closure":
        path = Path(core.settings.paid_model.journal_path)
        lines = path.read_bytes().splitlines(keepends=True)
        for index, raw in enumerate(lines):
            row = json.loads(raw)
            if row.get("kind") == model_carry.CLOSE and row["request_id"] == activity:
                row["lease_sha256"] = "0" * 64
                lines[index] = json.dumps(row).encode() + b"\n"
                break
        else:
            pytest.fail("native activity close missing from fixture")
        path.write_bytes(b"".join(lines))
    before = {table: core.store.rows("SELECT * FROM " + table + " ORDER BY request_id")
              for table in ("model_usage", "model_admissions")}
    with pytest.raises(CoreError):
        receipt(core, case)
    for table, rows in before.items():
        assert core.store.rows("SELECT * FROM " + table + " ORDER BY request_id") == rows
