"""Stopped input retirement retains usage and never claims a delivery ACK."""

import json

import httpx
import pytest
from pydantic import SecretStr

from companion.privacy import advance_epoch
from companion.store import encode, uid
from companion_onebot_bridge import owner_handoff
from tests.conftest import run
from tests.native_continuous_support import frame, host
from tests.native_owner_recovery_support import CONTROL_TOKEN, reopen_host, stop_host
from tests.support.onebot_events import raw_event


@pytest.mark.parametrize("mutation", [None, "model", "outbox", "source", "active", "proof",
                                     "erased", "privacy_epoch", "settled", "settled_recalled",
                                     "admission_job", "admission_authority", "delivery_content"])
def test_stopped_input_requires_original_native_materials(
    core, tmp_path, monkeypatch, mutation,
):
    generated = mutation in {"settled", "settled_recalled", "admission_job",
                             "admission_authority", "delivery_content"}

    async def source(ws):
        await ws.send(frame())
        await ws.wait_closed()

    async def scenario():
        async with host(core, tmp_path, source,
                        core_options={"control_token": SecretStr(CONTROL_TOKEN)}) as (
            active, _, sends, calls,
        ):
            await active.capture()
            await active.flush()
            event_id = core.store.one("SELECT event_id FROM jobs")["event_id"]
            if generated:
                assert await core.run_once()
            if mutation != "active":
                await stop_host(active)
            if mutation in {"erased", "settled_recalled"}:
                with core.store.transaction(control_write=True):
                    core.ledger.expire_events(core.store, [event_id], core.clock())
            elif mutation == "privacy_epoch":
                with core.store.transaction(control_write=True):
                    advance_epoch(core.store)
            if mutation == "source":
                core.store.execute('UPDATE events SET body_json=%s WHERE event_id=%s',
                                   (encode({"text": "changed"}), event_id))
            if mutation in {"model", "outbox"}:
                job = core.store.one("SELECT * FROM jobs")
                if mutation == "model":
                    core.store.insert("model_usage", usage_id=uid("usage"), request_id=uid("model"),
                        job_id=job["job_id"], scenario="chat", provider="mock", model="synthetic",
                        input_tokens=0, output_tokens=0, billed_micro_usd=1,
                        state="SETTLED", occurred_at=core.clock())
                else:
                    core.store.insert("outbox", message_id=uid("msg"), origin_event_id=event_id,
                        conversation_id=core.store.one("SELECT conversation_id FROM events")[
                            "conversation_id"], action_type="qq.private.reply",
                        content_json="{}", content_sha256="a" * 64, delivery_key="synthetic",
                        state="FAILED", attempt_count=1, created_at=core.clock(),
                        updated_at=core.clock())
            if mutation == "admission_job":
                core.store.execute("UPDATE model_admissions SET logical_request_id='foreign-job'")
            elif mutation == "admission_authority":
                core.store.execute("UPDATE model_admissions SET authority_json='{}'")
            elif mutation == "delivery_content":
                core.store.execute('UPDATE outbox SET content_json=%s',
                                   (encode({"text": "changed"}),))
            accounting = {table: core.store.rows(f"SELECT * FROM {table}") for table in (
                "model_usage", "model_admissions", "outbox")}
            if mutation == "proof":
                original = owner_handoff.bounded_post

                async def changed(client, path, body):
                    result = await original(client, path, body)
                    return result | {"input_sha256": "0" * 64} if path.endswith(
                        "/inputs/state") else result

                monkeypatch.setattr(owner_handoff, "bounded_post", changed)
            if mutation == "active":
                before = active.runtime.row()["journal_head"]
                with pytest.raises(ValueError, match="DELIVERY_QUIESCENT_PROCESS_REQUIRED"):
                    await owner_handoff.prepare(active.runtime, active.client)
                assert active.runtime.row()["journal_head"] == before
                return
            runtime = reopen_host(active, monkeypatch)
            with runtime.spool:
                before = runtime.row()["journal_head"]
                if mutation in {"model", "outbox", "source", "proof", "admission_job",
                                "admission_authority", "delivery_content"}:
                    with pytest.raises((ValueError, httpx.HTTPStatusError)):
                        await owner_handoff.prepare(runtime, active.client)
                    assert runtime.row()["journal_head"] == before
                else:
                    result = await owner_handoff.prepare(runtime, active.client)
                    assert result["state"] == "HANDOFF_PREPARED"
                    records = runtime.verify(active=False)
                    terminal = [r for r in records if r["kind"] == "OWNER_INPUT_TERMINAL"]
                    assert len(terminal) == 1 and terminal[0]["data"]["terminal"]
                    assert not any(r["kind"] == "ACK" for r in records)
                    assert (await owner_handoff.prepare(runtime, active.client)) == result
                    if mutation in {"erased", "settled_recalled"}:
                        assert terminal[0]["data"]["state"] == "RECALLED"
                    else:
                        capture = json.loads(runtime.db.execute(
                            "SELECT capture_json FROM window_inputs").fetchone()[0])
                        core.store.execute('UPDATE events SET body_json=%s WHERE event_id=%s',
                                           (encode({"text": "changed"}), event_id))
                        proof = await active.client.post(
                            "/internal/v1/owner-authority/inputs/state", json=capture)
                        assert proof.status_code in {403, 409}
                assert runtime.row()["state"] == "STOPPED"
                assert runtime.db.execute("SELECT COUNT(*) FROM window_inputs").fetchone()[0] == 1
                assert runtime.db.execute("SELECT state FROM window_inputs").fetchone()[0] == (
                    "CAPTURED")
                assert runtime.db.execute("SELECT COUNT(*) FROM window_attempts").fetchone()[0] == 0
            assert sends == []
            assert len(calls) == int(generated)
            assert accounting == {table: core.store.rows(f"SELECT * FROM {table}")
                                  for table in accounting}

    run(scenario())


