import asyncio
import json
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from companion import api, api_telemetry, telemetry
from companion.config import Settings


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")


def records(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def observed(app):
    app.add_middleware(api_telemetry.ApiTelemetry, routes=app.routes)
    return app


def test_matched_templates_http_errors_and_no_private_values(enabled, capsys):
    app = FastAPI()

    @app.post("/objects/{object_id}")
    async def echo(object_id: str, request: Request):
        return {"id": object_id, "body": (await request.body()).decode()}

    @app.get("/denied/{object_id}")
    async def denied(object_id: str):
        raise HTTPException(403, "SECRET_EXCEPTION_" + object_id)

    with TestClient(observed(app)) as client:
        for identity in ("SECRET_ID_A", "SECRET_ID_B"):
            response = client.post("/objects/" + identity + "?q=SECRET_QUERY",
                                   content="SECRET_BODY", headers={"Authorization": "SECRET_TOKEN"})
            assert response.json() == {"id": identity, "body": "SECRET_BODY"}
        assert client.get("/denied/SECRET_DENIED").status_code == 403
        assert client.get("/SECRET_UNKNOWN?q=SECRET_UNKNOWN_QUERY").status_code == 404
    rows = records(capsys)
    assert [row["route"] for row in rows] == [
        "/objects/{object_id}", "/objects/{object_id}", "/denied/{object_id}", "UNMATCHED"]
    assert [row["status"] for row in rows] == [200, 200, 403, 404]
    assert [row["error_class"] for row in rows] == [None, None, "HTTPError", "HTTPError"]
    assert all(row["response_complete"] for row in rows)
    assert all(row["elapsed_ms"] >= 0 for row in rows)
    assert all(row["stage"] == "api_request" for row in rows)
    assert "SECRET" not in json.dumps(rows)
    assert all(len(json.dumps(row)) + 1 <= telemetry.MAX_API_RECORD_BYTES for row in rows)


def test_stream_is_not_buffered_or_timed_at_headers(enabled, capsys, monkeypatch):
    clock = [0]
    monkeypatch.setattr(api_telemetry, "perf_counter_ns", lambda: clock[0])
    app = FastAPI()

    @app.get("/stream")
    async def stream():
        async def chunks():
            yield b"SECRET_CHUNK_1"
            await asyncio.sleep(0)
            clock[0] = 75_000_000
            yield b"SECRET_CHUNK_2"
        return StreamingResponse(chunks(), headers={"X-Private": "SECRET_HEADER"})

    with TestClient(observed(app)) as client:
        response = client.get("/stream")
        assert response.content == b"SECRET_CHUNK_1SECRET_CHUNK_2"
        assert response.headers["X-Private"] == "SECRET_HEADER"
    row, = records(capsys)
    assert row["elapsed_ms"] == 75
    assert row["response_complete"] is True
    assert row["status"] == 200
    assert "SECRET" not in json.dumps(row)


@pytest.mark.parametrize(("failure", "error_class"), [
    (psycopg.OperationalError("SECRET_SQL"), "OperationalError"),
    (psycopg.DatabaseError("SECRET_DB"), "DatabaseError"),
    (RuntimeError("SECRET_INTERNAL"), "UnhandledError"),
])
def test_unhandled_error_propagates_and_outer_500_is_not_invented(
    enabled, capsys, failure, error_class,
):
    app = FastAPI()

    @app.get("/failure/{identity}")
    async def fail(identity: str):
        raise failure

    with TestClient(observed(app), raise_server_exceptions=False) as client:
        response = client.get("/failure/SECRET_ID")
        assert response.status_code == 500
    row, = records(capsys)
    assert row["route"] == "/failure/{identity}"
    assert row["status"] is None
    assert row["response_complete"] is False
    assert row["error_class"] == error_class
    assert "SECRET" not in json.dumps(row)


def test_cancellation_propagates_without_a_response(enabled, capsys):
    async def scenario():
        entered = asyncio.Event()
        failure = asyncio.CancelledError("SECRET_CANCEL")
        app = FastAPI()

        @app.get("/cancel/{identity}")
        async def wait(identity: str):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise failure from None

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        messages = []

        async def send(message):
            messages.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "method": "GET", "path": "/cancel/SECRET_ID", "root_path": "",
                 "query_string": b"secret=SECRET_QUERY", "headers": [],
                 "scheme": "http", "http_version": "1.1"}
        task = asyncio.create_task(observed(app)(scope, receive, send))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value is failure
        assert messages == []
    async def bounded():
        await asyncio.wait_for(scenario(), 5)
    asyncio.run(bounded())
    row, = records(capsys)
    assert row["route"] == "/cancel/{identity}"
    assert row["status"] is None
    assert row["error_class"] == "CancelledError"
    assert row["response_complete"] is False
    assert "SECRET" not in json.dumps(row)


@pytest.mark.parametrize("reason", ["disabled", "invalid_evidence"])
def test_opt_in_and_evidence_gates_have_no_records(monkeypatch, capsys, reason):
    monkeypatch.setenv("COMPANION_TELEMETRY", "0" if reason == "disabled" else "1")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE",
                       "OFFLINE_FIXTURE" if reason == "disabled" else "SECRET_EVIDENCE")
    app = FastAPI()

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    with TestClient(observed(app)) as client:
        assert client.get("/ok").json() == {"ok": True}
    assert records(capsys) == []


@pytest.mark.parametrize("failure", [OSError("SECRET_SINK"), ValueError("SECRET_CLOSED")])
def test_broken_log_sink_cannot_replace_response(enabled, monkeypatch, failure):
    def broken(*args, **kwargs):
        raise failure
    monkeypatch.setattr(telemetry, "print", broken, raising=False)
    app = FastAPI()

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    with TestClient(observed(app)) as client:
        assert client.get("/ok").json() == {"ok": True}


