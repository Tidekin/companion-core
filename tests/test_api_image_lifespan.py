"""API lifespan stages originals; the independent native worker owns SDK inference."""

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import SecretStr

from companion import __main__, api, owner_authority, paid_budget
from companion import core as core_module
from companion.contracts import CoreError
from companion.core import Core
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from companion.store import encode
from companion_delivery_protocol.authority import (
    capture_signing_payload,
    image_upload_signing_payload,
)
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.rich_input import input_digest
from tests import test_onebot_window as window_fixtures
from tests.conftest import run
from tests.native_continuous_support import until
from tests.support.failure_notice import assert_failure_notice
from tests.support.onebot_events import config as bridge_config
from tests.support.onebot_events import raw_event
from tests.support.owner_authority import activate
from tests.test_live_image_input import options, png
from tests.test_memory_control import grant
from tests.test_onebot_capture import pinned_plan
from tests.test_onebot_images import image_segment
from tests.test_paid_model_gate import approved_live


def image_settings(core, tmp_path, monkeypatch):
    now = datetime.now(UTC)
    # The actual API and worker use their production clock. Shift only the
    # synthetic plan fixture into this test's current interval.
    monkeypatch.setattr(window_fixtures, "raw_event", lambda: raw_event(time=int(now.timestamp())))
    scope = options()
    scope["paid_options"].update(
        starts_at=(now - timedelta(hours=1)).isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
        rates_valid_until=(now + timedelta(hours=1)).isoformat(),
    )
    config, plan = pinned_plan(
        core, tmp_path, 3001, live=True, approved=True,
        rich_input_scope="references_v1", scope_options=scope,
    )
    return config, plan


def model_clients(monkeypatch, settings, respond):
    clients = []

    def factory(**kwargs):
        assert kwargs == {"timeout": settings.timeout_seconds, "trust_env": False}
        client = httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(respond))
        clients.append(client)
        return client

    # Patch only the API's outbound client factory. ModelClient, Core, app state,
    # HTTP routes, budget reservations and lifespan remain real.
    monkeypatch.setattr(api, "AsyncClient", factory, raising=False)
    return clients


def native_image_settings(core, tmp_path):
    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    core.settings.control_token = SecretStr("synthetic-native-image-control")
    scope = options()
    for name, value in scope["core_options"].items():
        setattr(core.settings, name, value)
    core.settings.rich_input_scope = "references_v1"
    approved_live(core, tmp_path, **scope["paid_options"])
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    key = Ed25519PrivateKey.generate()
    config = replace(bridge_config(), capture_signing_key=key.private_bytes_raw().hex(),
        protocol_epoch="native-image-api",
        bridge_token=core.settings.bridge_token.get_secret_value(),
        rich_input_scope="references_v1")
    policy = OwnerGrantPolicy(owner_qq_id=config.owner_qq_id,
        purposes=["chat", "image", "content"], authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capability_policies={"image": owner_authority.capability_hashes(core.settings)["image"]},
        capture={"public_key": config.capture_public_key,
            "protocol_epoch": config.protocol_epoch,
            "identity_sha256": paid_budget.sha([config.owner_qq_id, config.bot_qq_id])})
    authority = activate(core, policy)[0]["authority"]
    return replace(config, owner_authority=authority), key


def approve_image(client, config, key, now):
    raw = raw_event(time=int(now.timestamp()), message=[
        {"type": "text", "data": {"text": "请解释这张图"}}, image_segment(),
    ])
    event = normalize(json.dumps(raw), config, now=now.timestamp(),
                      rich_input_scope="references_v1", image_scope="owner_image_v1")
    receipt = {"kind": "authenticated_onebot_ws", "authority": config.owner_authority,
        "event_id": event["event_id"], "source_event_id": event["source_event_id"],
        "input_sha256": input_digest(event["payload"]),
        "frame_sha256": hashlib.sha256(encode(raw).encode()).hexdigest(),
        "evidence_sha256": "e" * 64,
        "identity_sha256": paid_budget.sha([config.owner_qq_id, config.bot_qq_id]),
        "protocol_epoch": config.protocol_epoch,
        "received_at": now.isoformat(), "occurred_at": event["occurred_at"]}
    receipt["signature"] = key.sign(capture_signing_payload(receipt)).hex()
    response = client.post("/internal/v1/owner-authority/inputs/approve", json=receipt)
    assert response.status_code == 200, response.text
    response = client.post("/internal/v1/inbox/events", json=event,
                           headers={"X-Idempotency-Key": event["event_id"]})
    assert response.status_code == 202, response.text
    return event, response.json()["job_id"]


