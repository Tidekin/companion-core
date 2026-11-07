import asyncio
import json
from contextlib import closing

import httpx
import pytest

from companion import gateway, paid_budget
from companion.contracts import CoreError
from companion.gateway_api import create_app
from companion.gateway_contract import ChatRequest, RequestContext
from companion.model import ModelClient
from companion.store import Store, encode
from tests.conftest import run
from tests.support.gateway import body, rotate
from tests.support.gateway import gateway_harness as gateway_harness


def provider(request):
    return httpx.Response(200, json={"choices": [{"index": 0, "message": {
        "role": "assistant", "content": "paid reply"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


def api(harness, method, path, *, token=None, **kwargs):
    async def call():
        transport = httpx.ASGITransport(app=create_app(harness.core.settings,
            clock=harness.core.clock), client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.request(method, path, headers={"Authorization": "Bearer " + (
                harness.key if token is None else token), **kwargs.pop("headers", {})}, **kwargs)
    return run(call())


def test_real_client_auth_and_models_expose_only_aliases(harness):
    result = api(harness, "GET", "/v1/models")
    assert result.status_code == 200
    assert {item["id"] for item in result.json()["data"]} == set(harness.grant.model_aliases)
    assert harness.core.settings.model_name not in result.text
    for key in ("bad", harness.core.settings.bridge_token.get_secret_value(),
                harness.core.settings.control_token.get_secret_value()):
        assert api(harness, "GET", "/v1/models", token=key).status_code == 401
    assert harness.key not in encode(harness.core.store.rows("SELECT * FROM gateway_clients"))
    assert not harness.core.store.rows("SELECT * FROM model_usage")


@pytest.mark.parametrize("model", ["xiaofeiyuv1", "xiaofeiyuv2", "xiaofeiyuv3"])
def test_native_passthrough_one_charge_and_lost_http_response_recovery(harness, model):
    payload = body(model, max_completion_tokens=50, temperature=0.2)
    headers = {"Idempotency-Key": "client-request-123"}
    pending = api(harness, "POST", "/v1/chat/completions", json=payload, headers=headers)
    assert pending.status_code == 503
    request_id = pending.headers["X-Request-Id"]
    seen = []

    def transport(request):
        seen.append(json.loads(request.content))
        return provider(request)

    assert harness.process(transport)
    completed = api(harness, "POST", "/v1/chat/completions", json=payload, headers=headers)
    assert completed.status_code == 200, completed.text
    result = completed.json()
    assert result["model"] == model
    assert result["choices"][0]["message"]["content"] == "paid reply"
    assert result["usage"] == {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    assert seen == [{"model": harness.core.settings.model_name,
        "messages": payload["messages"], "max_tokens": 50, "stream": False, "temperature": 0.2}]
    receipt = api(harness, "GET", "/v1/gateway/requests/" + request_id)
    assert receipt.json()["completion"] == result
    assert receipt.json()["accounting"]["state"] == "SETTLED"
    assert not harness.process(lambda req: pytest.fail("settled request replayed"))
    usage = harness.core.store.rows("SELECT * FROM model_usage")
    assert len(usage) == 1 and usage[0]["state"] == "SETTLED" and usage[0]["job_id"] is None
    assert not harness.core.store.rows("SELECT * FROM events")
    assert not harness.core.store.rows("SELECT * FROM outbox")
    assert harness.core.model.calls == []


@pytest.mark.parametrize("changes", [
    {"model": "unauthorized-model"}, {"stream": True}, {"tools": []},
    {"client_id": "owner"}, {"memory_scope": "owner_private"}, {"max_tokens": 129},
])
def test_rejected_requests_never_reserve_or_dispatch(harness, changes):
    result = api(harness, "POST", "/v1/chat/completions", json=body(**changes))
    assert result.status_code in (400, 403, 404, 429)
    assert not harness.core.store.rows("SELECT * FROM model_usage")
    assert not harness.core.store.rows("SELECT * FROM gateway_requests")


@pytest.mark.parametrize("mode", ["companion", "rp"])
def test_private_modes_and_world_selectors_do_not_elevate_client(harness, mode):
    result = api(harness, "POST", "/v1/chat/completions", json=body("xiaofeiyuv1"),
                 headers={"X-Companion-Mode": mode, "X-Companion-World-Id": "real-world"})
    assert result.status_code == 403
    assert not harness.core.store.rows("SELECT * FROM gateway_requests")


def test_same_key_different_frozen_request_conflicts(harness):
    first = harness.submit()
    assert harness.submit() == first
    with pytest.raises(CoreError, match="GATEWAY_IDEMPOTENCY_CONFLICT"):
        harness.submit(text="different")


def test_unknown_provider_usage_retains_liability_and_never_retries(harness):
    request_id = harness.submit()
    harness.process(lambda req: httpx.Response(200, json={"choices": [{"message": {
        "content": "without usage"}, "finish_reason": "stop"}]}))
    result = gateway.receipt(harness.core.store, harness.core.settings, "client-a", request_id,
                             harness.core.clock())
    assert result["status"] == "UNKNOWN" and result["completion"] is None
    usage = harness.core.store.one("SELECT * FROM model_usage")
    assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0
    assert result["accounting"] == {"state": "UNKNOWN",
        "micro_usd": usage["billed_micro_usd"], "currency": "USD",
        "basis": "configured_rates_not_invoice"}
    assert not harness.process(lambda req: pytest.fail("unknown request replayed"))


def test_revoke_during_provider_hook_proves_no_dispatch(harness):
    harness.submit()

    async def work():
        async def revoke(request):
            gateway.revoke_client(harness.core.store, "client-a", harness.core.clock())

        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: pytest.fail("revoked request dispatched")),
            event_hooks={"request": [revoke]}) as transport:
            await gateway.run_once(harness.core.store, harness.core.settings,
                                    ModelClient(harness.core.settings, transport),
                                    harness.core.clock)

    run(work())
    usage = harness.core.store.one("SELECT * FROM model_usage")
    assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] == 0
    assert api(harness, "GET", "/v1/models").status_code == 401


def test_expired_receipt_does_not_resurrect_body_or_execute_again(harness):
    request_id = harness.submit()
    harness.process(provider)
    harness.core.clock.advance(3601)
    result = gateway.receipt(harness.core.store, harness.core.settings, "client-a", request_id,
                             harness.core.clock())
    assert result["status"] == "EXPIRED" and result["completion"] is None
    row = harness.core.store.one("SELECT * FROM gateway_requests")
    assert row["request_cipher"] is None and row["response_cipher"] is None
    assert harness.submit() == request_id
    assert not harness.process(lambda req: pytest.fail("expired request replayed"))


def test_budget_journal_detects_gateway_attribution_loss_and_key_rollback(harness):
    harness.submit()
    harness.process(provider)
    core = harness.core
    with core.store.transaction():
        core.store.execute("UPDATE gateway_requests SET client_id='client-a',body_sha256=?",
                           ("0" * 64,))
    with pytest.raises(CoreError):
        paid_budget.journal(core.store, core.settings.paid_model)


def test_key_rotation_keeps_usage_and_invalidates_old_execution(harness):
    completed = harness.submit(key="completed-request")
    harness.process(provider)
    pending = harness.submit(key="pending-request")
    old_key = harness.key
    rotate(harness)
    assert api(harness, "GET", "/v1/models", token=old_key).status_code == 401
    assert api(harness, "GET", "/v1/models").status_code == 200
    harness.process(lambda req: pytest.fail("old grant resumed by new Key"))
    row = harness.core.store.one("SELECT state FROM gateway_requests WHERE request_id=?",
                                (pending,))
    assert row["state"] == "FAILED"
    recovered = api(harness, "GET", "/v1/gateway/requests/" + completed)
    assert recovered.json()["completion"]["choices"][0]["message"]["content"] == "paid reply"
    assert len(harness.core.store.rows("SELECT * FROM model_usage")) == 1
    with pytest.raises(CoreError, match="GATEWAY_IDEMPOTENCY_CONFLICT"):
        harness.submit(key="completed-request")


def test_key_revocation_cannot_be_rolled_back_in_the_database(harness):
    core = harness.core
    old = core.store.one("SELECT * FROM gateway_clients")
    gateway.revoke_client(core.store, "client-a", core.clock())
    with core.store.transaction():
        core.store.execute("UPDATE gateway_clients SET revoked=0")
    with pytest.raises(CoreError, match="RECONCILIATION_REQUIRED"):
        gateway.authenticate(core.store, core.settings, harness.key, core.clock())
    present = core.store.one("SELECT key_sha256 FROM gateway_clients")
    assert old["key_sha256"] == present["key_sha256"]


def test_alias_revision_change_fences_old_request(harness):
    harness.submit()
    harness.config_path.write_text(harness.config_path.read_text("utf-8").replace(
        "revision = 1", "revision = 2"), encoding="utf-8")
    harness.process(lambda req: pytest.fail("changed alias dispatched"))
    assert not harness.core.store.rows("SELECT * FROM model_usage")


def test_native_client_quota_serializes_independent_workers(harness):
    first = harness.submit(key="first-request-123")
    second = harness.submit(key="second-request-123")

    async def scenario():
        dispatched, release = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            dispatched.set()
            await release.wait()
            return provider(request)

        other = Store(harness.core.settings.database)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(delayed)) as a:
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda req: pytest.fail("client concurrency exceeded"))) as b:
                    task = asyncio.create_task(gateway.run_once(harness.core.store,
                        harness.core.settings, ModelClient(harness.core.settings, a),
                        harness.core.clock))
                    await asyncio.wait_for(dispatched.wait(), 5)
                    await gateway.run_once(other, harness.core.settings,
                        ModelClient(harness.core.settings, b), harness.core.clock)
                    release.set()
                    await task
        finally:
            other.close()

    run(scenario())
    states = {row["request_id"]: row["state"] for row in harness.core.store.rows(
        "SELECT * FROM gateway_requests")}
    assert set(states) == {first, second}
    assert sorted(states.values()) == ["FAILED", "SUCCEEDED"]
    assert len(harness.core.store.rows("SELECT * FROM model_usage")) == 1


def test_actual_client_amount_violation_preserves_native_unknown(harness):
    rotate(harness, budget={**harness.grant.budget.model_dump(), "max_request_micro_usd": 1000})
    harness.submit()

    def overspend(request):
        result = provider(request).json()
        result["usage"]["prompt_tokens"] = 2000
        return httpx.Response(200, json=result)

    harness.process(overspend)
    usage = harness.core.store.one("SELECT * FROM model_usage")
    assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] >= 2000
    diagnostic = harness.core.store.rows(
        "SELECT * FROM audit_log WHERE action='model.usage_diagnostic'")
    assert "GATEWAY_REQUEST_AMOUNT" in str(diagnostic)


