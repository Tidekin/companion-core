"""Exercise the actual v3 worker with mocked Core and provider transports."""

import asyncio
import copy
import json
from datetime import datetime, timedelta

import httpcore
import httpx
import pytest

from companion.reading_protocol import ReadObservation
from source_reader import worker_v1
from source_reader.bili_tools import BilibiliAccountHTTP
from source_reader.bilibili import BilibiliReader
from source_reader.network import ReadFailure
from source_reader.web import WebHTTP
from tests.conftest import run
from tests.support.heybox_reader import mock_pool
from tests.support.source_reader import (
    ITEM,
    META,
    NOW,
    PLAYER,
    RecordingStream,
    Responses,
    response,
)
from tests.test_next_reading import HTML, WEB, web_response
from tests.test_plugin_readonly import SECRET, grant

NOW_DT = datetime.fromisoformat(NOW)
TOKEN = "isolated-reader-core-token-only"


def lease(source="web", **changes):
    return {
        **ITEM, "schema_version": 1, "request_id": "read_fixture_v1",
        "lease_until": (NOW_DT + timedelta(seconds=30)).isoformat(),
        "url": WEB if source == "web" else ITEM["url"],
        "context": {"root_task_id": "root_fixture_v1", "source": source,
                    "source_event_ids": ["evt_owner_1"], "privacy_epoch": 3},
        "allowed_hosts": ["articles.example"] if source == "web" else [],
        **changes,
    }


class MockCore:
    def __init__(self, item, validate=None, receipt_error=False):
        self.item, self.validate, self.receipt_error = item, validate, receipt_error
        self.paths, self.bodies, self.results, self.validations = [], [], [], 0

    async def handle(self, request):
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        assert request.url.path.startswith("/internal/v3/readers/fixture/")
        self.paths.append(request.url.path)
        body = json.loads(request.content)
        self.bodies.append(body)
        if request.url.path.endswith("/lease"):
            item, self.item = self.item, None
            return httpx.Response(200, json={"item": item})
        if request.url.path.endswith("/validate"):
            self.validations += 1
            assert set(body) <= {"lease_token", "source_grant"}
            assert body["lease_token"] == ITEM["lease_token"]
            if self.validate:
                return await self.validate(self.validations)
            return httpx.Response(200, json={"valid": True})
        assert request.url.path.endswith("/result")
        # The independently authored Core DTO is an executable compatibility boundary.
        ReadObservation.model_validate(body)
        self.results.append(body)
        if self.receipt_error:
            raise httpx.ReadTimeout("uncertain receipt")
        return httpx.Response(200, json={"state": "DONE", "duplicate": False})

    def client(self):
        return httpx.AsyncClient(base_url="http://core", transport=httpx.MockTransport(self.handle),
                                 headers={"Authorization": "Bearer " + TOKEN})


def public_web(monkeypatch, *responses):
    pool, backend = mock_pool(*responses)
    scopes = []

    def factory(hosts):
        scopes.append(hosts)
        return WebHTTP(hosts, pool)

    monkeypatch.setattr(worker_v1, "WebHTTP", factory)
    return backend, scopes


async def web_tick(core, **kwargs):
    async with core.client() as client:
        return await worker_v1.tick(client, None, "fixture", source="web",
                                    clock=lambda: NOW_DT, **kwargs)


def test_empty_lease_never_constructs_provider(monkeypatch):
    def forbidden(*args):
        raise AssertionError("No provider without a lease")

    monkeypatch.setattr(worker_v1, "WebHTTP", forbidden)
    core = MockCore(None)
    assert run(web_tick(core)) is False
    assert len(core.paths) == 1


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"max_bytes": 1048577}, {"max_requests": 9},
    {"max_items": 0}, {"request_id": "../foreign"}, {"allowed_hosts": ["*.example"]},
    {"lease_until": NOW}, {"operation": "discover"}, {"url": ""},
    {"context": {"root_task_id": "root", "source": "heybox",
                 "source_event_ids": [], "privacy_epoch": 0}},
    {"context": {"root_task_id": "root", "source": "web",
                 "source_event_ids": ["dup", "dup"], "privacy_epoch": 0}},
])
def test_malformed_or_foreign_lease_fails_before_public_io(monkeypatch, change):
    backend, scopes = public_web(monkeypatch)
    core = MockCore(lease(**change))
    with pytest.raises(ReadFailure):
        run(web_tick(core))
    assert not backend.requests and not scopes and not core.results