def claim_image(client, config, key, event):
    prefix = "/internal/v2/content/" + event["event_id"] + "/image"
    source = event["payload"]["rich"]["segments"][0]["source_sha256"]
    claim = client.post(prefix + "/claim", json={
        "request_key": "image:0", "source_sha256": source})
    assert claim.status_code == 200, claim.text
    data = png()
    proof = {"authority": config.owner_authority, "event_id": event["event_id"], "slot": 0,
        "source_sha256": source, "lease_token": claim.json()["lease_token"],
        "media_sha256": hashlib.sha256(data).hexdigest(), "mime": "image/png"}
    proof["signature"] = key.sign(image_upload_signing_payload(proof)).hex()
    headers = {"Content-Type": "image/png", "X-Content-Key": "image:0",
        "X-Content-Lease": proof["lease_token"], "X-Content-Proof": encode(proof)}
    return prefix, data, headers


def native_instances(core, monkeypatch):
    instances = []

    def construct(*args, **kwargs):
        instance = Core(*args, clock=core.clock, **kwargs)
        instances.append(instance)
        return instance

    monkeypatch.setattr(api, "Core", construct)
    monkeypatch.setattr(core_module, "Core", construct)
    return instances


def native_worker_transport(monkeypatch, provider, *, before=None):
    monkeypatch.setattr(__main__, "httpx", SimpleNamespace(AsyncClient=lambda **kwargs:
        httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(provider),
                          **({"event_hooks": {"request": [before]}} if before else {}))))


def test_api_lifespan_image_upload_calls_model_and_worker_continues_same_budget(
    core, tmp_path, monkeypatch,
):
    config, key = native_image_settings(core, tmp_path)
    instances = native_instances(core, monkeypatch)
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "图中有蓝色区域。"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    clients = model_clients(monkeypatch, core.settings,
                            lambda _: pytest.fail("API staging cannot call the model"))
    native_worker_transport(monkeypatch, provider)
    app = api.create_app(core.settings)
    with TestClient(app, headers={"Authorization": "Bearer " + config.bridge_token}) as client:
        assert app.state.core is not core
        event, job_id = approve_image(client, config, key, core.clock.now)
        prefix, data, headers = claim_image(client, config, key, event)
        uploaded = client.post(prefix, content=data, headers=headers)
        assert uploaded.status_code == 200 and uploaded.json()["input_ready"], uploaded.text
        assert clients == [] and app.state.core.model is None
        assert calls == [] and core.store.rows("SELECT * FROM model_usage") == []
        receipt = core.store.one("SELECT * FROM captured_images")
        replay = client.post(prefix, content=data, headers=headers)
        assert replay.status_code == 200 and replay.json()["duplicate"]
        assert core.store.one("SELECT * FROM captured_images") == receipt

        async def scenario():
            task = asyncio.create_task(__main__.worker(core.settings))
            try:
                await until(lambda: core.store.one("SELECT 1 FROM outbox WHERE state='PENDING'"),
                            tasks=[task], deadline_seconds=20)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        run(scenario())
        assert len(calls) == 1 and len(instances) == 2
        assert instances[0].store is not instances[1].store
        assert any(part["type"] == "image_url" for message in calls[0]["messages"]
                   if isinstance(message["content"], list) for part in message["content"])
        assert core.store.rows("SELECT scenario,state FROM model_usage") == [
            {"scenario": "chat", "state": "SETTLED"}]
        assert {row["logical_request_id"] for row in core.store.rows(
            "SELECT logical_request_id FROM model_admissions")} == {job_id}
        assert paid_budget.journal(core.store, core.settings.paid_model)["head_sha256"]
    assert clients == []
    for instance in instances:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            instance.store.one("SELECT 1")