def test_native_daily_client_limit_survives_rotation(harness):
    rotate(harness, budget={**harness.grant.budget.model_dump(), "daily_calls": 1})
    harness.submit()
    harness.process(provider)
    rotate(harness)
    harness.submit(key="after-rotation-123")
    harness.process(lambda req: pytest.fail("rotation reset the client daily quota"))
    assert len(harness.core.store.rows("SELECT * FROM model_usage")) == 1


def test_receipt_cannot_be_read_by_another_authenticated_client(harness):
    request_id = harness.submit()
    harness.process(provider)
    other = harness.grant.model_dump(mode="json")
    other.update(client_id="client-b", grant_id="client-b-grant", key_id="client-b-key")
    key = gateway.put_client(harness.core.store, harness.core.settings, other, harness.core.clock())
    assert api(harness, "GET", "/v1/gateway/requests/" + request_id, token=key).status_code == 404


def test_expiry_in_last_http_hook_blocks_actual_provider_handoff(harness):
    harness.submit()

    async def scenario():
        async def expire(request):
            harness.core.clock.advance(121)

        mock = httpx.MockTransport(lambda req: pytest.fail("expired request reached provider"))
        async with httpx.AsyncClient(
            event_hooks={"request": [expire]}, transport=mock,
        ) as transport:
            await gateway.run_once(harness.core.store, harness.core.settings,
                ModelClient(harness.core.settings, transport), harness.core.clock)

    run(scenario())
    row = harness.core.store.one("SELECT * FROM model_usage")
    assert row["state"] == "SETTLED" and row["billed_micro_usd"] == 0


