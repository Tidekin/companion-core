"""Native HTTP and original worker share revisions; provider transport is synthetic."""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import __main__, live_text, owner_authority, paid_budget
from companion.core import Core
from companion.store import encode
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.rich_input import input_digest
from tests.conftest import run
from tests.support.native_consumer_entry import environment
from tests.support.onebot_events import raw_event


async def admit(client, config, plan, number, text):
    now = datetime.now(UTC)
    raw = raw_event(number, time=int(now.timestamp()), text=text)
    event = normalize(json.dumps(raw), config, now=now.timestamp())
    capture = {
        "kind": "authenticated_onebot_ws", "window_sha256": plan.sha256,
        "event_id": event["event_id"], "source_event_id": event["source_event_id"],
        "input_sha256": input_digest(event["payload"]),
        "frame_sha256": hashlib.sha256(encode(raw).encode()).hexdigest(),
        "evidence_sha256": "e" * 64, "identity_sha256": plan.value["identity_sha256"],
        "config_sha256": plan.value["config_sha256"], "protocol_epoch": config.protocol_epoch,
        "received_at": now.isoformat(), "occurred_at": event["occurred_at"],
    }
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(config.capture_signing_key))
    capture["signature"] = key.sign(
        live_text.CAPTURE_SIGNATURE_DOMAIN + encode(capture).encode(),
    ).hex()
    approved = await client.post("/internal/v1/live-text/inputs/approve", json={
        "window_sha256": plan.sha256, "event_id": event["event_id"],
        "input_sha256": capture["input_sha256"], "evidence_sha256": "f" * 64,
        "capture": capture,
    })
    assert approved.status_code == 200, approved.text
    result = await client.post("/internal/v1/inbox/events", json=event,
                               headers={"X-Idempotency-Key": event["event_id"]})
    assert result.status_code == 202, result.text
    return event, result.json()


def test_native_api_accepts_burst_while_independent_worker_drains_old_model(
    core, tmp_path, monkeypatch,
):
    calls = []

    async def scenario():
        entered, release, replied = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def provider(request):
            calls.append(json.loads(request.content))
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop",
                             "message": {"content": "最新问题的合成回复"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            })

        monkeypatch.setattr(__main__, "httpx", SimpleNamespace(AsyncClient=lambda **kwargs:
            httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(provider))))
        completed = __main__.Recorder.completed

        def observed(recorder, **kwargs):
            completed(recorder, **kwargs)
            if recorder.store.one("SELECT 1 FROM outbox WHERE state='PENDING'"):
                replied.set()

        monkeypatch.setattr(__main__.Recorder, "completed", observed)
        async with environment(core, tmp_path) as (entry, sends):
            client = entry.client
            first = await entry.admit(1, "先记下青竹项目")
            first_result = entry.accepted[first["event_id"]]
            worker = asyncio.create_task(__main__.worker(core.settings))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                started = time.monotonic()
                second = await entry.admit(2, "补充地点在城西")
                third = await entry.admit(3, "请把前两条一起回答")
                second_result = entry.accepted[second["event_id"]]
                third_result = entry.accepted[third["event_id"]]
                assert not release.is_set() and not replied.is_set()
                assert time.monotonic() - started < 3  # Local acceptance, not QQ latency.
                duplicate = await client.post("/internal/v1/inbox/events", json=third,
                    headers={"X-Idempotency-Key": third["event_id"]})
                assert duplicate.status_code == 202, duplicate.text
                assert duplicate.json()["duplicate"] and duplicate.json()["reply_revision"] == 3
                assert [item["reply_revision"] for item in (
                    first_result, second_result, third_result,
                )] == [1, 2, 3]
                assert [row["state"] for row in core.store.rows(
                    "SELECT state FROM jobs ORDER BY rowid",
                )] == ["CANCELLED", "CANCELLED", "QUEUED"]
                assert core.store.one("SELECT state FROM model_usage") == {"state": "RESERVED"}
                with Core(core.settings) as second_worker:
                    assert second_worker.store is not core.store
                    assert second_worker.claim_job("concurrent-worker") is None
                    assert second_worker.store.rows("SELECT state FROM model_usage") == [
                        {"state": "RESERVED"}]
                    assert second_worker.store.one("SELECT state FROM jobs WHERE job_id=?",
                        (third_result["job_id"],)) == {"state": "QUEUED"}
                release.set()
                await asyncio.wait_for(replied.wait(), 5)
                assert not worker.done(), worker.exception()
                assert len(calls) == 2
                prompt = encode(calls[-1]["messages"])
                assert all(text in prompt for text in ("青竹项目", "地点在城西", "前两条一起回答"))
                assert core.store.rows("SELECT origin_event_id FROM outbox") == [
                    {"origin_event_id": third["event_id"]},
                ]
                assert core.store.rows("SELECT state FROM model_usage ORDER BY rowid") == [
                    {"state": "SETTLED"}, {"state": "SETTLED"},
                ]
                assert {row["event_id"] for row in core.store.rows(
                    "SELECT event_id FROM job_sources WHERE job_id=?", (third_result["job_id"],),
                )} == {first["event_id"], second["event_id"], third["event_id"]}
                assert owner_authority.current(core.store, core.settings, core.clock
                    ).authority.as_dict() == entry.runtime.authority
                assert paid_budget.journal(core.store, core.settings.paid_model)["head_sha256"]
                assert (await entry.send(client, entry.protocol))["worked"]
                assert len(sends) == 1
                assert sends[0]["message"][0]["data"]["text"] == "最新问题的合成回复"
            finally:
                release.set()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    run(scenario())
