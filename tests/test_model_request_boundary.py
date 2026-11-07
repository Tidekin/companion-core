"""Inspect actual ASGI receive bytes after request hooks, without provider/socket I/O."""

import json
from datetime import datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from companion import budget
from companion.contracts import CoreError
from companion.model import ModelClient, model_payload
from tests.conftest import run
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_model import approved_live


class UnreadableStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise AssertionError("an unapproved stream must never be iterated")
        yield b""


@pytest.mark.parametrize("mutation", ["stream", "unknown_stream", "cached_and_stream"])
def test_only_admitted_content_reaches_actual_receive(core, tmp_path, mutation):
    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "SYNTHETIC_APPROVED"}]
    expected = model_payload(core.settings, messages, "chat")
    request_id = budget.reserve(core.store, core.settings, authorized_job(core),
        "chat", messages, core.clock())
    received = []

    async def receiver(scope, receive, send):
        content = bytearray()
        while True:
            event = await receive()
            content.extend(event.get("body", b""))
            if not event.get("more_body", False):
                break
        received.append(json.loads(content))
        body = json.dumps({
            "choices": [{"message": {"content": "synthetic reply"}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 2},
        }).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    async def mutate(request):
        changed = {**expected, "messages": [{"role": "user", "content": "CHANGED"}]}
        wire = json.dumps(changed).encode()
        request.stream = (
            UnreadableStream() if mutation == "unknown_stream" else httpx.ByteStream(wire)
        )
        if mutation == "cached_and_stream":
            request._content = wire
        request.headers["Content-Length"] = str(len(wire))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=receiver), event_hooks={"request": [mutate]},
        ) as client:
            model = ModelClient(core.settings, client)
            if mutation == "cached_and_stream":
                with pytest.raises(CoreError, match="REQUEST_CHANGED"):
                    await budget.complete(model, core.store, core.settings, request_id,
                                          messages, "chat", core.clock)
                budget.settle(core.store, core.settings, request_id)
            else:
                response = await budget.complete(model, core.store, core.settings, request_id,
                                                 messages, "chat", core.clock)
                budget.settle(core.store, core.settings, request_id, response)
            assert client.event_hooks["request"] == [mutate]

    run(scenario())
    assert received == ([] if mutation == "cached_and_stream" else [expected])


@pytest.mark.parametrize("mutation", [
    "url", "host", "authorization", "method", "transfer_encoding", "credential", "lease",
    "expired", "approval_removed", "mode", "output_config", "logical_request",
])
def test_every_paid_request_rechecks_after_all_user_hooks(core, tmp_path, mutation):
    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "SYNTHETIC_APPROVED"}]
    request_id = budget.reserve(core.store, core.settings, authorized_job(core),
        "chat", messages, core.clock())
    received = []

    async def mutate(request):
        if mutation == "url":
            request.url = httpx.URL("https://changed.invalid/v1/chat/completions")
        elif mutation == "host":
            request.headers["Host"] = "changed.invalid"
        elif mutation == "authorization":
            request.headers["Authorization"] = "Bearer changed"
        elif mutation == "method":
            request.method = "PUT"
        elif mutation == "transfer_encoding":
            request.headers["Transfer-Encoding"] = "chunked"
        elif mutation == "credential":
            core.settings.model_key = SecretStr("changed-synthetic-key")
        elif mutation == "lease":
            core.clock.advance(core.settings.timeout_seconds + 11)
        elif mutation == "expired":
            core.clock.advance(70 * 86400)
        elif mutation == "mode":
            core.settings.mode = "demo"
        elif mutation == "output_config":
            core.settings.max_output_tokens -= 1
        elif mutation == "logical_request":
            core.store.execute(
                "UPDATE model_admissions SET logical_request_id=? WHERE request_id=?",
                ("other", request_id),
            )
        else:
            core.settings.paid_model = None

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: received.append(request)),
            event_hooks={"request": [mutate]},
        ) as client:
            with pytest.raises(CoreError):
                await budget.complete(ModelClient(core.settings, client), core.store, core.settings,
                                      request_id, messages, "chat", core.clock)
            budget.settle(core.store, core.settings, request_id)
            assert client.event_hooks["request"] == [mutate]

    run(scenario())
    assert received == []
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
        "state": "SETTLED", "billed_micro_usd": 0}


def test_scoped_clients_do_not_close_the_callers_transport_or_accumulate_hooks(core, tmp_path):
    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "hello"}]

    class CountingTransport(httpx.MockTransport):
        closed = 0

        async def aclose(self):
            self.closed += 1

    transport = CountingTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 2},
    }))
    hooks = []

    async def hook(request):
        hooks.append(request)

    async def scenario():
        async with httpx.AsyncClient(
            transport=transport, event_hooks={"request": [hook]},
        ) as client:
            for _ in range(2):
                request_id = budget.reserve(core.store, core.settings, authorized_job(core),
                    "chat", messages,
                                            core.clock())
                response = await budget.complete(ModelClient(core.settings, client), core.store,
                                                 core.settings, request_id, messages, "chat",
                                                 core.clock)
                budget.settle(core.store, core.settings, request_id, response)
                assert transport.closed == 0
                assert client.event_hooks["request"] == [hook]

    run(scenario())
    assert len(hooks) == 2 and transport.closed == 1


@pytest.mark.parametrize("boundary", ["paid", "lease"])
def test_final_sample_rechecks_paid_expiry_as_well_as_the_admission_lease(
    core, tmp_path, monkeypatch, boundary,
):
    from companion import paid_budget

    changes = {"expires_at": core.clock.now + timedelta(seconds=5)} if boundary == "paid" else {}
    approval = approved_live(core, tmp_path, **changes)
    messages = [{"role": "user", "content": "synthetic final clock"}]
    # Isolate the admission boundary from the separate native job deadline.
    job_id = authorized_job(core, lease_seconds=core.settings.lease_seconds * 2)
    request_id = budget.reserve(core.store, core.settings, job_id,
        "chat", messages, core.clock())
    lease = datetime.fromisoformat(core.store.one("SELECT lease_until FROM model_admissions")[
        "lease_until"
    ])
    original = paid_budget.contract
    dispatched, hooked, checks = [], [], []

    async def hook(request):
        hooked.append(True)

    def advance_after_final_contract(settings, now):
        value = original(settings, now)
        if hooked:
            checks.append(now)
            if len(checks) == 2:
                core.clock.now = approval.expires_at if boundary == "paid" else lease
        return value

    monkeypatch.setattr(paid_budget, "contract", advance_after_final_contract)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: dispatched.append(request)),
            event_hooks={"request": [hook]},
        ) as client:
            code = "PAID_APPROVAL_EXPIRED" if boundary == "paid" else "JOB_LEASE_LOST"
            with pytest.raises(CoreError, match=code):
                await budget.complete(ModelClient(core.settings, client), core.store, core.settings,
                                      request_id, messages, "chat", core.clock)
            budget.settle(core.store, core.settings, request_id)

    run(scenario())
    assert len(checks) == 2 and dispatched == []
    if boundary == "lease":
        assert core.store.one("SELECT lease_until FROM jobs WHERE job_id=?",
                              (job_id,))["lease_until"] > core.clock()
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
        "state": "SETTLED", "billed_micro_usd": 0}