def test_web_worker_uses_only_lease_hosts_and_sends_flat_evidence_result(monkeypatch):
    backend, scopes = public_web(monkeypatch, web_response())
    core = MockCore(lease())
    assert run(web_tick(core, local_hosts=["articles.example", "evil.example"]))
    result = core.results[0]
    assert scopes == [frozenset({"articles.example"})]
    assert len(backend.requests) == 1 and core.validations >= 4
    assert result["status"] == "SUCCESS" and result["used_requests"] == 1
    assert result["lease_token"] == ITEM["lease_token"]
    assert result["observations"][0]["provenance"] == lease()["context"]
    assert all(TOKEN.encode() not in request for request in backend.requests)


def test_local_scope_can_only_remove_core_hosts(monkeypatch):
    backend, scopes = public_web(monkeypatch)
    core = MockCore(lease())
    assert run(web_tick(core, local_hosts=["evil.example"]))
    assert not scopes and not backend.requests
    assert core.results[0]["error_code"] == "READER_HOST_SCOPE_EMPTY"
    assert core.results[0]["used_requests"] == 0


def test_url_cannot_grant_its_own_host(monkeypatch):
    backend, _ = public_web(monkeypatch)
    core = MockCore(lease(url="https://ungranted.example/private"))
    assert run(web_tick(core))
    assert not backend.requests
    assert core.results[0]["error_code"] == "CONTENT_URL_NOT_ALLOWED"


@pytest.mark.parametrize(("granted", "local", "expected"), [
    (["*"], None, {"*"}), (["*"], ["articles.example"], {"articles.example"}),
    (["articles.example"], ["*"], {"articles.example"}),
])
def test_all_public_scope_preserves_local_restrictions(monkeypatch, granted, local, expected):
    backend, scopes = public_web(monkeypatch, web_response())
    core = MockCore(lease(allowed_hosts=granted))
    assert run(web_tick(core, local_hosts=local))
    assert scopes == [frozenset(expected)] and len(backend.requests) == 1
    assert core.results[0]["status"] == "SUCCESS"


def test_titleless_long_url_keeps_dto_title_limit(monkeypatch):
    public_web(monkeypatch, web_response(HTML.replace("<title>测试文章</title>".encode(), b"")))
    url = "https://articles.example/" + "a" * 400
    core = MockCore(lease(url=url))
    assert run(web_tick(core))
    row = core.results[0]["observations"][0]
    assert row["title"] == url[:300] and row["url"] == url


@pytest.mark.parametrize("decision", [{"valid": 1}, {"valid": True, "extra": True}, {}])
def test_validation_requires_exact_true(monkeypatch, decision):
    backend, _ = public_web(monkeypatch)

    async def validate(count):
        return httpx.Response(200, json=decision)

    core = MockCore(lease(), validate)
    assert run(web_tick(core))
    assert not backend.requests and core.results[0]["status"] == "CANCELLED"
    assert core.results[0]["used_requests"] == 0


def test_lease_expiring_during_validation_cannot_dispatch(monkeypatch):
    backend, _ = public_web(monkeypatch)
    now = NOW_DT

    async def validate(count):
        nonlocal now
        now += timedelta(seconds=31)
        return httpx.Response(200, json={"valid": True})

    async def scenario():
        core = MockCore(lease(), validate)
        async with core.client() as client:
            assert await worker_v1.tick(client, None, "fixture", source="web", clock=lambda: now)
        assert core.results[0]["error_code"] == "STALE_READER_LEASE"

    run(scenario())
    assert not backend.requests


