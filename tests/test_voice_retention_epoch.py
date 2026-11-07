"""Raw retention retires stale voice evidence without stranding the current chat."""

import base64
import json
import sys
from datetime import timedelta

import httpx
import pytest

from companion import cloud_budget, owner_authority, transport
from companion.contracts import CoreError, DeliveryResult, LeaseRequest
from companion.core import Core
from companion.model import ModelClient
from companion.privacy import epoch
from companion.retrieval import SemanticIndex
from companion.store import encode
from companion.voice_jobs import INPUT_RECEIPT
from companion.voice_protocol import (
    ROUTE_PREFIX,
    VoiceClaimResponse,
    VoiceLeaseRef,
    VoiceStopRef,
    canonical_bytes,
    digest,
)
from tests.conftest import ingest, run
from tests.support.voice_jobs import TOKEN, result, voice_config
from tests.test_voice_flow import signed_input
from tests.test_voice_node_api import claim_body, tls_core

OLD_TRANSCRIPT = "不应在新隐私版本读取的旧语音转写"
REASON = "VOICE_INPUT_PRIVACY_EPOCH_CHANGED"


def expired_input_with_settled_charge(core):
    """Retain real native accounting for a completed, independently old conversation."""
    current = core.clock.now
    core.clock.now -= timedelta(days=core.settings.raw_event_retention_days + 1)
    old = ingest(core, 99, "独立的过期合成输入")
    assert run(core.run_once("old-settled-chat"))
    item = transport.lease_deliveries(core.store, core.settings,
        LeaseRequest(worker_id="old-synthetic-delivery"), core.clock())["items"][0]
    transport.begin_send(core.store, item["message_id"], item["lease_token"], core.clock(),
                         core.settings, clock=core.clock)
    transport.delivery_result(core.store, core.settings, item["message_id"],
        DeliveryResult(lease_token=item["lease_token"], result="SENT",
                       provider_message_id="old-synthetic-ack"), core.clock())
    core.store.execute("UPDATE events SET memory_processed=1 WHERE event_id=?", (old["event_id"],))
    core.clock.now = current
    # Retention crosses the default 30-day FX interval. Renew only the current
    # policy through its native API; the old fee keeps its original FX snapshot.
    cloud_budget.configure(core.store, core.settings, core.settings.cloud_budget,
        expected_version=1, idempotency_key="retention-fx-renewal", now=core.clock())
    charges = core.store.rows("SELECT * FROM model_usage")
    assert len(charges) == 1 and charges[0]["state"] == "SETTLED"
    assert charges[0]["billed_micro_usd"] > 0
    costs = core.store.rows("SELECT * FROM cloud_cost_snapshots")
    return old["event_id"], charges, costs


async def start_asr(client, core, event, key, *, complete):
    data, proof = signed_input(core, event["event_id"], key, index=0)
    headers = {"Content-Type": "audio/wav",
               "x-voice-proof": base64.urlsafe_b64encode(canonical_bytes(proof)).decode()}
    uploaded = await client.post("/internal/v1/voice-inputs/" + event["event_id"],
                                 content=data, headers=headers)
    assert uploaded.status_code == 200, uploaded.text
    body = claim_body(core)
    node_headers = {
        "Authorization": "Bearer " + TOKEN,
        "Idempotency-Key": f"voice-claim:{body['incarnation']}:{body['sequence']}",
    }
    response = await client.post(ROUTE_PREFIX + "/claim", json=body, headers=node_headers)
    assert response.status_code == 200, response.text
    lease = VoiceClaimResponse.model_validate(response.json()).lease
    ref = VoiceLeaseRef.from_lease(lease)
    assert ref.job_id == uploaded.json()["job_id"]
    path = ROUTE_PREFIX + "/jobs/" + ref.job_id
    released = await client.post(path + "/input", json=ref.model_dump(), headers=node_headers)
    assert released.status_code == 200 and released.content == data
    value = result(ref, operation="asr", text=OLD_TRANSCRIPT)
    if complete:
        ready = await client.post(path + "/control", json={
            "ref": ref.model_dump(), "phase": "RESULT_READY", "resource_state": "READY",
            "result_sha256": value.result_sha256,
        }, headers=node_headers)
        assert ready.status_code == 200 and ready.json()["action"] == "CONTINUE"
        accepted = await client.put(path + "/result", json=value.model_dump(mode="json"),
            headers={**node_headers,
                     "Idempotency-Key": f"voice-result:{ref.job_id}:{ref.generation}"})
        assert accepted.status_code == 200, accepted.text
    return ref, value, data, headers, VoiceStopRef.from_lease(lease)


