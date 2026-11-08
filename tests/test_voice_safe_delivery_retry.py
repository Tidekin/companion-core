"""A known pre-send failure reuses selected audio without another local inference."""

import json
from contextlib import closing
from datetime import datetime

import httpx
import pytest

from companion import transport, voice_authority
from companion.contracts import CoreError, DeliveryResult, LeaseRequest
from companion.core import Core
from companion.model import ModelClient
from companion.store import Store, encode
from companion.voice_config import VoiceNode, VoiceReplyPolicy
from companion.voice_flow import VoiceFlow
from companion.voice_jobs import VoiceJobs, erase_voice_jobs
from companion.voice_protocol import digest
from companion_voice_node.worker import local_manifest
from tests.conftest import run
from tests.support.voice_flow import finish_node, flow_fixture, leased, prepare
from tests.support.voice_jobs import TEXT, claim, voice_config
from tests.support.voice_node_api import independent_node, tls_core
from tests.support.voice_node_worker import child_receipts
from tests.support.voice_node_worker import node as node  # noqa: F401 -- pytest fixture


def safe_retry(core, monkeypatch, stage="retry_wait"):
    flow, jobs, message_id, event_id, _ = flow_fixture(
        core, monkeypatch, job_ttl_seconds=600, result_ttl_seconds=300,
    )
    prepare(core, flow, message_id)
    finish_node(jobs)
    prepare(core, flow, message_id)
    first = leased(core, message_id)
    transport.delivery_result(core.store, core.settings, message_id, DeliveryResult(
        lease_token=first["lease_token"], result="RETRYABLE_FAILURE",
        error_code="SYNTHETIC_BEFORE_SEND"), core.clock())
    if stage != "retry_wait":
        core.clock.advance(6)
        leased(core, message_id)
        until = core.store.one("SELECT lease_until FROM outbox")["lease_until"]
        core.clock.advance((datetime.fromisoformat(until) - core.clock.now).total_seconds() + 1)
        if stage == "recovered_pending":
            with core.store.transaction():
                transport.recover_deliveries(core.store, core.clock())
    return flow, jobs, message_id, event_id, first


@pytest.mark.parametrize("stage", ["retry_wait", "expired_lease", "recovered_pending"])
def test_idle_node_preserves_selected_audio_for_safe_native_delivery_retry(
    core, monkeypatch, stage,
):
    _, _, message_id, _, first = safe_retry(core, monkeypatch, stage)
    original = core.store.one("SELECT * FROM voice_jobs")
    model_usage = core.store.rows("SELECT * FROM model_usage")
    charges = core.store.rows("SELECT * FROM cloud_cost_snapshots")
    with closing(Store(core.store.database, core.store.state_directory)) as other_store:
        assert other_store.backend_pid != core.store.backend_pid
        idle = VoiceJobs(other_store, core.settings, core.clock)
        assert claim(idle)[1].lease is None
    assert core.store.one("SELECT * FROM voice_jobs") == original
    core.clock.advance(6)
    second = leased(core, message_id)
    assert second["lease_token"] != first["lease_token"]
    assert second["voice"] == first["voice"]
    with core.store.transaction():
        envelope = VoiceFlow(core.store, core.settings, core.clock).delivery_envelope(
            message_id, second["lease_token"])
    assert envelope.binding.job_id == original["job_id"]
    assert core.store.one("SELECT COUNT(*) n FROM voice_jobs")["n"] == 1
    assert core.store.one("SELECT state,attempt_count,provider_message_id FROM outbox") == {
        "state": "LEASED", "attempt_count": 1, "provider_message_id": None,
    }
    assert core.store.rows("SELECT * FROM model_usage") == model_usage
    assert core.store.rows("SELECT * FROM cloud_cost_snapshots") == charges
    committed = core.store.one("SELECT COUNT(*) n FROM audit_log WHERE action='voice.committed'")
    assert committed["n"] == 1
    assert core.model.calls == []