def test_record_size_is_bounded(enabled, capsys):
    telemetry.emit_api("/" + "x" * 5000, "GET", status=200, elapsed_ms=1,
                       error_class=None, response_complete=True)
    assert records(capsys) == []


@pytest.mark.parametrize("ending", ["pathsend", "trailers"])
def test_asgi_completion_extensions_and_background_time(enabled, capsys, monkeypatch, ending):
    clock, messages = [0], []
    monkeypatch.setattr(api_telemetry, "perf_counter_ns", lambda: clock[0])
    route = SimpleNamespace(path_format="/files/{identity}")

    async def handler(scope, receive, send):
        scope["route"] = route
        await send({"type": "http.response.start", "status": 200, "headers": [],
                    "trailers": ending == "trailers"})
        clock[0] = 2_000_000
        if ending == "pathsend":
            await send({"type": "http.response.pathsend", "path": "/SECRET_FILE"})
        else:
            await send({"type": "http.response.body", "body": b"SECRET_BODY"})
            clock[0] = 3_000_000
            await send({"type": "http.response.trailers", "headers": [(b"x", b"SECRET")]})
        clock[0] = 90_000_000  # Work after the response must not inflate response latency.

    async def receive():
        raise AssertionError("telemetry must not read requests")

    async def send(message):
        messages.append(message)

    middleware = api_telemetry.ApiTelemetry(handler, routes=[route])
    asyncio.run(middleware({"type": "http", "method": "GET"}, receive, send))
    assert messages[-1]["type"] == "http.response." + ending
    row, = records(capsys)
    assert row["response_complete"] is True
    assert row["elapsed_ms"] == (2 if ending == "pathsend" else 3)
    assert "SECRET" not in json.dumps(row)


def test_non_http_bypass_and_unregistered_scope_route(enabled, capsys):
    async def handler(scope, receive, send):
        scope["route"] = SimpleNamespace(path_format="/SECRET_UNREGISTERED")
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    async def unused(*args):
        pass

    middleware = api_telemetry.ApiTelemetry(handler, routes=[])
    asyncio.run(middleware({"type": "websocket"}, unused, unused))
    assert records(capsys) == []
    asyncio.run(middleware({"type": "http", "method": "SECRET_METHOD"}, unused, unused))
    row, = records(capsys)
    assert row["route"] == "UNMATCHED"
    assert row["method"] == "OTHER"
    assert "SECRET" not in json.dumps(row)


def test_api_registration_reuses_one_lifespan_and_fixed_core_routes(enabled, monkeypatch, capsys):
    lifecycle = []
    settings = Settings(owner_qq_id="10001", bot_qq_id="10002", bridge_token="x" * 32,
                        database="dbname=synthetic")

    def make_core(*args):
        lifecycle.append("init")
        return SimpleNamespace(database=object(), store=SimpleNamespace(),
                               close=lambda: lifecycle.append("close"))

    monkeypatch.setattr(api, "Core", make_core)
    with TestClient(api.create_app(settings)) as client:
        assert client.get("/internal/v1/health/live").json()["status"] == "alive"
        response = client.post("/internal/v1/owner-authority/inputs/approve", json={})
        assert response.status_code == 401
        response = client.post("/internal/v1/owner-authority/inputs/approve", json={},
                               headers={"Authorization": "Bearer " + "x" * 32})
        assert response.status_code == 400
    assert lifecycle == ["init", "close"]
    rows = records(capsys)
    assert len(rows) == 3
    assert rows[0]["route"] == "/internal/v1/health/live"
    assert all(row["route"] == "/internal/v1/owner-authority/inputs/approve" for row in rows[1:])
    assert [row["status"] for row in rows] == [200, 401, 400]
    assert [row["error_class"] for row in rows] == [None, "HTTPError", "HTTPError"]


def test_summary_uses_known_routes_evidence_and_nearest_rank():
    def sample(duration, **extra):
        return {"schema_version": 1, "evidence_type": "LIVE_ONEBOT", "stage": "api_request",
                "route": "/objects/{identity}", "method": "GET", "status": 200,
                "elapsed_ms": duration, "error_class": None, "response_complete": True, **extra}
    rows = [sample(value) for value in (10, 20, 30, 40)]
    rows[-1] = sample(40, status=None, error_class="CancelledError", response_complete=False)
    rows.extend([sample(1000, route="/SECRET_ID"),
                 sample(1000, evidence_type="OFFLINE_FIXTURE"),
                 sample(float("nan")), sample(10**500), sample(-1),
                 sample(1, status=["SECRET"]),
                 sample(1, error_class={"SECRET": "EXCEPTION"}), {"body": "SECRET"}])
    missing = sample(1)
    del missing["status"]
    rows.append(missing)
    result = telemetry.summarize_api(iter(rows), "LIVE_ONEBOT", {"/objects/{identity}"})
    assert result["percentile_method"] == "nearest_rank"
    assert result["records_scanned"] == len(rows)
    assert result["truncated"] is False
    assert result["routes"] == [{"route": "/objects/{identity}", "method": "GET", "count": 4,
                                 "error_count": 1, "error_rate": 0.25, "p50_ms": 20, "p95_ms": 40}]
    assert "SECRET" not in json.dumps(result)


def test_summary_caps_input_without_consuming_an_unbounded_log():
    consumed = []

    def rows():
        while True:
            consumed.append(1)
            yield {}
    result = telemetry.summarize_api(rows(), "LIVE_ONEBOT", set())
    assert len(consumed) == telemetry.MAX_API_SAMPLES + 1
    assert result["records_scanned"] == telemetry.MAX_API_SAMPLES
    assert result["truncated"] is True
    assert result["routes"] == []
