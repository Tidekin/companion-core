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
from companion_onebot_bridge.owner_memes import MemePump
from tests.support.native_meme import NativeCase, capture, native_item
from tests.support.next_memes_catalog import png


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
