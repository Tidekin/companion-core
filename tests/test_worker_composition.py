"""Workers depend on their own services, with semantic startup kept on chat/life."""

import asyncio
import importlib
import sqlite3
from functools import partial

import httpx
import pytest

from companion.config import Settings
from companion.contracts import CoreError
from companion.memory import Memory
from companion.retrieval import SemanticIndex
from companion.runtime import Runtime
from companion.store import Store, migrate
from companion.worker_wakeup import Wakeup


@pytest.fixture
def worker_settings(tmp_path):
    settings = Settings(
        owner_qq_id="10001",
        bot_qq_id="10002",
        bridge_token="test-token-" * 4,
        database=str(tmp_path / "core.db"),
        erasure_ledger=str(tmp_path / "privacy.jsonl"),
        memory_enabled=True,
    )
    migrate(settings.database)
    return settings


@pytest.fixture
def unavailable_semantics(monkeypatch):
    calls = []

    def fail(index):
        calls.append(index)
        raise CoreError("MEMORY_INDEX_UNAVAILABLE", 503)

    monkeypatch.setattr(SemanticIndex, "warmup", fail)
    return calls


@pytest.fixture
def opened_stores(monkeypatch):
    stores = []
    original = Store.__init__

    def capture(store, *args, **kwargs):
        original(store, *args, **kwargs)
        stores.append(store)

    monkeypatch.setattr(Store, "__init__", capture)
    yield stores
    # Avoid leaking handles even when an assertion exposes a regression.
    for store in stores:
        store.close()


