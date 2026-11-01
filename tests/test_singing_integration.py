"""Actual SDK deferral, native Core HTTP custody, and ordered singing deliveries."""

import copy
import json
from contextlib import asynccontextmanager
from datetime import datetime

import pytest

from companion import owner_delivery
from companion.contracts import CoreError
from companion.core import Core
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.voice_config import VoiceNode
from companion.voice_jobs import erase_voice_jobs
from companion.voice_protocol import (
    ROUTE_PREFIX,
    SingingResult,
    VoiceLeaseRef,
    canonical_bytes,
    make_voice_result,
)
from companion_onebot_bridge.voice_media import prepare_delivery
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, call, worker_for
from tests.test_owner_authority import activate
from tests.test_reading_followup import URL, admit, window
from tests.test_singing_protocol import result as song_result
from tests.test_voice_jobs import TOKEN, voice_config


@asynccontextmanager
async def singing_window(core, tmp_path, monkeypatch, *, manifest_sha256="b" * 64):
    def unexpected_failure(self, *args):
        raise

    monkeypatch.setattr(Core, "fail_job", unexpected_failure)
    core.settings.voice = voice_config(nodes={"local": VoiceNode(
        token=TOKEN, manifest_sha256=manifest_sha256, capabilities=("tts", "asr", "sing"))},
        singing={"source_adapter_id": "bili"}, job_ttl_seconds=600,
        result_ttl_seconds=300, max_private_blob_bytes=80 * 1024 * 1024)
    async with window(core, tmp_path, monkeypatch) as (client, config, plan):
        core.settings.exploration_daily_bytes = 80 * 1024 * 1024
        plan.policy = OwnerGrantPolicy.model_validate({**plan.policy.model_dump(),
            "purposes": [*plan.policy.purposes, "voice"],
            "capability_policies": {"voice": core.settings.voice.authority_sha256()}})
        active, _ = activate(core, plan.policy, revision=1, operation="enable-native-test-singing")
        plan.authority = active["authority"]
        yield client, config, plan


async def queue_song(core, client, config, plan):
    event, admitted = await admit(client, core, config, plan, 1, "想听你唱首歌", card=URL)
    provider = Provider(answer(calls=[call("catalog", "reading_catalog", {})]),
        answer(calls=[call("sing", "sing_song", {"source_event_id": event["event_id"]})]))
    async with worker_for(core, provider) as worker:
        assert await worker.run_once("choose-singing", job_id=admitted["job_id"])
    job = core.store.one("SELECT * FROM voice_jobs")
    assert job is not None, core.store.rows("SELECT state,last_error_code FROM jobs")
    assert job["operation"] == "sing" and job["state"] == "QUEUED"
    assert job["reserved_output_bytes"] == 56 * 1024 * 1024
    assert core.store.one("SELECT state FROM jobs")["state"] == "RETRY_WAIT"
    assert len(provider.requests) == 2 and not core.store.rows("SELECT * FROM outbox")
    return event, admitted, job


async def finish_song(core, client, job):
    headers = {"Authorization": "Bearer " + TOKEN}
    session = (await client.get(ROUTE_PREFIX + "/session", headers=headers)).json()
    body = {"node_id": "local", "incarnation": session["incarnation"],
        "sequence": session["last_sequence"] + 1, "resource_state": "READY",
        "manifest_sha256": "b" * 64}
    response = await client.post(ROUTE_PREFIX + "/claim", json=body, headers=headers | {
        "Idempotency-Key": f"voice-claim:{body['incarnation']}:{body['sequence']}"})
    assert response.status_code == 200, response.text
    from companion.voice_protocol import VoiceClaimResponse

    lease = VoiceClaimResponse.model_validate(response.json()).lease
    ref = VoiceLeaseRef.from_lease(lease)
    response = await client.post(ROUTE_PREFIX + f"/jobs/{job['job_id']}/input",
        content=canonical_bytes(ref), headers=headers | {"Content-Type": "application/json"})
    assert response.status_code == 200, response.text
    recipe = response.json()
    value = song_result(65).model_dump(mode="json")
    value["manifest"]["node_manifest_sha256"] = "b" * 64
    value["manifest"]["source"].update(bvid=recipe["bvid"], part=recipe["part"])
    result = make_voice_result(ref, SingingResult.model_validate(value))
    assert len(canonical_bytes(result)) > 5_500_000
    response = await client.put(ROUTE_PREFIX + f"/jobs/{job['job_id']}/result",
        content=canonical_bytes(result), headers=headers | {"Content-Type": "application/json",
            "Idempotency-Key": f"voice-result:{job['job_id']}:1"})
    assert response.status_code == 200, response.text
    return result


