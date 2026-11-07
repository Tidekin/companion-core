"""Native poke HTTP admission through the real Core runner and paid model boundary."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from companion import input_protocol, owner_delivery, poke
from companion.api import create_app
from companion.contracts import CoreError, LeaseRequest
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerSendCheck
from companion.transport import begin_send, lease_deliveries
from tests.poke_authority_fixture import environment, settle, signed
from tests.support.poke import claim, receive


@pytest.mark.parametrize("kind", ["text", "poke", "none"])
def test_real_native_poke_admission_runner_and_delivery(core, tmp_path, kind):
    env = environment(core, tmp_path)
    capture = signed(env)
    app = create_app(core.settings)
    app.state.core = core
    headers = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
    client = TestClient(app)
    received = client.post("/internal/v1/pokes", headers=headers,
                           json=capture.model_dump(mode="json"))
    assert received.status_code == 200, received.text
    assert received.headers["Cache-Control"] == "no-store"
    core.clock.advance(core.settings.poke_policy.coalesce_seconds + 1)
    calls = []

    async def run():
        def provider(request):
            calls.append(request)
            decision = {"kind": kind, **({"text": "在呢。"} if kind == "text" else {})}
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps(decision)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 5}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            core.model = ModelClient(core.settings, http)
            assert await core._run_poke_once("native-poke-entry")
    asyncio.run(run())
    assert len(calls) == 1
    job = core.store.one("SELECT * FROM jobs WHERE kind='poke'")
    assert job["state"] == "SUCCEEDED", job["last_error_code"]
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    items = lease_deliveries(core.store, core.settings, LeaseRequest(
        worker_id="native-bridge", authority=capture.authority), core.clock())["items"]
    assert len(items) == (0 if kind == "none" else 1)
    if not items:
        return
    item = items[0]
    assert item["authority"] == capture.authority
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock(),
               core.settings, clock=core.clock)
    check = OwnerSendCheck(authority=capture.authority, event_id=capture.event_id,
        message_id=item["message_id"], content_sha256=item["content_sha256"],
        lease_token=item["lease_token"])
    assert owner_delivery.send_check(core.store, core.settings, core.clock, check)["approved"]


@pytest.mark.parametrize("evidence", ["valid", "changed_hash", "observed"])
def test_native_cancel_preserves_original_pre_dispatch_proof(core, tmp_path, evidence):
    env = environment(core, tmp_path)
    receive(env)
    job = claim(env)
    choice = {"kind": "poke"}
    settle(env, job, choice)
    message_id = poke.finish(env.store, env.settings, env.approval, job, choice, env.clock())[0]
    item = lease_deliveries(core.store, core.settings, LeaseRequest(
        worker_id="native-bridge", authority=env.approval.authority.as_dict()),
        core.clock())["items"][0]
    begin_send(core.store, message_id, item["lease_token"], core.clock(),
               core.settings, clock=core.clock)
    with core.store.transaction():
        core.ledger.expire_events(core.store, [job["event_id"]], core.clock())
        if evidence == "observed":
            core.store.audit("delivery.result", message_id, "delivery", core.clock(),
                             state="DELIVERY_UNCERTAIN")
    body = input_protocol.OwnerCancelBeforeSend(authority=env.approval.authority.as_dict(),
        message_id=message_id, lease_token=item["lease_token"],
        content_sha256="f" * 64 if evidence == "changed_hash" else item["content_sha256"])
    if evidence == "valid":
        result = input_protocol.cancel_before_send(core.store, core.settings, core.clock, body)
        assert result["state"] == "CANCELLED"
        replay = input_protocol.cancel_before_send(core.store, core.settings, core.clock, body)
        assert replay == result
    else:
        with pytest.raises(CoreError, match="INPUT_CANCEL_BEFORE_SEND_UNPROVEN"):
            input_protocol.cancel_before_send(core.store, core.settings, core.clock, body)
    stored = core.store.one('SELECT state,attempt_count FROM outbox WHERE message_id=%s',
                            (message_id,))
    assert stored == {"state": "CANCELLED" if evidence == "valid" else "DELIVERY_UNCERTAIN",
                      "attempt_count": 1}