@pytest.mark.parametrize("paid", [False, True])
def test_cancelled_input_proof_survives_handoff_and_cannot_be_forged_in_spool(tmp_path, paid):
    from companion_onebot_bridge.spool import Spool
    from companion_onebot_bridge.window import TestWindow, digest
    from tests.support.onebot_events import normalized
    from tests.support.onebot_window import input_approval
    from tests.support.onebot_window_handoff import CoreContract, plan_for

    cfg, plan, now = plan_for(tmp_path)

    class Contract(CoreContract):
        def __call__(self, request):
            if request.url.path.endswith('/inputs/cancelled'):
                body=json.loads(request.content)
                proof = {**body,'input_sha256':digest('你好'),
                    'state':'CANCELLED','event_sha256':'a'*64,'job_sha256':'b'*64,
                    'privacy_epoch':0,'model_attempts':0,'send_attempts':0}
                if paid:
                    proof.update(model_attempts=1, settled_model_usage_sha256='c'*64,
                                 billed_micro_usd=295)
                return httpx.Response(200,json=proof)
            return super().__call__(request)

    contract=Contract(plan)

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as client:
                old=TestWindow(spool,plan,clock=lambda:now)
                await old.open(client)
                raw=raw_event()
                await old.ingest(client,raw,input_approval(plan,raw))
                # Native proof validation and authenticated receipts are exercised
                # above; this synthetic contract isolates Bridge succession.
                with spool.db:
                    spool.db.execute("UPDATE window_inputs SET capture_json='{}'")
                await old.stop_live(client)
                await old.reconcile_cancelled_inputs(client)
                _,new_plan,_=plan_for(tmp_path,2,predecessor=old)
                contract.successor=new_plan
                new=TestWindow(spool,new_plan,clock=lambda:now)
                await new.handoff(client)
                await new.open(client)
                assert new.verify()['state']=='OPEN'
                assert old.count('window_inputs')==1 and new.count('window_inputs')==0
                assert spool.db.execute(
                    "SELECT state FROM window_inputs").fetchone()[0]=='CANCELLED'
                event_id=normalized(raw,cfg)['event_id']
                with spool.db:
                    spool.db.execute("UPDATE window_inputs SET input_hash=? WHERE event_id=?",
                                     ('0'*64,event_id))
                with pytest.raises(ValueError,match='CANCELLED_INPUT_CORE_PROOF_REQUIRED'):
                    new.verify()

    run(scenario())
