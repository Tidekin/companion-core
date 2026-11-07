"""Real Core HTTP, independent planning Store and original account delivery lifecycle."""

import json

import httpx
import pytest

from companion import paid_budget
from companion.autonomous_selector import native_selector
from companion.core import Core
from companion.model import ModelClient
from companion.retrieval import SemanticIndex
from companion.social_autonomy_protocol import FrozenPlan, SocialAutonomyConfig
from companion.store import encode
from tests.conftest import ingest, run
from tests.social_model_transport import next_social_turn
from tests.support.core_http import real_core_http
from tests.support.social_autonomy import read_public
from tests.support.social_owner_authority import authorize


@pytest.mark.parametrize("outcome", ["SUCCEEDED", "REJECTED", "UNKNOWN", "CANCELLED", "ERASED"])
def test_account_action_http_uses_distinct_scope_native_budget_and_original_outbox(
    core,
    tmp_path,
    monkeypatch,
    outcome,
):
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    ref = read_public(core, "bilibili")
    owner = ingest(core, 99, "用于绑定的合成输入", origin="onebot-direct:FriendMessage:10001")
    conversation = core.store.one(
        'SELECT conversation_id FROM events WHERE event_id=%s', (owner["event_id"],)
    )["conversation_id"]
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    policy = SocialAutonomyConfig(
        enabled=True, scope="qq_social_v1", authorization_sha256="a" * 64, qzone_enabled=True,
        max_output_tokens=128,
    )
    core.settings.social_autonomy = policy
    core.settings.content_enabled = False
    core.settings.observation_adapters = {}
    _, authority = authorize(core, tmp_path)
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        assert "用于绑定的合成输入" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": encode(
                                {
                                    "kind": "qzone_publish",
                                    "text": "从公开阅读中认识恒星。",
                                    "source_refs": [ref],
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    async def scenario():
        async with real_core_http(core) as port:
            async with (
                httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}",
                    trust_env=False,
                    headers={
                        "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value(),
                    },
                ) as client,
                httpx.AsyncClient(transport=httpx.MockTransport(provider)) as external,
            ):
                with Core(
                    core.settings, ModelClient(core.settings, external), core.clock
                ) as worker:
                    assert worker.store is not core.store
                    selector = native_selector(worker, "independent-social-worker")
                    assert await next_social_turn(selector)
                    planned = core.store.one("SELECT plan_json FROM social_autonomy_plans")
                    frozen = FrozenPlan.model_validate_json(planned["plan_json"])
                    assert core.store.one("SELECT conversation_id FROM outbox")[
                        "conversation_id"] == conversation
                    assert len(calls) == 1
                    assert core.store.one("SELECT scenario,state FROM model_usage") == {
                        "scenario": "social_autonomy",
                        "state": "SETTLED",
                    }
                    assert len(core.store.rows("SELECT * FROM cloud_cost_snapshots")) == 1
                    private = await client.post(
                        "/internal/v1/deliveries/lease",
                        json={"worker_id": "private-only", "authority": authority},
                    )
                    assert private.status_code == 200, private.text
                    assert private.json() == {"items": []}
                    request = {
                        "worker_id": "account-bridge",
                        "limit": 1,
                        "delivery_lane": "qq_account",
                        "authority": authority,
                        "account_actions_authorization_sha256": policy.authorization_sha256,
                    }
                    denied = await client.post(
                        "/internal/v1/deliveries/lease",
                        json={**request, "account_actions_authorization_sha256": "0" * 64},
                    )
                    assert denied.status_code == 403
                    leased = await client.post("/internal/v1/deliveries/lease", json=request)
                    assert leased.status_code == 200, leased.text
                    item = leased.json()["items"][0]
                    assert item["message_id"] == frozen.message_id
                    assert item["content"]["audience"] == {
                        "ugc_right": 16,
                        "target_uins": [core.settings.owner_qq_id],
                    }
                    path = "/internal/v1/deliveries/" + item["message_id"]
                    begun = await client.post(
                        path + "/begin-send", json={"lease_token": item["lease_token"]}
                    )
                    assert begun.status_code == 200, begun.text
                    check = {
                        "authority": authority,
                        "message_id": frozen.message_id,
                        "lease_token": item["lease_token"],
                        "content_sha256": item["content_sha256"],
                        "payload_sha256": frozen.payload_sha256,
                    }
                    if outcome in {"CANCELLED", "ERASED"}:
                        if outcome == "ERASED":
                            with worker.store.transaction():
                                worker.ledger.expire_events(worker.store, [ref], worker.clock())
                        cancelled = await client.post(
                            "/internal/v1/account-actions/cancel-before-send",
                            json={**check, "reason": "LOCAL_PREPARATION_FAILED"},
                        )
                        assert cancelled.status_code == 200, cancelled.text
                        assert cancelled.json()["state"] == "NOT_SENT"
                        assert (
                            await client.post(
                                "/internal/v1/account-actions/cancel-before-send",
                                json={**check, "reason": "LOCAL_PREPARATION_FAILED"},
                            )
                        ).json() == cancelled.json()
                        expected = "CANCELLED"
                    else:
                        checked = await client.post(
                            "/internal/v1/account-actions/send-check", json=check
                        )
                        assert checked.status_code == 200, checked.text
                        again = await client.post(
                            "/internal/v1/account-actions/send-check", json=check
                        )
                        assert again.status_code == 409
                        receipt = {
                            "message_id": frozen.message_id,
                            "lease_token": item["lease_token"],
                            "payload_sha256": frozen.payload_sha256,
                            "action_type": "qzone.publish",
                            "result": outcome,
                            "native_retcode": {"SUCCEEDED": 0, "REJECTED": 100}.get(outcome),
                            "native_receipt_sha256": paid_budget.sha({"tid": "test-tid"})
                            if outcome != "UNKNOWN"
                            else None,
                            "resource_id": "test-tid" if outcome == "SUCCEEDED" else None,
                        }
                        expected = {"SUCCEEDED": "SENT", "REJECTED": "FAILED",
                                    "UNKNOWN": "DELIVERY_UNCERTAIN"}[outcome]
                        body = {
                            "lease_token": item["lease_token"],
                            "result": "PERMANENT_FAILURE" if outcome == "REJECTED" else expected,
                            "account_action_receipt": receipt,
                        }
                        sent = await client.post(path + "/result", json=body)
                        assert sent.status_code == 200, sent.text
                        assert sent.json() == {"state": expected}
                        assert (
                            await client.post(path + "/result", json=body)
                        ).json() == sent.json()
                        if outcome == "REJECTED":
                            before = core.store.db.total_changes
                            conflict = await client.post(path + "/result", json={**body,
                                "account_action_receipt": {**receipt,
                                    "native_receipt_sha256": "e" * 64}})
                            assert conflict.status_code == 409
                            assert core.store.db.total_changes == before
                        stored = core.store.one(
                            "SELECT receipt_json FROM social_autonomy_reservations"
                        )
                        assert (
                            json.loads(stored["receipt_json"])["resource_id"]
                            == receipt["resource_id"]
                        )
                    row = core.store.one(
                        "SELECT state,attempt_count,provider_message_id,native_ack_json FROM outbox"
                    )
                    assert row == {
                        "state": expected,
                        "attempt_count": 1,
                        "provider_message_id": None,
                        "native_ack_json": None,
                    }
                    assert len(calls) == 1

    run(scenario())