@pytest.mark.parametrize("change", [
    "missing_binding", "binding_hash", "binding_extra", "binding_reply", "content",
    "source_erasure", "privacy_epoch", "reply_revision", "expired_result", "unknown_job",
])
def test_safe_retry_does_not_restore_expired_changed_or_unproven_audio(core, monkeypatch, change):
    _, jobs, message_id, event_id, _ = safe_retry(core, monkeypatch)
    key = "voice_reply:" + message_id
    if change.startswith("binding_") or change == "missing_binding":
        value = json.loads(core.store.one('SELECT value FROM runtime_meta WHERE key=%s', (key,))[
            "value"])
        if change == "missing_binding":
            value.pop("binding")
        elif change == "binding_hash":
            value["binding_sha256"] = "0" * 64
        else:
            field = "untrusted_extra" if change == "binding_extra" else "reply_sha256"
            value["binding"][field] = "0" * 64
            value["binding_sha256"] = digest(value["binding"])
        core.store.execute('UPDATE runtime_meta SET value=%s WHERE key=%s', (encode(value), key))
    elif change == "content":
        core.store.execute('UPDATE outbox SET content_json=%s', (encode({"text": "changed"}),))
    elif change == "source_erasure":
        with core.store.transaction():
            erase_voice_jobs(core.store, [event_id])
    elif change == "privacy_epoch":
        core.store.execute(
            'INSERT INTO runtime_meta (key,value) VALUES '
            "('privacy_epoch','1') ON CONFLICT(key) DO UPDATE SET "
            'value=excluded.value,record_sequence=DEFAULT'
        )
    elif change == "reply_revision":
        core.store.execute("UPDATE conversations SET reply_revision=reply_revision+1")
    elif change == "expired_result":
        core.clock.advance(301)
    else:
        core.store.execute("UPDATE voice_jobs SET state='UNKNOWN',result_blob=NULL")
    assert claim(jobs)[1].lease is None
    row = core.store.one("SELECT * FROM voice_jobs")
    assert row["state"] in {"CANCELLED", "EXPIRED", "UNKNOWN"}
    assert row["result_blob"] is None
    assert jobs.result(row["job_id"]) is None
    assert core.store.one("SELECT COUNT(*) n FROM voice_jobs")["n"] == 1
    assert core.store.rows("SELECT state FROM model_usage") == [{"state": "SETTLED"}]
    assert core.model.calls == []


def test_retained_audio_still_requires_current_nonempty_delivery_lease(core, monkeypatch):
    _, jobs, message_id, _, first = safe_retry(core, monkeypatch)
    job = core.store.one("SELECT * FROM voice_jobs")
    for token in (None, "", first["lease_token"]):
        with core.store.transaction(), pytest.raises(CoreError):
            jobs.delivery_audio(job["job_id"], message_id, token)
    assert core.store.one("SELECT * FROM voice_jobs") == job
    core.clock.advance(6)
    second = leased(core, message_id)
    with core.store.transaction(), pytest.raises(CoreError, match="VOICE_OUTBOX_SOURCE_REQUIRED"):
        jobs.delivery_audio(job["job_id"], message_id, first["lease_token"])
    with core.store.transaction():
        audio = jobs.delivery_audio(job["job_id"], message_id, second["lease_token"])
    assert audio.model_dump(mode="json") == first["voice"]["audio"]


def test_failure_after_begin_send_stays_uncertain_without_audio_retry(core, monkeypatch):
    _, jobs, message_id, _, _ = safe_retry(core, monkeypatch)
    core.clock.advance(6)
    item = leased(core, message_id)
    transport.begin_send(core.store, message_id, item["lease_token"], core.clock(),
                         core.settings, clock=core.clock)
    transport.delivery_result(core.store, core.settings, message_id, DeliveryResult(
        lease_token=item["lease_token"], result="RETRYABLE_FAILURE",
        error_code="SYNTHETIC_AFTER_SEND"), core.clock())
    assert claim(jobs)[1].lease is None
    core.clock.advance(6)
    authority = voice_authority.current(core.store, core.settings, core.clock).authority.as_dict()
    assert transport.lease_deliveries(core.store, core.settings,
        LeaseRequest(worker_id="uncertain-retry-test", authority=authority),
        core.clock())["items"] == []
    assert core.store.one("SELECT state,provider_message_id FROM outbox") == {
        "state": "DELIVERY_UNCERTAIN", "provider_message_id": None,
    }
    assert core.store.one("SELECT state,result_blob FROM voice_jobs") == {
        "state": "CANCELLED", "result_blob": None,
    }
    receipt = core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                             ("voice_reply:" + message_id,))
    assert json.loads(receipt["value"])["state"] != "TEXT"
    assert core.store.one("SELECT COUNT(*) n FROM voice_jobs")["n"] == 1
    assert core.store.rows("SELECT state FROM model_usage") == [{"state": "SETTLED"}]
    assert core.model.calls == []


