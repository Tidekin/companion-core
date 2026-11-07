"""Model dispatch races use chunked ASGI bodies and a synthetic provider only."""

import asyncio
import json

import httpx
import pytest

from companion.executor_budget_proxy import create_budget_proxy
from tests.conftest import run
from tests.support.executive import envelope

ADMIN = "synthetic-proxy-admin-" * 3
HEADERS = {"Authorization": "Bearer " + ADMIN}
BODY = {"messages": [{"role": "user", "content": "synthetic"}]}


def proxy(provider, **kwargs):
    return create_budget_proxy(
        ADMIN,
        "https://model.example/v1",
        "synthetic-provider-key",
        "fake-model",
        300000,
        1200000,
        transport=httpx.MockTransport(provider),
        **kwargs,
    )


async def reserve(client):
    response = await client.post(
        "/internal/v1/reservations", headers=HEADERS, json=envelope().model_dump()
    )
    assert response.status_code == 200
    return response.json()["token"]


@pytest.mark.parametrize("boundary", ["revoke", "expiry"])
def test_revoke_or_expiry_during_body_read_prevents_provider_dispatch(boundary):
    now, calls = [0.0], []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={})

    app = proxy(provider, clock=lambda: now[0])

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            token = await reserve(client)
            entered, release = asyncio.Event(), asyncio.Event()

            async def body():
                yield b'{"messages":'
                entered.set()
                await release.wait()
                yield json.dumps(BODY["messages"]).encode() + b"}"

            task = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    content=body(),
                    headers={"Authorization": "Bearer " + token},
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 3)
                if boundary == "revoke":
                    revoked = await asyncio.wait_for(
                        client.delete(
                            "/internal/v1/reservations/" + token,
                            headers=HEADERS,
                        ),
                        1,
                    )
                    assert revoked.status_code == 200 and revoked.json() == {"revoked": True}
                else:
                    now[0] += envelope().timeout_seconds + 6
                release.set()
                result = await asyncio.wait_for(task, 3)
                assert result.status_code == 403 and calls == []
                usage = (
                    await client.get("/internal/v1/reservations/" + token, headers=HEADERS)
                ).json()
                assert usage == {"calls": 0, "reserved_micro_usd": 0}
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(scenario())


def test_stalled_upload_does_not_block_other_task_or_race_past_call_cap():
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={})

    app = proxy(provider)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await reserve(client)
            entered, release = asyncio.Event(), asyncio.Event()

            async def body():
                yield b'{"messages":'
                entered.set()
                await release.wait()
                yield json.dumps(BODY["messages"]).encode() + b"}"

            task = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    content=body(),
                    headers={"Authorization": "Bearer " + first},
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 3)
                second = await asyncio.wait_for(reserve(client), 1)
                for token in (second, first):
                    result = await asyncio.wait_for(
                        client.post(
                            "/v1/chat/completions",
                            json=BODY,
                            headers={"Authorization": "Bearer " + token},
                        ),
                        1,
                    )
                    assert result.status_code == 200
                release.set()
                assert (await asyncio.wait_for(task, 3)).status_code == 429
                assert len(calls) == 2
                for token in (first, second):
                    usage = (
                        await client.get("/internal/v1/reservations/" + token, headers=HEADERS)
                    ).json()
                    assert usage["calls"] == 1
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(scenario())


@pytest.mark.parametrize("unknown", [False, True])
def test_inflight_revocation_retains_cost_reservation_and_never_repeats(unknown):
    async def scenario():
        entered, release, calls = asyncio.Event(), asyncio.Event(), []

        async def provider(request):
            calls.append(request)
            entered.set()
            await release.wait()
            if unknown:
                raise httpx.ReadTimeout("synthetic unknown provider result")
            return httpx.Response(200, json={})

        app = proxy(provider)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            token = await reserve(client)
            auth = {"Authorization": "Bearer " + token}
            task = asyncio.create_task(client.post("/v1/chat/completions", json=BODY, headers=auth))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                revoked = await asyncio.wait_for(
                    client.delete(
                        "/internal/v1/reservations/" + token,
                        headers=HEADERS,
                    ),
                    1,
                )
                assert revoked.status_code == 200
                before = (
                    await client.get("/internal/v1/reservations/" + token, headers=HEADERS)
                ).json()
                assert before["calls"] == 1 and before["reserved_micro_usd"] > 0
                release.set()
                assert (await asyncio.wait_for(task, 3)).status_code == (500 if unknown else 200)
                assert (
                    await client.post("/v1/chat/completions", json=BODY, headers=auth)
                ).status_code == 403
                after = (
                    await client.get("/internal/v1/reservations/" + token, headers=HEADERS)
                ).json()
                assert after == before and len(calls) == 1
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(scenario())