async def select_song(core, admitted, song_id):
    core.clock.advance(2)
    provider = Provider(answer(calls=[call("final-song", "reply_with_song",
        {"text": "给你唱一首。", "request_id": song_id})]))
    async with worker_for(core, provider) as worker:
        assert await worker.run_once("send-singing", job_id=admitted["job_id"])
    assert core.store.one("SELECT state,last_error_code FROM jobs")["state"] == "SUCCEEDED"
    assert len(provider.requests) == 1
    return core.store.rows("SELECT * FROM outbox ORDER BY segment_index")


def test_sdk_song_tool_resumes_into_bound_ordered_bubbles(core, tmp_path, monkeypatch):
    async def scenario():
        async with singing_window(core, tmp_path, monkeypatch) as (client, config, plan):
            event, admitted, job = await queue_song(core, client, config, plan)
            original = core.store.rows("SELECT * FROM model_usage")
            async with worker_for(core, Provider()) as worker:
                assert await worker.run_once("pending-song", job_id=admitted["job_id"]) is False
            assert core.store.rows("SELECT * FROM model_usage") == original
            result = await finish_song(core, client, job)
            rows = await select_song(core, admitted, job["job_id"])
            assert len(rows) == len(result.payload.audio) == 2
            assert all(row["reply_group_id"] == admitted["job_id"] for row in rows)
            metered = core.store.one("SELECT source_used_requests,source_used_bytes,"
                "reserved_output_bytes FROM voice_jobs")
            assert metered == {"source_used_requests": 4, "source_used_bytes": 2000,
                               "reserved_output_bytes": 0}
            leased = await client.post("/internal/v1/deliveries/lease", json={
                "worker_id": "singing-bridge", "authority": plan.authority})
            assert leased.status_code == 200, leased.text
            item = leased.json()["items"][0]
            assert item["message_id"] == rows[0]["message_id"]
            prepared = prepare_delivery(item, config,
                authority=plan.authority,
                voice_policy_sha256=core.settings.voice.authority_sha256(),
                now=datetime.fromisoformat(core.clock()))
            assert prepared.binding_sha256 == item["voice"]["binding_sha256"]
            wrong = copy.deepcopy(item)
            wrong["voice"]["binding"]["singing_segment_index"] = 1
            with pytest.raises(ValueError):
                prepare_delivery(wrong, config, authority=plan.authority,
                    voice_policy_sha256=core.settings.voice.authority_sha256(),
                    now=datetime.fromisoformat(core.clock()))
            with core.store.transaction():
                core.voice.validate_send(item["message_id"], item["lease_token"],
                                          json.loads(prepared.check_json))
            assert item["voice"]["audio"] == result.payload.audio[0].model_dump(mode="json")
            assert event["event_id"] == job["event_id"]
    run(scenario())


@pytest.mark.parametrize("change", ["erasure", "expiry", "source", "tool", "receipt_text"])
def test_singing_source_fences_every_unsent_part(core, tmp_path, monkeypatch, change):
    async def scenario():
        async with singing_window(core, tmp_path, monkeypatch) as (client, config, plan):
            event, admitted, job = await queue_song(core, client, config, plan)
            await finish_song(core, client, job)
            rows = await select_song(core, admitted, job["job_id"])
            usage = core.store.rows("SELECT * FROM model_usage")
            if change == "erasure":
                with core.store.transaction():
                    erase_voice_jobs(core.store, [event["event_id"]])
            elif change == "expiry":
                core.clock.advance(301)
            elif change == "source":
                core.store.execute("UPDATE content_items SET target_url=?", (URL + "changed",))
            elif change == "receipt_text":
                core.store.execute("UPDATE runtime_meta SET value=? WHERE key=?",
                    (json.dumps({"state": "TEXT", "job_id": job["job_id"]}),
                     "voice_reply:" + rows[0]["message_id"]))
            else:
                core.store.execute("UPDATE agent_tool_calls SET args_sha256=? "
                                   "WHERE tool_name='sing_song'", ("f" * 64,))
            with pytest.raises(CoreError):
                with core.store.transaction():
                    if change == "receipt_text":
                        core.voice.prepare_reply(rows[0]["message_id"])
                    else:
                        owner_delivery.check_delivery(core.store, core.settings, core.clock,
                                                       rows[0]["message_id"])
            leased = await client.post("/internal/v1/deliveries/lease", json={
                "worker_id": "fenced-singing-bridge", "authority": plan.authority})
            assert leased.status_code == 200, leased.text
            assert leased.json()["items"] == []
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert len(core.store.rows("SELECT * FROM voice_jobs")) == 1
    run(scenario())