def test_dead_worker_is_unknown_without_replaying_paid_request(harness):
    request_id = harness.submit()

    class Crash(BaseException):
        pass

    with pytest.raises(Crash):
        harness.process(lambda request: (_ for _ in ()).throw(Crash()))
    assert harness.core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"
    harness.core.clock.advance(121)
    assert not harness.process(lambda req: pytest.fail("dead worker was replayed"))
    result = gateway.receipt(harness.core.store, harness.core.settings, "client-a", request_id,
                             harness.core.clock())
    assert result["status"] == "UNKNOWN" and result["completion"] is None


def test_restore_invalidates_keys_and_clears_receipt_bodies(harness, tmp_path):
    from cryptography.fernet import Fernet

    from companion.backup import backup, restore

    harness.submit()
    harness.process(provider)
    key = Fernet.generate_key()
    archive, destination = tmp_path / "snapshot.enc", tmp_path / "restored.db"
    backup(harness.core.settings.database, str(archive), key)
    restore(str(archive), str(destination), key, harness.core.settings.erasure_ledger)
    settings = harness.core.settings.model_copy(update={"database": str(destination)})
    with closing(Store(str(destination))) as restored:
        with pytest.raises(CoreError, match="GATEWAY_INVALID_KEY"):
            gateway.authenticate(restored, settings, harness.key, harness.core.clock())
        row = restored.one("SELECT * FROM gateway_requests")
        assert row["state"] == "UNKNOWN"
        assert row["request_cipher"] is None and row["response_cipher"] is None
        with pytest.raises(CoreError, match="RECONCILIATION_REQUIRED"):
            gateway.submit(restored, settings, harness.grant,
                ChatRequest.model_validate(body()), RequestContext(), "new-intent-123",
                harness.core.clock())


