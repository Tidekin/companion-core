"""Current chat permission is independent of optional context and native execution leases."""

import asyncio
import json

import httpx
import pytest

from companion import budget, owner_authority, owner_delivery, owner_input
from companion.contracts import CoreError, LeaseRequest
from companion.model import ModelClient
from companion.owner_authority_protocol import AuthorityStop, OwnerSendCheck
from companion.store import after, encode, uid
from companion.transport import begin_send, enqueue, lease_deliveries
from tests.conftest import ingest
from tests.support.evaluation import response, work
from tests.support.owner_input import captured, inbound
from tests.support.owner_runtime import chat


def test_chat_only_context_never_reads_other_purposes(core, tmp_path, monkeypatch):
    _, job = chat(core, tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("chat permission accessed another purpose")

    for component, method in [(core.affect, "snapshot"), (core.memory_context, "recall"),
                               (core.life, "context"), (core.expression, "instruction_parts"),
                               (core.expression_learning, "snapshot")]:
        monkeypatch.setattr(component, method, forbidden)
    messages = core.context(job)
    assert messages[-1] == {"role": "user", "content": "synthetic owner input"}
    assert asyncio.run(core.recall_for_reply(job)) == ([], False)
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_PURPOSE_REQUIRED"):
        budget.reserve(core.store, core.settings, job["job_id"], "memory_review", messages,
                       core.clock(), clock=core.clock)
    assert core.store.rows("SELECT * FROM model_usage") == []


@pytest.mark.parametrize("fence", [None, "stop", "lease"])
def test_native_chat_admission_rechecks_authority_after_http_hooks(core, tmp_path, fence):
    capture, job = chat(core, tmp_path)
    messages = core.context(job)
    request_id = budget.reserve(core.store, core.settings, job["job_id"], "chat", messages,
                                core.clock(), clock=core.clock)
    row = core.store.one('SELECT authority_json FROM model_admissions WHERE request_id=%s',
                         (request_id,))
    assert json.loads(row["authority_json"]) == capture.authority
    calls = []

    async def hook(request):
        if fence == "stop":
            owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                operation_id="operation-stop-native-model", authority=capture.authority))
        elif fence == "lease":
            core.clock.advance(core.settings.lease_seconds + 1)

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: calls.append(request) or response(request)), trust_env=False,
            event_hooks={"request": [hook]}) as client:
            return await budget.complete(ModelClient(core.settings, client), core.store,
                core.settings, request_id, messages, "chat", core.clock)

    if fence:
        with pytest.raises(CoreError):
            asyncio.run(execute())
        assert calls == []
    else:
        assert asyncio.run(execute()).text == "SYNTHETIC observed reply"
        assert len(calls) == 1
    state = core.store.one('SELECT state,billed_micro_usd FROM model_usage WHERE request_id=%s',
                           (request_id,))
    assert state["state"] == "SETTLED"
    if fence:
        # The controlled transport proves that the provider delegate was never reached.
        assert state["billed_micro_usd"] == 0


def test_native_chat_outbox_requires_same_reference_and_live_send_lease(core, tmp_path):
    snapshot_reads = []

    def trace(sql):
        if sql.startswith("SELECT phase,snapshots_json FROM agent_conversations"):
            snapshot_reads.append(core.store.frozen_scope)

    core.store.db.set_trace_callback(trace)
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    source = core.ingest(inbound(core, capture), "full-native-chat")
    assert work(core)
    job = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (source["job_id"],))
    assert job["state"] == "SUCCEEDED", job["last_error_code"]
    row = core.store.one('SELECT * FROM outbox WHERE origin_event_id=%s', (capture.event_id,))
    assert json.loads(row["authority_json"]) == capture.authority
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_REFERENCE_REQUIRED"):
        lease_deliveries(core.store, core.settings, LeaseRequest(worker_id="bridge"), core.clock())
    item = lease_deliveries(core.store, core.settings, LeaseRequest(
        worker_id="bridge", authority=capture.authority), core.clock())["items"][0]
    check = OwnerSendCheck(authority=item["authority"], message_id=item["message_id"],
        event_id=item["origin_event_id"], content_sha256=item["content_sha256"],
        lease_token=item["lease_token"], reply_revision=item["reply_revision"])
    with pytest.raises(CoreError, match="OWNER_DELIVERY_LEASE_LOST"):
        owner_delivery.send_check(core.store, core.settings, core.clock, check)
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock(),
               core.settings, clock=core.clock)
    assert owner_delivery.send_check(core.store, core.settings, core.clock, check)["approved"]
    # The next preflight must read the original evidence again, even for the same lease.
    core.store.execute("UPDATE agent_conversations SET snapshots_json='[]' WHERE job_id=%s",
                       (job["job_id"],))
    with pytest.raises(CoreError, match="REPLY_CANCELLED"):
        owner_delivery.send_check(core.store, core.settings, core.clock, check)
    assert len(snapshot_reads) >= 5 and None not in snapshot_reads
    assert len(set(snapshot_reads)) == len(snapshot_reads)
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="operation-stop-native-send", authority=capture.authority))
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_DISABLED"):
        owner_delivery.send_check(core.store, core.settings, core.clock, check)
    assert core.store.one('SELECT state FROM outbox WHERE message_id=%s',
                           (item["message_id"],))["state"] == "SENDING"


def test_retire_old_timer_queue_preserves_admitted_running_and_delivery_custody(core):
    source = ingest(core)
    conversation = core.store.one('SELECT conversation_id FROM events WHERE event_id=%s',
                                   (source["event_id"],))["conversation_id"]
    jobs = {}
    for kind in ("queued", "model", "outbox", "running", "notice"):
        event_id = uid("activity")
        core.store.insert("events", event_id=event_id, schema_version=1, source="system.timer",
            source_event_id=event_id, conversation_id=conversation, actor_id="system",
            event_type="activity.tick", occurred_at=core.clock(), ingested_at=core.clock(),
            privacy_scope="private", body_json=encode({"text": "synthetic timer", **(
                {"notice": {"kind": "share"}} if kind == "notice" else {})}), trace_id=event_id)
        jobs[kind] = core.new_job(event_id, "activity", event_id, core.clock())
        if kind == "model":
            budget.reserve(core.store, core.settings, jobs[kind], "memory_review",
                           [{"role": "user", "content": "synthetic"}], core.clock())
        elif kind == "outbox":
            enqueue(core.store, event_id, conversation, "synthetic", core.clock(),
                    action="qq.private.proactive")
        elif kind == "running":
            core.store.execute("UPDATE jobs SET state='RUNNING',lease_until=%s WHERE job_id=%s",
                                (after(core.clock(), 60), jobs[kind]))
    usage = core.store.rows("SELECT * FROM model_usage")
    outbox = core.store.rows("SELECT * FROM outbox")
    with core.store.transaction():
        core.recover(core.clock())
        core.recover(core.clock())
    for kind, job_id in jobs.items():
        row = core.store.one('SELECT state,last_error_code FROM jobs WHERE job_id=%s', (job_id,))
        assert row["state"] == ("CANCELLED" if kind == "queued" else (
            "RUNNING" if kind == "running" else "QUEUED"))
        if kind == "queued":
            assert row["last_error_code"] == "LEGACY_TIMER_RETIRED"
    assert core.store.rows("SELECT * FROM model_usage") == usage
    assert core.store.rows("SELECT * FROM outbox") == outbox
