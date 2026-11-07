"""Authenticated real HTTP, independent worker and native budget/Outbox poke integration."""

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import paid_budget
from companion.cloud_budget import CloudBudgetConfig
from companion.core import Core
from companion.model import ModelClient
from companion.poke_protocol import PokePolicy, canonical_notice, canonical_source, signed_payload
from companion.retrieval import SemanticIndex
from companion.store import encode
from tests.conftest import run
from tests.poke_authority_fixture import environment as native_environment
from tests.poke_authority_fixture import refresh
from tests.support.core_http import real_core_http
from tests.support.poke import native_frame


@pytest.fixture(autouse=True)
def no_embedding_download(monkeypatch):
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)


def capture(core, cfg, plan, *, second=0):
    now = core.clock.now
    frame = native_frame(int(now.timestamp()) + second)
    notice = canonical_notice(frame, owner_qq_id=cfg.owner_qq_id, bot_qq_id=cfg.bot_qq_id)
    identity = canonical_source(notice)
    value = {
        "kind": "authenticated_onebot_poke", "authority": plan.authority.as_dict(),
        "event_id": identity["event_id"], "source_event_id": identity["source_event_id"],
        "notice": notice, "protocol_epoch": cfg.protocol_epoch,
        "identity_sha256": plan.policy.capture.identity_sha256,
        "config_sha256": "c" * 64,
        "frame_sha256": hashlib.sha256(encode(frame).encode()).hexdigest(),
        "evidence_sha256": "e" * 64,
        "occurred_at": datetime.fromtimestamp(frame["time"], UTC).isoformat(),
        "received_at": now.isoformat(),
    }
    value["signature"] = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(cfg.capture_signing_key)).sign(signed_payload(value)).hex()
    return value


@asynccontextmanager
async def environment(core, tmp_path, model, *, daily_budget=20_000_000):
    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    core.settings.cloud_budget = CloudBudgetConfig(daily_micro_cny=daily_budget)
    env = native_environment(core, tmp_path)
    core.settings.poke_policy = PokePolicy(enabled=True, allow_native=True, coalesce_seconds=0)
    refresh(env)
    async with real_core_http(core) as port:
        cfg = replace(env.config, core_url=f"http://127.0.0.1:{port}",
                      capture_signing_key=env.key.private_bytes_raw().hex(),
                      owner_authority=env.approval.authority.as_dict())
        async with httpx.AsyncClient(base_url=cfg.core_url, trust_env=False,
            headers={"Authorization": "Bearer " + cfg.bridge_token}) as client, (
            httpx.AsyncClient(transport=httpx.MockTransport(model))
        ) as model_http:
            with Core(core.settings, ModelClient(core.settings, model_http), core.clock) as worker:
                assert worker.store is not core.store
                yield client, worker, cfg, env.approval


async def lease_and_begin(client, authority):
    leased = await client.post("/internal/v1/deliveries/lease",
                               json={"worker_id": "synthetic-bridge", "limit": 1,
                                     "authority": authority})
    assert leased.status_code == 200, leased.text
    item = leased.json()["items"][0]
    begun = await client.post("/internal/v1/deliveries/" + item["message_id"] + "/begin-send",
                              json={"lease_token": item["lease_token"]})
    assert begun.status_code == 200, begun.text
    return item


@pytest.mark.parametrize("kind", ["poke", "text", "none"])
def test_signed_poke_uses_real_http_worker_budget_and_original_outbox(core, tmp_path, kind):
    calls = []

    def model(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["response_format"] == {"type": "json_object"}
        choice = {"kind": kind, **({"text": "怎么啦？"} if kind == "text" else {})}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": encode(choice)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    async def scenario():
        async with environment(core, tmp_path, model) as (client, worker, cfg, plan):
            body = capture(core, cfg, plan)
            accepted = await client.post("/internal/v1/pokes", json=body)
            assert accepted.status_code == 200, accepted.text
            job_id = accepted.json()["job_id"]
            assert accepted.json()["state"] == "QUEUED"
            assert await worker.run_once("independent-poke")
            job = core.store.one((
                'SELECT state,last_error_code FROM jobs WHERE job_id=%s'
            ), (job_id,))
            assert job == {"state": "SUCCEEDED", "last_error_code": None}, job
            assert not await worker.run_once("poke-not-repeated")
            assert core.store.one("SELECT reply_revision FROM conversations")["reply_revision"] == 0
            event = core.store.one("SELECT event_type,body_json,memory_processed FROM events")
            assert event["event_type"] == "notice.poke" and event["memory_processed"] == 1
            assert "text" not in json.loads(event["body_json"])
            assert core.store.rows("SELECT scenario,state FROM model_usage") == [
                {"scenario": "qq_poke", "state": "SETTLED"}]
            assert len(core.store.rows("SELECT * FROM cloud_cost_snapshots")) == 1
            if kind == "none":
                assert not core.store.rows("SELECT * FROM outbox")
            else:
                item = await lease_and_begin(client, cfg.owner_authority)
                assert item["origin_event_id"] == body["event_id"]
                assert "reply_revision" not in item
                check = {"authority": plan.authority.as_dict(), "event_id": body["event_id"],
                         "message_id": item["message_id"], "lease_token": item["lease_token"]}
                check["content_sha256"] = paid_budget.sha(item["content"])
                validated = await client.post("/internal/v1/owner-authority/send-check", json=check)
                assert validated.status_code == 200, validated.text
                receipt = {"lease_token": item["lease_token"], "result": "SENT"}
                if kind == "poke":
                    receipt["native_ack"] = {"kind": "onebot_action", "action": "friend_poke",
                        "status": "ok", "retcode": 0, "response_sha256": paid_budget.sha(
                            {"status": "ok", "retcode": 0, "data": None})}
                else:
                    receipt["provider_message_id"] = "12345"
                path = "/internal/v1/deliveries/" + item["message_id"] + "/result"
                result = await client.post(path, json=receipt)
                assert result.status_code == 200, result.text
                assert result.json() == {"state": "SENT"}
                assert (await client.post(path, json=receipt)).json() == {"state": "SENT"}
                saved = core.store.one("SELECT * FROM outbox")
                assert saved["provider_message_id"] == (None if kind == "poke" else "12345")
                assert bool(saved["native_ack_json"]) is (kind == "poke")
            repeated = await client.post("/internal/v1/pokes", json=body)
            assert repeated.status_code == 200 and repeated.json()["duplicate"]
            assert not await worker.run_once("receipt-replay")
            assert len(calls) == 1

    run(scenario())


@pytest.mark.parametrize("failure", ["provider_unknown", "daily_budget"])
def test_poke_cannot_repeat_uncertain_or_unfunded_model_call(core, tmp_path, failure):
    calls = []

    def model(request):
        calls.append(request)
        return httpx.Response(503)

    async def scenario():
        async with environment(core, tmp_path, model,
            daily_budget=0 if failure == "daily_budget" else 20_000_000,
        ) as (client, worker, cfg, plan):
            accepted = await client.post("/internal/v1/pokes",
                                         json=capture(core, cfg, plan))
            assert accepted.status_code == 200, accepted.text
            assert await worker.run_once("one-attempt")
            assert core.store.one("SELECT state FROM jobs")["state"] == "FAILED"
            assert not await worker.run_once("no-paid-retry")
            assert not core.store.rows("SELECT * FROM outbox")
            if failure == "daily_budget":
                assert not calls and not core.store.rows("SELECT * FROM model_usage")
            else:
                assert len(calls) == 1
                assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"

    run(scenario())