def test_worker_budget_error_is_returned_as_429_with_original_receipt_id(harness):
    rotate(harness, budget={**harness.grant.budget.model_dump(), "max_input_tokens": 1})
    headers = {"Idempotency-Key": "over-input-123"}
    submitted = api(harness, "POST", "/v1/chat/completions", json=body(), headers=headers)
    request_id = submitted.headers["X-Request-Id"]
    harness.process(lambda req: pytest.fail("over-budget request dispatched"))
    rejected = api(harness, "POST", "/v1/chat/completions", json=body(), headers=headers)
    assert rejected.status_code == 429
    assert rejected.headers["X-Request-Id"] == request_id
    assert not harness.core.store.rows("SELECT * FROM model_usage")


def test_remote_socket_and_disabled_listener_cannot_be_bypassed_by_headers(harness):
    async def remote():
        transport = httpx.ASGITransport(app=create_app(harness.core.settings),
                                        client=("203.0.113.5", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            result = await client.get("/v1/models", headers={
                "Authorization": "Bearer " + harness.key, "X-Forwarded-For": "127.0.0.1"})
            assert result.status_code == 403
    run(remote())
    harness.config_path.write_text("enabled = false\n", encoding="utf-8")
    assert api(harness, "GET", "/v1/models").status_code == 503


def test_gateway_custody_blocks_schema_downgrade(harness, tmp_path):
    from contextlib import closing
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from companion.store import Store
    from tests.support.historical_schema import historical_database

    path = historical_database(harness.core.store, tmp_path / "gateway.db", "0059_gateway")
    config = Config()
    config.set_main_option("script_location", str(
        Path(__file__).resolve().parents[1] / "migrations"))
    config.set_main_option("sqlalchemy.url", "sqlite:///" + path.as_posix())
    with pytest.raises(RuntimeError, match="Gateway custody blocks downgrade"):
        command.downgrade(config, "0058_bili_discovery")
    with closing(Store(path)) as store:
        client = store.one("SELECT client_id FROM gateway_clients")
        assert client["client_id"] == "client-a"


def test_gateway_unknown_never_uses_private_owner_attempt_closures(harness):
    core = harness.core
    core.settings.paid_model = core.settings.paid_model.model_copy(update={
        "concurrency_basis": "local_active_attempts_v2"})
    reference = gateway.budget_reference(core.store, core.settings, core.clock())
    rotate(harness, budget={**harness.grant.budget.model_dump(), **reference})
    harness.submit()
    harness.process(lambda req: httpx.Response(503))
    assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"
    assert not paid_budget.journal(core.store, core.settings.paid_model,
                                   allow_settlement=True)["model_closures"]


def test_gateway_obeys_original_cumulative_account_without_owner_message(harness):
    from tests.support.cumulative_budget import policy

    core = harness.core
    policy(core, account={"model_attempts": 1})
    reference = gateway.budget_reference(core.store, core.settings, core.clock())
    rotate(harness, budget={**harness.grant.budget.model_dump(), **reference})
    harness.submit()
    harness.process(provider)
    harness.submit(key="over-account-123")
    harness.process(lambda req: pytest.fail("original cumulative account bypassed"))
    assert len(core.store.rows("SELECT * FROM model_usage")) == 1
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