@pytest.mark.parametrize("voice_state", ["missing", "running", "transcribed", "fallback",
                                         "source-erased"])
def test_retention_epoch_keeps_active_source_usable_and_old_voice_unusable(
    core, tmp_path, monkeypatch, voice_state,
):
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    old_id, old_charges, old_costs = expired_input_with_settled_charge(core)
    calls, failures = [], []
    voice = voice_config()
    old_proof = None
    active_worker = None

    from tests.voice_owner_fixtures import native_voice

    capture, key, _ = native_voice(core, tmp_path, config=voice)
    event = core.store.one("SELECT * FROM events WHERE event_id=?", (capture.event_id,))

    def model(request):
        calls.append(json.loads(request.content)["messages"])
        if old_proof is not None:
            job = active_worker.store.one("SELECT * FROM jobs WHERE job_id=?",
                                           (old_proof["job_id"],))
            with pytest.raises(CoreError, match="VOICE_CONTEXT_RESULT_CHANGED"):
                active_worker.voice.check_context(job, old_proof)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "这条语音暂时不可用，请补充文字。"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    async def scenario():
        nonlocal old_proof, active_worker
        import ssl

        async with tls_core(core, tmp_path) as (origin, certificate), httpx.AsyncClient(
            base_url=origin, verify=ssl.create_default_context(cafile=str(certificate)),
            trust_env=False, headers={"Authorization": "Bearer "
                + core.settings.bridge_token.get_secret_value()}) as client, \
                httpx.AsyncClient(transport=httpx.MockTransport(model)) as model_http:
            path = "/internal/v1/voice-inputs/" + event["event_id"]
            receipt_key = INPUT_RECEIPT + event["event_id"]
            with Core(core.settings, ModelClient(core.settings, model_http),
                      core.clock) as waiting:
                assert waiting.store is not core.store
                assert not await waiting.run_once("retention-voice-wait")
            receipt = json.loads(core.store.one(
                "SELECT value FROM runtime_meta WHERE key=?", (receipt_key,))["value"])
            assert receipt["state"] == "WAITING" and not calls
            node = None
            if voice_state in {"running", "transcribed", "source-erased"}:
                node = await start_asr(client, core, event, key,
                                       complete=voice_state == "transcribed")
            if voice_state == "transcribed":
                assert core.voice.input_status(event["event_id"]).state == "READY"
            if voice_state == "fallback":
                core.clock.advance(voice.asr.wait_seconds + 1)
                assert core.voice.input_status(event["event_id"]).state == "FALLBACK"
                previous = json.loads(core.store.one("SELECT value FROM runtime_meta "
                    "WHERE key=?", (receipt_key,))["value"])
                old_proof = {"event_id": event["event_id"],
                    "job_id": core.store.one("SELECT job_id FROM jobs WHERE event_id=?",
                                               (event["event_id"],))["job_id"],
                    "input_receipt_sha256": digest(previous), "voice_results": []}
            old_node = core.store.one("SELECT * FROM voice_jobs") if node else None
            before_epoch = epoch(core.store)
            with Core(core.settings, clock=core.clock) as maintenance:
                if voice_state == "source-erased":
                    with maintenance.store.transaction():
                        maintenance.ledger.expire_events(maintenance.store,
                            [event["event_id"]], maintenance.clock())
                else:
                    assert maintenance.expire_raw_events() == 1
            assert epoch(core.store) == before_epoch + 1
            current = core.store.one(
                "SELECT redacted,body_json FROM events WHERE event_id=?",
                (event["event_id"],))
            assert current["redacted"] == int(voice_state == "source-erased")
            if voice_state != "source-erased":
                assert current["body_json"] == event["body_json"]
                assert core.store.one("SELECT redacted FROM events WHERE event_id=?",
                                       (old_id,))["redacted"] == 1
            assert owner_authority.current(
                core.store, core.settings, core.clock).authority.as_dict() == (
                capture.authority)
            before = core.store.db.total_changes
            projected = await client.get(path + "/state")
            if voice_state == "source-erased":
                assert projected.status_code in {403, 409}, projected.text
            else:
                assert projected.status_code == 200, projected.text
                assert projected.json()["state"] == "FALLBACK"
                assert all(not row["upload_allowed"] for row in projected.json()["records"])
            assert core.store.db.total_changes == before
            with Core(core.settings, ModelClient(core.settings, model_http),
                      core.clock) as worker:
                active_worker = worker
                fail_job = worker.fail_job

                def record_failure(job, code):
                    failures.append((code, repr(sys.exception())))
                    fail_job(job, code)

                monkeypatch.setattr(worker, "fail_job", record_failure)
                assert await worker.run_once("retention-voice-recovery") is (
                    voice_state != "source-erased")
            expected_calls = int(voice_state != "source-erased")
            assert len(calls) == expected_calls, (failures, core.store.rows(
                "SELECT event_id,state,last_error_code FROM jobs"))
            if calls:
                evidence = [json.loads(message["content"].split("\n", 1)[1])
                    for message in calls[0] if isinstance(message["content"], str)
                    and '"kind":"untrusted_transcription"' in message["content"]]
                assert len(evidence) == 1 and evidence[0]["status"] == "UNAVAILABLE"
                assert evidence[0]["transcripts"] == []
                assert evidence[0]["unavailable"] == [{"segment_index": 0,
                                                       "reason": REASON}]
                assert OLD_TRANSCRIPT not in encode(calls)
                replacement = json.loads(core.store.one("SELECT value FROM runtime_meta "
                    "WHERE key=?", (receipt_key,))["value"])
                assert replacement["privacy_epoch"] == epoch(core.store)
                assert replacement["wait_until"] == receipt["wait_until"]
                assert replacement["state"] == "FALLBACK"
            if node:
                ref, value, audio, upload_headers, stop = node
                saved = core.store.one("SELECT * FROM voice_jobs WHERE job_id=?",
                                       (ref.job_id,))
                assert saved["state"] == ("CANCELLED" if voice_state == "transcribed"
                                          else "UNKNOWN")
                assert saved["input_blob"] is None and saved["result_blob"] is None
                for field in ("captured_privacy_epoch", "generation", "started_at"):
                    assert saved[field] == old_node[field]
                assert saved["input_sha256"] is saved["result_sha256"] is None
                node_path = ROUTE_PREFIX + "/jobs/" + ref.job_id
                late = await client.put(node_path + "/result",
                    json=value.model_dump(mode="json"), headers={
                        "Authorization": "Bearer " + TOKEN,
                        "Idempotency-Key": f"voice-result:{ref.job_id}:{ref.generation}",
                    })
                assert late.status_code == 410, late.text
                assert late.json()["error"] == "VOICE_LEASE_RETIRED"
                before_replay = core.store.db.total_changes
                replayed = await client.post(path, content=audio, headers=upload_headers)
                if voice_state == "source-erased":
                    assert replayed.status_code in {403, 409}, replayed.text
                else:
                    assert replayed.status_code == 410, replayed.text
                    assert replayed.json()["error"] == "VOICE_JOB_RETIRED"
                    assert core.store.one("SELECT * FROM voice_jobs WHERE job_id=?",
                                           (ref.job_id,)) == saved
                    assert core.store.db.total_changes == before_replay
                if voice_state != "transcribed":
                    held = core.store.one("SELECT active_job_id FROM voice_node_state")
                    assert held["active_job_id"] == ref.job_id
                    stopped = await client.post(node_path + "/stop", json={
                        "ref": stop.model_dump(), "outcome": "CANCELLED",
                        "resource_state": "READY", "cleanup_epoch": saved["cleanup_epoch"],
                        "cleanup": {"cache_removed": True, "journal_removed": True,
                                    "temp_removed": True},
                    }, headers={"Authorization": "Bearer " + TOKEN})
                    assert stopped.status_code == 200
                    assert stopped.json()["custody_closed"]
                    assert stopped.json()["cleanup_confirmed"]
                    assert core.store.one("SELECT active_job_id FROM voice_node_state")[
                        "active_job_id"] is None
                assert len(core.store.rows("SELECT * FROM voice_jobs")) == 1
            charges = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
            assert charges[:len(old_charges)] == old_charges
            assert len(charges) == len(old_charges) + expected_calls
            assert all(row["state"] == "SETTLED" for row in charges)
            costs = core.store.rows("SELECT * FROM cloud_cost_snapshots ORDER BY rowid")
            assert costs[:len(old_costs)] == old_costs
            assert len(costs) == len(old_costs) + expected_calls
            replies = core.store.rows("SELECT * FROM outbox WHERE origin_event_id=?",
                                       (event["event_id"],))
            assert len(replies) == expected_calls
            with Core(core.settings, ModelClient(core.settings, model_http),
                      core.clock) as restarted:
                assert not await restarted.run_once("retention-voice-restart")
            assert len(calls) == expected_calls

    run(scenario())
