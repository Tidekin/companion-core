"""A peer aborting its request body must not become a middleware-generated 500."""

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from companion.api import create_app
from tests.conftest import run
from tests.support.memory_control import real_http


@pytest.mark.parametrize("partial", [False, True])
def test_disconnected_body_never_enters_business_or_invents_response(core, monkeypatch, capsys,
                                                                  partial):
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    app = create_app(core.settings)
    entered, sent = [], []

    @app.post("/synthetic-disconnect")
    async def endpoint():
        entered.append(True)
        return {"status": "ok"}

    async def scenario():
        messages = iter(([{"type": "http.request", "body": b"{", "more_body": True}]
                         if partial else []) + [{"type": "http.disconnect"}])

        async def receive():
            try:
                return next(messages)
            except StopIteration:
                await asyncio.Event().wait()

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": "/synthetic-disconnect", "raw_path": b"/synthetic-disconnect",
                 "query_string": b"", "root_path": "", "headers": [],
                 "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 2)}
        await asyncio.wait_for(app(scope, receive, send), 5)

    run(scenario())
    assert entered == sent == core.model.calls == []
    row, = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert row["route"] == "UNMATCHED" and row["method"] == "POST"
    assert row["status"] is None and row["response_complete"] is False
    assert row["error_class"] is None
    for table in ("jobs", "outbox", "model_usage", "model_admissions"):
        assert not core.store.rows("SELECT * FROM " + table)


def test_real_http_peer_disconnect_does_not_log_500_or_block_next_request(core, caplog):
    app = create_app(core.settings)
    app.state.core = core
    caplog.set_level(logging.ERROR, logger="uvicorn.error")

    async def scenario():
        finished, disconnected = asyncio.Event(), asyncio.Event()

        async def observed(scope, receive, send):
            async def incoming():
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected.set()
                return message

            try:
                await app(scope, incoming, send)
            finally:
                finished.set()

        async with real_http(observed) as port:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"POST /internal/v1/inbox/events HTTP/1.1\r\n"
                         b"Host: localhost\r\nContent-Type: application/json\r\n"
                         b"Content-Length: 100\r\n\r\n{")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(disconnected.wait(), 5)
            await asyncio.wait_for(finished.wait(), 5)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                         trust_env=False) as client:
                assert (await client.get("/internal/v1/health/live")).status_code == 200
                assert (await client.post("/internal/v1/inbox/events", json={})).status_code == 401

    run(scenario())
    assert not [row for row in caplog.records if row.levelno >= logging.ERROR]
    assert core.model.calls == []
    for table in ("jobs", "outbox", "model_usage", "model_admissions"):
        assert not core.store.rows("SELECT * FROM " + table)


def test_console_security_headers_cover_handled_error_and_other_routes_stay_unchanged(core):
    app = create_app(core.settings)
    app.state.core = core
    client = TestClient(app)
    response = client.get("/v1/console/not-a-route")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    live = client.get("/internal/v1/health/live")
    assert live.status_code == 200 and "cache-control" not in live.headers


def test_real_application_runtime_error_still_propagates(core):
    app = create_app(core.settings)

    @app.get("/synthetic-runtime-error")
    async def fail():
        raise RuntimeError("synthetic application failure")

    with pytest.raises(RuntimeError, match="synthetic application failure"):
        TestClient(app).get("/synthetic-runtime-error")
