"""Core-mode byte adapter faults and native transport custody; no real provider I/O."""

import asyncio
import base64
import builtins
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from companion import owner_authority
from companion_meme_protocol import digest
from companion_onebot_bridge import journal
from companion_onebot_bridge.meme_transport import key_hash
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.owner_memes import MemePump
from tests.conftest import run
from tests.support.native_meme import NativeCase, capture, native_item
from tests.test_next_memes_catalog import png


@pytest.fixture
def transport_case(core, tmp_path, monkeypatch):
    core.settings = core.settings.model_copy(update={"meme_catalog_owner": "core"})
    native = NativeCase(core, tmp_path, rules=(), semantic=True, catalog_owner="core")
    owner = native.owner
    owner.config = native.spool.config = replace(
        owner.config,
        core_url="http://127.0.0.1:8765",
        onebot_http_url="http://127.0.0.1:3000",
        onebot_ws_url="ws://127.0.0.1:3001",
    )
    original_import = builtins.__import__

    def isolated(name, *args, **kwargs):
        if name in {"meme_runtime", "meme_catalog"}:
            raise AssertionError("Core mode imported the Bridge business catalog")
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(builtins, "__import__", isolated)
        runtime = owner.memes
        assert owner.memes is runtime
    assert MemePump(owner, None).runtime is None
    tables = {
        r[0] for r in native.spool.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert not tables & {
        "meme_assets",
        "meme_offers",
        "meme_runtime_offers",
        "meme_caption_cache",
        "meme_semantic_vectors",
        "meme_runtime_sources",
        "meme_caption_bindings",
    }
    data = png()
    event = capture(runtime)
    item = {
        **native_item(runtime, event, digest_bytes(data)),
        "action_type": "qq.private.reply",
        "lease_until": (owner.clock() + timedelta(seconds=60)).isoformat(),
        "lease_token": "lease_" + "a" * 32,
        "conversation_platform_session_key": owner.config.origin,
    }
    value = {
        "message_id": item["message_id"],
        "delivery_key": item["delivery_key"],
        "event_id": event["event_id"],
        "reply_revision": 1,
        "authority": runtime.authority,
        "content_sha256": journal.digest(item["content"]),
        "lease_until": item["lease_until"],
        "asset_sha256": digest_bytes(data),
        "source_sha256": "b" * 64,
        "custody_sha256": "c" * 64,
        "record_sha256": "d" * 64,
        "caption_sha256": "e" * 64,
        "binding_sha256": "f" * 64,
        "asset_id": digest(["c" * 64, "b" * 64, digest_bytes(data)]),
        "mime": "image/png",
        "byte_length": len(data),
        "data_base64": base64.b64encode(data).decode(),
    }
    try:
        yield native, runtime, item, value, data
    finally:
        native.spool.close()


def digest_bytes(data):
    import hashlib

    return hashlib.sha256(data).hexdigest()


def handler(native, value, *, after=None):
    def handle(request):
        if request.url.path.endswith("/owner-authority/check"):
            body = json.loads(request.content)
            view = owner_authority.current(
                native.core.store,
                native.core.settings,
                native.core.clock,
                authority=body["authority"],
                purpose=body["purpose"],
            )
            return httpx.Response(
                200,
                json={
                    "approved": True,
                    "authority": view.authority.as_dict(),
                    "purpose": body["purpose"],
                    "capability_policies": dict(view.policy.capability_policies),
                    "budget_journal_id": view.journal_id,
                    "capture": view.policy.capture.model_dump(mode="json"),
                    "expires_at": view.policy.expires_at.isoformat()
                    if view.policy.expires_at
                    else None,
                },
            )
        assert request.url.path.endswith("/asset")
        if after:
            after()
        return httpx.Response(200, json=value)

    return handle


def test_core_only_prepare_and_original_pin_unknown_late_ack(transport_case):
    native, runtime, item, value, data = transport_case

    async def scenario():
        async with private_http_client(
            runtime.config.core_url,
            runtime.config.bridge_token,
            transport=httpx.MockTransport(handler(native, value)),
        ) as client:
            image = await runtime.prepare(client, item, check=lambda: None)
            assert image.data == data
            before = native.spool.db.execute("SELECT * FROM meme_runtime_bindings").fetchall()
            assert len(before) == 1
            runtime.mark_sending(item, check=lambda: None)
            runtime.receipt(item, "UNKNOWN")
            with pytest.raises(ValueError, match="MEME_SEND_FENCED"):
                await runtime.prepare(client, item, check=lambda: None)
            runtime.receipt(item, "SENT")
            sent = native.spool.db.execute("SELECT sent_at FROM meme_pins").fetchone()[0]
            native.core.clock.now += timedelta(seconds=20)
            runtime.receipt(item, "SENT")
            assert native.spool.db.execute("SELECT sent_at FROM meme_pins").fetchone()[0] == sent
            assert (
                native.spool.db.execute("SELECT * FROM meme_runtime_bindings").fetchall() == before
            )

    run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        "bytes",
        "mime",
        "length",
        "source",
        "asset",
        "authority",
        "event",
        "revision",
        "content",
        "lease",
        "extra",
        "stop",
        "new_input",
        "cookie",
        "path",
    ],
)
def test_core_asset_faults_never_prepare_or_release_original_custody(transport_case, change):
    native, runtime, item, value, _ = transport_case
    value = dict(value)
    if change == "bytes":
        value["data_base64"] = base64.b64encode(png("blue")).decode()
    elif change == "mime":
        value["mime"] = "image/gif"
    elif change == "length":
        value["byte_length"] = True
    elif change in {"source", "asset"}:
        value[change + "_sha256" if change == "source" else "asset_id"] = "a" * 64
    elif change in {"event", "content"}:
        value[{"event": "event_id", "content": "content_sha256"}[change]] = "x"
    elif change == "revision":
        value["reply_revision"] = True
    elif change == "authority":
        value["authority"] = {}
    elif change == "lease":
        value["lease_until"] = (native.core.clock.now + timedelta(seconds=600)).isoformat()
    elif change == "extra":
        value["file_path"] = "private.png"

    def after():
        if change == "stop":
            runtime.window.stop()
        if change == "new_input":
            capture(runtime, 43)

    async def scenario():
        async with private_http_client(
            runtime.config.core_url,
            runtime.config.bridge_token,
            transport=httpx.MockTransport(handler(native, value, after=after)),
        ) as client:
            if change in {"cookie", "path"}:

                async def tamper(request):
                    if request.url.path.endswith("/asset"):
                        if change == "cookie":
                            request.headers["Cookie"] = "changed=1"
                        else:
                            request.url = request.url.copy_with(path="/other")

                client.event_hooks["request"].append(tamper)
            with pytest.raises(ValueError):
                await runtime.prepare(client, item, check=lambda: None)
        assert not native.spool.db.execute("SELECT 1 FROM meme_pins").fetchone()
        assert not native.spool.db.execute("SELECT 1 FROM meme_runtime_bindings").fetchone()

    run(scenario())