@pytest.mark.parametrize("outcome", [
    "settled", "unknown", "stopped", "admission_expired", "image_lease_expired",
])
def test_worker_loop_waits_for_api_vision_and_preserves_unknown_block(
    core, tmp_path, monkeypatch, outcome,
):
    if outcome == "admission_expired":
        core.settings.lease_seconds = core.settings.model_timeout("chat") + 60
    config, key = native_image_settings(core, tmp_path)
    instances = native_instances(core, monkeypatch)
    calls, scans, prepared_requests = [], [], []
    original_completed = __main__.Recorder.completed

    def completed(recorder, **kwargs):
        original_completed(recorder, **kwargs)
        scans.append(kwargs)

    monkeypatch.setattr(__main__.Recorder, "completed", completed)

    def provider(request):
        calls.append(json.loads(request.content))
        admission = core.store.one("SELECT * FROM model_admissions")
        assert admission["io_started_at"]
        result = {"choices": [{"message": {"content": "图中有蓝色区域。"}}]}
        if outcome != "unknown":
            result["usage"] = {"prompt_tokens": 100, "completion_tokens": 20}
        return httpx.Response(200, json=result)

    clients = model_clients(monkeypatch, core.settings,
                            lambda _: pytest.fail("API cannot prepay image inference"))
    async def expire_admission(request):
        prepared_requests.append(json.loads(request.content))
        admission = core.store.one("SELECT * FROM model_admissions")
        core.clock.now = datetime.fromisoformat(admission["lease_until"]) + timedelta(seconds=1)
        assert core.store.one("SELECT lease_until FROM jobs WHERE job_id=?",
                              (job_id,))["lease_until"] > core.clock()

    native_worker_transport(monkeypatch, provider,
                            before=expire_admission if outcome == "admission_expired" else None)
    app = api.create_app(core.settings)
    with TestClient(app, headers={"Authorization": "Bearer " + config.bridge_token}) as client:
        event, job_id = approve_image(client, config, key, core.clock.now)
        prefix, data, headers = claim_image(client, config, key, event)

        async def scenario():
            task = asyncio.create_task(__main__.worker(core.settings))
            try:
                await until(lambda: scans, tasks=[task], deadline_seconds=20)
                assert core.store.one("SELECT state,attempts FROM jobs WHERE job_id=?",
                                      (job_id,)) == {"state": "QUEUED", "attempts": 0}
                assert core.store.one("SELECT state FROM content_items")["state"] == "FETCHING"
                assert calls == [] and core.store.rows("SELECT * FROM model_admissions") == []
                assert not core.store.one("SELECT 1 FROM outbox")
                if outcome == "stopped":
                    stopped = await asyncio.to_thread(client.post,
                        "/internal/v1/owner-authority/stop", json={
                            "authority": config.owner_authority, "operation_id": "stop-image-wait"},
                        headers={"Authorization": "Bearer " +
                                 core.settings.control_token.get_secret_value()})
                    assert stopped.status_code == 200, stopped.text
                elif outcome == "image_lease_expired":
                    lease = core.store.one("SELECT lease_until FROM content_items")["lease_until"]
                    core.clock.now = datetime.fromisoformat(lease) + timedelta(seconds=1)
                uploaded = await asyncio.to_thread(client.post, prefix,
                                                    content=data, headers=headers)
                if outcome in {"stopped", "image_lease_expired"}:
                    assert uploaded.status_code in {403, 409, 503}, uploaded.text
                    assert calls == [] and core.store.rows("SELECT * FROM model_usage") == []
                    assert core.store.rows("SELECT * FROM captured_images") == []
                else:
                    assert uploaded.status_code == 200, uploaded.text
                    await until(lambda: core.store.one(
                        "SELECT 1 FROM model_usage WHERE state<>'RESERVED'"),
                        tasks=[task], deadline_seconds=20)
                    usage = core.store.rows("SELECT * FROM model_usage")
                    assert len(usage) == 1
                    if outcome == "admission_expired":
                        assert calls == [] and len(prepared_requests) == 1
                        assert usage[0]["billed_micro_usd"] == 0
                    else:
                        assert len(calls) == 1 and usage[0]["billed_micro_usd"] > 0
                    if outcome == "settled":
                        await until(lambda: core.store.one("SELECT 1 FROM outbox"),
                                    tasks=[task], deadline_seconds=10)
                        assert usage[0]["state"] == "SETTLED"
                    else:
                        assert usage[0]["state"] == (
                            "UNKNOWN" if outcome == "unknown" else "SETTLED")
                        await until(lambda: core.store.one("SELECT state FROM jobs WHERE job_id=?",
                            (job_id,))["state"] != "RUNNING", tasks=[task], deadline_seconds=10)
                        if outcome == "admission_expired":
                            assert_failure_notice(core)
                        else:
                            assert not core.store.one("SELECT 1 FROM outbox")
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        if outcome == "unknown":
                            core.clock.advance(core.settings.lease_seconds + 1)
                        scans.clear()
                        task = asyncio.create_task(__main__.worker(core.settings))
                        await until(lambda: scans, tasks=[task], deadline_seconds=20)
                        assert len(calls) == (0 if outcome == "admission_expired" else 1)
                        assert core.store.rows("SELECT * FROM model_usage") == usage
                        if outcome == "admission_expired":
                            assert_failure_notice(core)
                        else:
                            assert not core.store.one("SELECT 1 FROM outbox")
                        if outcome == "unknown":
                            assert core.store.one("SELECT state,last_error_code FROM jobs "
                                "WHERE job_id=?", (job_id,)) == {
                                    "state": "FAILED",
                                    "last_error_code": "MODEL_OUTCOME_UNKNOWN"}
                        else:
                            assert core.store.one("SELECT state,last_error_code FROM jobs "
                                "WHERE job_id=?", (job_id,)) == {
                                    "state": "FAILED",
                                    "last_error_code": "JOB_LEASE_LOST"}
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        run(scenario())
    assert len(instances) >= 2 and instances[0].store is not instances[1].store
    assert clients == []