@pytest.mark.parametrize(
    "module_name,entry,service,operation",
    [
        ("companion.content_worker", "content_worker", "ContentService", "run_batch"),
        ("companion.executive_worker", "executive_worker", "Executive", "run_one"),
    ],
)
def test_nonsemantic_worker_enters_own_loop_and_closes_store(
    worker_settings, unavailable_semantics, opened_stores, monkeypatch,
    module_name, entry, service, operation,
):
    module = importlib.import_module(module_name)
    service_class = getattr(importlib.import_module({
        "ContentService": "companion.content",
        "Executive": "companion.executive",
    }[service]), service)
    worked = []

    async def tick(instance, *args):
        assert instance.store.one("SELECT value FROM runtime_meta WHERE key='owner_qq_id'") == {
            "value": worker_settings.owner_qq_id,
        }
        worked.append(instance)
        return False

    async def stop(*_args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(service_class, operation, tick)
    monkeypatch.setattr(Wakeup, "wait", stop)

    def forbidden_memory(*args, **kwargs):
        pytest.fail("A nonsemantic worker constructed Memory")

    monkeypatch.setattr(Memory, "__init__", forbidden_memory)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(getattr(module, entry)(worker_settings))
    assert len(worked) == 1
    assert unavailable_semantics == []
    assert len(opened_stores) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened_stores[0].one("SELECT 1")


@pytest.mark.parametrize("failure", ["owner", "ledger"])
def test_shared_bootstrap_rejects_identity_or_ledger_mismatch_and_closes_store(
    worker_settings, unavailable_semantics, opened_stores, failure,
):
    with Runtime(worker_settings):
        pass
    if failure == "owner":
        settings = worker_settings.model_copy(update={"owner_qq_id": "different-owner"})
        error = "OWNER_IDENTITY_MISMATCH"
    else:
        settings = worker_settings.model_copy(
            update={"erasure_ledger": worker_settings.erasure_ledger + ".missing"}
        )
        error = "ERASURE_LEDGER_MISSING"
    with pytest.raises(CoreError, match=error):
        Runtime(settings)
    assert unavailable_semantics == []
    assert len(opened_stores) == 2
    for store in opened_stores:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            store.one("SELECT 1")


@pytest.mark.parametrize(
    "module_name,entry",
    [("companion.__main__", "worker"), ("companion.life_worker", "life_worker")],
)
def test_semantic_worker_still_fails_closed_and_closes_store(
    worker_settings, unavailable_semantics, opened_stores, module_name, entry,
):
    module = importlib.import_module(module_name)
    with pytest.raises(CoreError, match="MEMORY_INDEX_UNAVAILABLE"):
        asyncio.run(getattr(module, entry)(worker_settings))
    assert len(unavailable_semantics) == 1
    assert len(opened_stores) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened_stores[0].one("SELECT 1")


def test_content_worker_finishes_queued_article_images_without_semantic_startup(
    core, unavailable_semantics, opened_stores, monkeypatch,
):
    from companion import content, content_worker
    from tests.support.article_vision import article
    from tests.support.content import enable, png

    service, _ = enable(core)
    owner_id, article_id = article(core, 1)
    settings = core.settings.model_copy(update={"memory_enabled": True, "content": service.options})
    clock = core.clock
    core.store.close()

    async def fetch(*args, **kwargs):
        return png(), "image/png"

    async def stop(*_args):
        raise asyncio.CancelledError()

    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "离线图片结果"}}]})

    original_provider_init = content.HTTPMediaProvider.__init__

    def offline_provider_init(provider, options):
        original_provider_init(provider, options, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(content.HTTPMediaProvider, "__init__", offline_provider_init)
    monkeypatch.setattr(content, "fetch_public_image", fetch)
    monkeypatch.setattr(content_worker, "Runtime", partial(Runtime, clock=clock))
    monkeypatch.setattr(Wakeup, "wait", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(content_worker.content_worker(settings))
    assert unavailable_semantics == []
    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert len(opened_stores) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened_stores[0].one("SELECT 1")
    with Runtime(settings, clock) as runtime:
        image = runtime.store.one("SELECT * FROM content_items WHERE kind='link_image'")
        assert image["state"] == "SUCCEEDED"
        assert (image["owner_event_id"], image["source_event_id"]) == (owner_id, article_id)
        assert runtime.store.one(
            'SELECT result_status FROM observations WHERE event_id=%s', (image["output_event_id"],)
        ) == {"result_status": "FETCHED"}
        assert runtime.store.one(
            'SELECT state FROM model_usage WHERE request_id=%s', (image["usage_request_id"],)
        ) == {"state": "SETTLED"}


def test_content_worker_restart_keeps_expired_inflight_image_unknown_without_retry(
    core, unavailable_semantics, opened_stores, monkeypatch,
):
    from companion import content, content_worker
    from tests.support.article_vision import article
    from tests.support.content import enable, png

    service, vision = enable(core)
    owner_id, article_id = article(core, 1)
    fetches, provider_calls = [], []

    async def fetch(*args, **kwargs):
        fetches.append(args)
        return png(), "image/png"

    async def die_after_reservation(row, data, mime):
        # Model a hard process exit after the durable reservation, before a receipt.
        reserved, duplicate = service._reserve(row, data, mime)
        assert not duplicate and reserved["state"] == "RUNNING"
        raise SystemExit("SYNTHETIC_PROCESS_EXIT")

    monkeypatch.setattr(content, "fetch_public_image", fetch)
    monkeypatch.setattr(service, "_analyze", die_after_reservation)
    with pytest.raises(SystemExit, match="SYNTHETIC_PROCESS_EXIT"):
        asyncio.run(service.run_once())
    image = core.store.one("SELECT * FROM content_items WHERE kind='link_image'")
    usage = core.store.one(
        'SELECT * FROM model_usage WHERE request_id=%s', (image["usage_request_id"],),
    )
    assert len(fetches) == 1 and vision.calls == []
    assert usage["state"] == "RESERVED" and usage["billed_micro_usd"] > 0
    settings = core.settings.model_copy(update={"memory_enabled": True, "content": service.options})
    clock = core.clock
    clock.advance(service.options.provider_timeout_seconds + 6)
    core.store.close()
    fetches.clear()

    async def forbidden_provider(*args, **kwargs):
        provider_calls.append(args)
        raise AssertionError("An interrupted image must not be dispatched again")

    async def stop(*_args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(content.HTTPMediaProvider, "process", forbidden_provider)
    monkeypatch.setattr(content_worker, "Runtime", partial(Runtime, clock=clock))
    monkeypatch.setattr(Wakeup, "wait", stop)
    for _ in range(2):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(content_worker.content_worker(settings))
    assert fetches == provider_calls == unavailable_semantics == []
    assert len(opened_stores) == 2
    for store in opened_stores:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            store.one("SELECT 1")
    with Runtime(settings, clock) as runtime:
        recovered = runtime.store.one(
            'SELECT * FROM content_items WHERE item_id=%s', (image["item_id"],),
        )
        assert recovered["state"] == "UNKNOWN"
        assert recovered["error_code"] == "CONTENT_PROCESSOR_LOST"
        assert recovered["output_event_id"] is None
        assert (recovered["owner_event_id"], recovered["source_event_id"]) == (owner_id, article_id)
        assert recovered["reserved_bytes"] == image["reserved_bytes"]
        assert runtime.store.one(
            'SELECT * FROM model_usage WHERE request_id=%s', (image["usage_request_id"],),
        ) == {**usage, "state": "UNKNOWN"}


def test_init_bootstraps_identity_without_semantic_index(
    worker_settings, unavailable_semantics, opened_stores, monkeypatch, capsys,
):
    from companion import __main__

    monkeypatch.setattr(Settings, "load", lambda: worker_settings)
    monkeypatch.setattr("sys.argv", ["companion", "init"])
    __main__.main()
    assert "identity and erasure ledger initialized" in capsys.readouterr().out
    assert unavailable_semantics == []
    assert len(opened_stores) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened_stores[0].one("SELECT 1")