@pytest.mark.parametrize("kind", ["meme", "generated"])
@pytest.mark.parametrize("change", ["stop", "expiry"])
def test_asset_request_rechecks_lease_after_awaiting_approval(
    transport_case, monkeypatch, kind, change,
):
    from companion_onebot_bridge.generated_image_http import read_image
    from companion_onebot_bridge.meme_asset_http import read_asset

    native, runtime, item, value, _ = transport_case
    owner = runtime.window
    if kind == "generated":
        item["content"]["generated_image"] = {
            "image_id": "gimg_" + "a" * 32, "sha256": value["asset_sha256"],
            "byte_length": value["byte_length"], "mime": value["mime"],
        }

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def await_approval(client, *, purpose):
            assert purpose == ("expression" if kind == "generated" else "meme")
            entered.set()
            await release.wait()
            return {"expires_at": None}

        monkeypatch.setattr(owner, "check", await_approval)
        transport = httpx.MockTransport(lambda request: pytest.fail("Fenced bytes dispatched"))
        async with private_http_client(owner.config.core_url, owner.config.bridge_token,
                                       transport=transport) as client:
            read = read_image if kind == "generated" else read_asset
            pending = asyncio.create_task(read(owner, client, item, check=lambda: None))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                if change == "stop":
                    owner.stop()
                else:
                    native.core.clock.advance(60)
            finally:
                release.set()
                with pytest.raises(ValueError, match="STOPPED|LEASE_LOST"):
                    await pending

    run(scenario())


def test_stop_and_cancel_keep_sending_pin_unknown(transport_case):
    native, runtime, item, value, _ = transport_case

    async def scenario():
        async with private_http_client(
            runtime.config.core_url,
            runtime.config.bridge_token,
            transport=httpx.MockTransport(handler(native, value)),
        ) as client:
            await runtime.prepare(client, item, check=lambda: None)
        runtime.mark_sending(item, check=lambda: None)
        runtime.window.stop()
        runtime.cancel(item["content"]["image"]["offer_key"])
        pin = native.spool.db.execute(
            "SELECT state FROM meme_pins WHERE key=?",
            (key_hash(item["content"]["image"]["offer_key"]),),
        ).fetchone()
        assert pin[0] == "UNKNOWN"
        with pytest.raises(ValueError, match="MEME_RECEIPT_CONFLICT"):
            runtime.receipt(item, "NOT_SENT")

    run(scenario())