@pytest.mark.parametrize("mode", ["demo", "live_text", "ordinary_content"])
def test_api_without_owner_image_scope_opens_no_model_http_client(
    core, tmp_path, monkeypatch, mode,
):
    if mode == "live_text":
        approved_live(core, tmp_path)
    elif mode == "ordinary_content":
        core.settings.content_enabled = True
    clients = model_clients(monkeypatch, core.settings,
                            lambda request: pytest.fail("No model I/O expected"))
    app = api.create_app(core.settings)
    with TestClient(app) as client:
        assert client.get("/internal/v1/health/live").status_code == 200
        assert app.state.core.model is None
    assert clients == []


def test_api_image_client_closes_when_core_startup_fails(core, tmp_path, monkeypatch):
    image_settings(core, tmp_path, monkeypatch)
    clients = model_clients(monkeypatch, core.settings,
                            lambda request: pytest.fail("Startup must not call the model"))

    def fail_core(*args, **kwargs):
        raise CoreError("SYNTHETIC_STARTUP_FAILURE", 503)

    monkeypatch.setattr(api, "Core", fail_core)
    with pytest.raises(CoreError, match="SYNTHETIC_STARTUP_FAILURE"), TestClient(
        api.create_app(core.settings),
    ):
        pytest.fail("Startup failure must stop lifespan")
    assert len(clients) == 1 and clients[0].is_closed


def test_exclusive_memory_control_opens_no_image_client_even_with_prior_image_settings(
    core, tmp_path, monkeypatch,
):
    config, _ = image_settings(core, tmp_path, monkeypatch)
    approval = grant(core, config, "/记住这个：合成项目")
    clients = model_clients(monkeypatch, core.settings,
                            lambda request: pytest.fail("Memory controls have no model I/O"))
    def forbid_full_core(*args, **kwargs):
        pytest.fail("Exclusive controls must use only the durable runtime")

    monkeypatch.setattr(api, "Core", forbid_full_core)
    app = api.create_app(core.settings, memory_control_grant=approval)
    with TestClient(app) as client:
        assert client.get("/internal/v1/health/live").status_code == 200
        assert client.get("/internal/v1/health/ready").status_code == 403
    assert clients == []