def test_real_http_retry_preserves_independent_node_result_and_paid_usage(
    core, tmp_path, monkeypatch, node,
):
    config, signing_key, counter = node
    config = config.model_copy(update={
        "node_id": "local", "request_timeout_seconds": 10.0,
        "heartbeat_seconds": 0.5, "execution_timeout_seconds": 20.0,
    })
    manifest_sha256 = digest(local_manifest(config))
    core_voice = voice_config(
        signing_private_key=signing_key.private_bytes_raw().hex(),
        nodes={"local": VoiceNode(token=config.credential, manifest_sha256=manifest_sha256)},
        reply=VoiceReplyPolicy(mode="auto"),
    )
    from tests.voice_owner_fixtures import native_voice

    capture, _, _ = native_voice(core, tmp_path, record=False, config=core_voice)
    calls = []

    def model(request):
        calls.append(json.loads(request.content)["messages"])
        return httpx.Response(200, json={
            "choices": [{"message": {"content": TEXT}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    async def scenario():
        import ssl

        async with tls_core(core, tmp_path) as (origin, certificate), httpx.AsyncClient(
            base_url=origin, verify=ssl.create_default_context(cafile=str(certificate)),
            trust_env=False, headers={"Authorization": "Bearer "
                + core.settings.bridge_token.get_secret_value()}) as client, \
                httpx.AsyncClient(transport=httpx.MockTransport(model)) as model_http:
            with Core(core.settings, ModelClient(core.settings, model_http), core.clock
                      ) as worker:
                assert worker.store is not core.store
                assert await worker.run_once("safe-retry-worker")
            assert len(calls) == 1
            usage = core.store.rows("SELECT * FROM model_usage")
            charges = core.store.rows("SELECT * FROM cloud_cost_snapshots")
            assert len(usage) == 1 and usage[0]["state"] == "SETTLED"
            lease_path = "/internal/v1/deliveries/lease"
            waiting = await client.post(lease_path, json={
                "worker_id": "retry-bridge", "authority": capture.authority})
            assert waiting.status_code == 200 and waiting.json()["items"] == []
            await independent_node(config.model_copy(update={"core_origin": origin}),
                certificate, counter, tmp_path, core.clock.now.timestamp())
            leased_reply = await client.post(lease_path, json={
                "worker_id": "retry-bridge", "authority": capture.authority})
            assert leased_reply.status_code == 200, leased_reply.text
            first = leased_reply.json()["items"][0]
            original = core.store.one("SELECT * FROM voice_jobs")
            delivery_path = f"/internal/v1/deliveries/{first['message_id']}"
            failed = await client.post(delivery_path + "/result", json={
                "lease_token": first["lease_token"], "result": "RETRYABLE_FAILURE",
                "error_code": "SYNTHETIC_BEFORE_SEND",
            })
            assert failed.status_code == 200 and failed.json()["state"] == "RETRY_WAIT"
            session = (await client.get("/internal/v2/voice-node/session", headers={
                "Authorization": "Bearer " + config.credential.get_secret_value()})).json()
            sequence = session["last_sequence"] + 1
            request_id = f"voice-claim:{session['incarnation']}:{sequence}"
            idle = await client.post(
                "/internal/v2/voice-node/claim",
                json={
                    "node_id": "local",
                    "incarnation": session["incarnation"],
                    "sequence": sequence,
                    "manifest_sha256": manifest_sha256,
                    "resource_state": "READY",
                },
                headers={
                    "Authorization": "Bearer " + config.credential.get_secret_value(),
                    "Idempotency-Key": request_id,
                },
            )
            assert idle.status_code == 200 and idle.json()["lease"] is None, idle.text
            assert core.store.one("SELECT * FROM voice_jobs") == original
            core.clock.advance(6)
            again = await client.post(lease_path, json={
                "worker_id": "retry-bridge", "authority": capture.authority})
            assert again.status_code == 200, again.text
            second = again.json()["items"][0]
            assert second["lease_token"] != first["lease_token"]
            assert second["voice"] == first["voice"]
            stale = await client.post(delivery_path + "/begin-send", json={
                "lease_token": first["lease_token"],
            })
            assert stale.status_code == 409
            assert core.store.one(
                "SELECT state,attempt_count,provider_message_id FROM outbox") == {
                "state": "LEASED", "attempt_count": 1, "provider_message_id": None,
            }
            assert core.store.one("SELECT COUNT(*) n FROM voice_jobs")["n"] == 1
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert core.store.rows("SELECT * FROM cloud_cost_snapshots") == charges
            assert len(child_receipts(counter)) == 1 and len(calls) == 1

    run(scenario())