@pytest.mark.parametrize("reason", ["INPUT_RECALLED", "LIVE_WINDOW_STOPPED", "unknown_validation"])
def test_watcher_cancels_body_download_closes_transport_and_retains_usage(monkeypatch, reason):
    async def scenario():
        started, closed = asyncio.Event(), asyncio.Event()

        class Stream(RecordingStream):
            headers_sent = False

            async def read(self, max_bytes, timeout=None):  # noqa: ASYNC109
                if not self.headers_sent:
                    self.headers_sent = True
                    return (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                            b"Content-Length: 500\r\n\r\n")
                started.set()
                await asyncio.Event().wait()

            async def aclose(self):
                closed.set()

        class Backend(Responses):
            async def connect_tcp(self, host, port, **kwargs):
                return Stream(b"", self)

        backend = Backend()
        pool = httpcore.AsyncConnectionPool(network_backend=backend, max_keepalive_connections=0)
        monkeypatch.setattr(worker_v1, "WebHTTP", lambda hosts: WebHTTP(hosts, pool))

        async def validate(count):
            if started.is_set():
                if reason == "unknown_validation":
                    raise httpx.ReadTimeout("validation unavailable")
                return httpx.Response(409, json={"error": reason})
            return httpx.Response(200, json={"valid": True})

        core = MockCore(lease(), validate)
        assert await web_tick(core, validation_interval=0.5)
        assert closed.is_set() and len(backend.requests) == 1
        result = core.results[0]
        assert result["status"] == "CANCELLED" and result["observations"] == []
        assert result["used_requests"] is None and result["used_bytes"] is None
        assert result["observed_requests"] == 1 and result["observed_bytes"] > 0
        assert result["error_code"] == (
            "READER_VALIDATION_UNKNOWN" if reason == "unknown_validation" else reason
        )

    run(scenario())


def test_uncertain_result_receipt_never_repeats_provider_read(monkeypatch):
    backend, _ = public_web(monkeypatch, web_response())
    core = MockCore(lease(), receipt_error=True)
    with pytest.raises(httpx.ReadTimeout):
        run(web_tick(core))
    assert len(backend.requests) == 1
    assert run(web_tick(core)) is False
    assert len(backend.requests) == 1 and len(core.results) == 1


def test_existing_account_path_supports_49_spans_and_real_grant_shape(tmp_path):
    path = grant(tmp_path, "bilibili", authorization={
        "grant_id": "grant_" + "1" * 32, "epoch": 1, "fingerprint": "2" * 64,
    })
    captions = {"body": [{"from": i * 2, "to": i * 2 + 1.5, "content": f"模拟字幕 {i}"}
                         for i in range(49)]}
    player = copy.deepcopy(PLAYER)
    player["data"]["subtitle"]["subtitles"][0]["lan"] = "ai-zh"

    async def scenario():
        pool, backend = mock_pool(response(META), response(player), response(captions))
        async with BilibiliAccountHTTP(path, pool, clock=lambda: NOW_DT) as public:
            snapshot = public.grant_snapshot(required=True)
            core = MockCore(lease("bilibili", source_grant=snapshot))
            async with core.client() as client:
                assert await worker_v1.tick(client, BilibiliReader(public, clock=lambda: NOW),
                                            "fixture", source="bilibili", clock=lambda: NOW_DT)
        result = core.results[0]
        row = result["observations"][0]
        assert result["source_grant"] == row["provenance"]["source_grant"] == snapshot
        assert row["coverage"]["level"] == "L2" and len(row["coverage"]["spans"]) == 49
        assert row["evidence"]["method"] == "account_api" and result["used_requests"] == 3
        assert SECRET not in json.dumps(core.bodies)
        assert SECRET.encode() in backend.requests[0] and SECRET.encode() in backend.requests[1]
        assert SECRET.encode() not in backend.requests[2]

    run(scenario())


def test_grant_changed_after_lease_is_rejected_before_provider_io(tmp_path):
    path = grant(tmp_path, "bilibili", authorization={
        "grant_id": "grant_" + "1" * 32, "epoch": 1, "fingerprint": "2" * 64,
    })

    async def scenario():
        pool, backend = mock_pool()
        async with BilibiliAccountHTTP(path, pool, clock=lambda: NOW_DT) as public:
            snapshot = public.grant_snapshot(required=True)
            core = MockCore(lease("bilibili", source_grant={**snapshot, "epoch": 2}))
            async with core.client() as client:
                with pytest.raises(ReadFailure):
                    await worker_v1.tick(client, BilibiliReader(public), "fixture",
                                         source="bilibili", clock=lambda: NOW_DT)
        assert not backend.requests and not core.results

    run(scenario())


def test_slow_core_validation_under_lock_contention_does_not_cancel_the_read(monkeypatch):
    # Production saw one validate take 2.1 s while Core waited for its SQLite write lock.
    backend, _ = public_web(monkeypatch, web_response())

    async def validate(count):
        if count == 1:
            await asyncio.sleep(2.5)
        return httpx.Response(200, json={"valid": True})

    core = MockCore(lease(), validate)
    assert run(web_tick(core))
    assert core.results[0]["status"] == "SUCCESS" and len(backend.requests) == 1
