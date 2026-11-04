"""Meme leaf uses signed Bridge custody and real native Core HTTP authorization."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import owner_authority, paid_budget
from companion.image_input import ImageInputPolicy
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.meme_runtime import MemeRuntime
from companion_onebot_bridge.meme_semantic import SemanticLibrary
from companion_onebot_bridge.meme_semantic_http import CaptionHTTP, SemanticHTTP
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.onebot_events import raw_event
from tests.support.owner_authority import activate
from tests.test_meme_runtime import RULE, item, public_transport
from tests.test_next_memes_catalog import metadata, png
from tests.test_next_memes_discovery import resolver
from tests.test_paid_model_gate import approved_live
from tests.test_selector_capabilities import child


@asynccontextmanager
async def native(core, tmp_path, *, public=None, caption=False):
    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    approved_live(core, tmp_path, **({"max_output_tokens": 4096,
        "meme_input": ImageInputPolicy(approval_sha256="e" * 64, estimated_tokens_per_image=100,
            max_bytes_per_image=4000000, max_pixels_per_image=16000000,
            max_images_per_request=5)} if caption else {}))
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    core.settings.expression_enabled = True
    core.settings.meme_discovery_enabled = True
    core.settings.meme_semantics_enabled = caption
    core.settings.meme_caption_daily_micro_usd = 50000
    key = Ed25519PrivateKey.generate()
    policy = OwnerGrantPolicy(owner_qq_id=core.settings.owner_qq_id,
        purposes=["chat", "expression", "meme"], authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capability_policies={"meme": owner_authority.capability_hashes(core.settings)["meme"]},
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
                 "identity_sha256": paid_budget.sha(
                     [core.settings.owner_qq_id, core.settings.bot_qq_id]),
                 "protocol_epoch": "native-meme-leaf"})
    authority = activate(core, policy)[0]["authority"]
    async with real_core_http(core) as port:
        cfg = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
            bot_qq_id=core.settings.bot_qq_id,
            bridge_token=core.settings.bridge_token.get_secret_value(), onebot_token="s" * 32,
            core_url=f"http://127.0.0.1:{port}", protocol_epoch=policy.capture.protocol_epoch,
            owner_authority=authority, capture_signing_key=key.private_bytes_raw().hex(),
            data_dir=str(tmp_path / "bridge"), meme_enabled=True, meme_discovery_enabled=True,
            meme_semantics_enabled=caption,
            meme_trusted_sources=(RULE,))
        with Spool(cfg) as spool:
            owner = OwnerRuntime(spool, clock=lambda: core.clock.now)
            owner._memes = MemeRuntime(owner, transport=public, resolver=resolver)
            async with private_http_client(cfg.core_url, cfg.bridge_token) as client:
                yield owner.memes, client


def capture(runtime, number=1):
    raw = json.dumps(raw_event(number, time=int(runtime.window.clock().timestamp()),
                               text="太棒了，成功了"))
    event = normalize(raw, runtime.config, now=runtime.window.clock().timestamp())
    runtime.window.persist_capture(event, raw, received_at=runtime.window.clock(),
                                   peer=("127.0.0.1", 3001))
    return event


def test_signed_catalog_unknown_pin_survives_restart(core, tmp_path):
    async def scenario():
        async with native(core, tmp_path) as (runtime, client):
            event = capture(runtime)
            digest = runtime.catalog.add(png(), metadata(), check=runtime._owner_check)
            assert await runtime.window.flush_one(client)
            offer = runtime.prepare_offer(event)
            assert offer["candidates"][0]["candidate_id"] == digest
            delivery = {**item(event, digest), "authority": runtime.authority}
            delivery["origin_event_id"] = delivery.pop("window_origin_event_id")
            assert runtime.prepare(delivery, check=runtime._owner_check).data == png()
            runtime.mark_sending(delivery, check=runtime._owner_check)
            runtime.receipt(delivery, "UNKNOWN")
            restarted = MemeRuntime(runtime.window)
            assert restarted.prepare(delivery, check=restarted._owner_check) is None
            with pytest.raises(ValueError, match="MEME_SEND_FENCED"):
                restarted.mark_sending(delivery, check=restarted._owner_check)
            assert core.store.rows("SELECT * FROM model_usage") == []
    run(scenario())


def test_caption_adapter_uses_real_http_and_independent_native_selector(core, tmp_path):
    calls = []

    async def scenario():
        async with native(core, tmp_path, public=public_transport(calls), caption=True) as pair:
            runtime, client = pair
            event = capture(runtime)
            assert await runtime.window.flush_one(client)
            core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE kind='chat'")
            submitted = await runtime.submit_missing(client, event["event_id"],
                {"offer_key": "meme:" + event["event_id"], "intent": "celebrate", "candidates": []},
                reply_revision=1)
            result = await runtime.run_once(client)
            assert result.get("status") == "completed", result
            sha = runtime.db.execute("SELECT sha FROM meme_assets").fetchone()[0]
            attempt = runtime.db.execute(
                "SELECT attempt_key FROM meme_runtime_results").fetchone()[0]
            http = SemanticHTTP(runtime.window, client, check=runtime._owner_check)
            identity, embedding = await http.profile()
            library = SemanticLibrary(runtime.catalog, identity, embedding)
            request = library._request(sha, {"event_id": event["event_id"], "reply_revision": 1,
                "discovery_job_id": submitted["job_id"], "attempt_key": attempt})
            adapter = CaptionHTTP(http, runtime.catalog)
            async def check():
                runtime._owner_check()

            pending = await library.advance(sha, adapter, request.context,
                check=check, commit_check=runtime._owner_check)
            assert pending["state"] == "PENDING", pending
            original = runtime.db.execute(
                "SELECT job_id FROM meme_caption_bindings").fetchone()[0]
            report = await child(core, tmp_path, "meme_caption", 1)
            assert len(report["calls"]) == 1, report
            outcome = await adapter.get(original)
            assert outcome["state"] == "SUCCEEDED", outcome
            assert outcome["usage"]["state"] == "SETTLED"
            assert await adapter.submit(request) == outcome
            assert len(core.store.rows("SELECT * FROM model_usage")) == 1
            assert core.store.rows("SELECT * FROM outbox") == []
    run(scenario())


def test_signed_source_supersession_and_stop_fence_catalog(core, tmp_path):
    async def scenario():
        async with native(core, tmp_path) as (runtime, client):
            event = capture(runtime)
            assert await runtime.window.flush_one(client)
            offer = runtime.prepare_offer(event)
            capture(runtime, 2)
            assert runtime.prepare_offer(event) == offer
            with pytest.raises(ValueError, match="MEME_INPUT_CANCELLED"):
                runtime._event_check(event["event_id"])
            runtime.window.stop()
            with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                runtime._owner_check()
            assert core.store.rows("SELECT * FROM model_usage") == []
    run(scenario())


def test_offer_rejects_changed_payload_and_unretained_capture(core, tmp_path):
    async def scenario():
        async with native(core, tmp_path) as (runtime, client):
            event = capture(runtime)
            receipt = json.loads(runtime.db.execute(
                "SELECT capture_json FROM window_inputs").fetchone()[0])
            changed = {**event, "payload": {**event["payload"], "text": "抱抱"}}
            with pytest.raises(ValueError, match="MEME_INPUT_INVALID"):
                runtime.prepare_offer(changed)
            with pytest.raises(ValueError, match="MEME_INPUT_INVALID"):
                runtime.prepare_offer(event, {**receipt, "signature": "0" * 128})
            assert runtime.prepare_offer(event, receipt)["intent"] == "celebrate"
    run(scenario())


def test_discovery_real_http_retains_one_original_budget(core, tmp_path):
    calls = []

    async def scenario():
        async with native(core, tmp_path, public=public_transport(calls)) as (runtime, client):
            event = capture(runtime)
            assert await runtime.window.flush_one(client)
            offer = runtime.prepare_offer(event)
            submitted = await runtime.submit_missing(client, event["event_id"], offer,
                                                     reply_revision=1)
            assert submitted["state"] == "QUEUED", submitted
            assert await runtime.run_once(client) == {"status": "IDLE"}
            core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE kind='chat'")
            result = await runtime.run_once(client)
            assert result.get("status") == "completed", result
            assert len(result["accepted"]) == 1, result
            count = len(calls)
            assert count > 0
            assert await MemeRuntime(runtime.window).run_once(client) == {"status": "IDLE"}
            assert len(calls) == count
            assert runtime.db.execute(
                "SELECT state FROM meme_runtime_results").fetchall() == [("SETTLED",)]
            assert core.store.rows("SELECT * FROM model_usage") == []
    run(scenario())


@pytest.mark.parametrize("fence", ["stop", "body"])
def test_final_http_hook_cannot_bypass_native_fence(core, tmp_path, fence):
    async def scenario():
        async with native(core, tmp_path) as (runtime, client):
            event = capture(runtime)
            assert await runtime.window.flush_one(client)
            offer = runtime.prepare_offer(event)

            async def mutate(request):
                if request.url.path.endswith("/discovery/submit"):
                    if fence == "stop":
                        runtime.window.stop()
                    else:
                        request._content = b'{}'

            client.event_hooks["request"].append(mutate)
            code = "OWNER_RUNTIME_STOPPED" if fence == "stop" else "MEME_CORE_REQUEST_CHANGED"
            with pytest.raises(ValueError, match=code):
                await runtime.submit_missing(client, event["event_id"], offer, reply_revision=1)
            assert core.store.rows("SELECT * FROM jobs WHERE kind='meme_discovery'") == []
    run(scenario())


def test_lost_settlement_ack_replays_original_receipt_after_local_stop(core, tmp_path):
    calls, results = [], []

    async def scenario():
        async with native(core, tmp_path, public=public_transport(calls)) as (runtime, client):
            event = capture(runtime)
            assert await runtime.window.flush_one(client)
            await runtime.submit_missing(client, event["event_id"], runtime.prepare_offer(event),
                                         reply_revision=1)
            core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE kind='chat'")

            async def lose_ack(response):
                if response.request.url.path.endswith("/result"):
                    results.append(json.loads(response.request.content))
                    if len(results) <= 2:
                        raise httpx.ReadTimeout("synthetic lost settlement ACK")

            client.event_hooks["response"].append(lose_ack)
            outcome = await runtime.run_once(client)
            assert outcome["status"] == "RESULT_PENDING", (outcome, results, len(calls),
                core.store.rows("SELECT kind,state,last_error_code FROM jobs"))
            count = len(calls)
            assert count > 0
            runtime.window.stop()
            restarted = MemeRuntime(runtime.window)
            assert await restarted.run_once(client) == {
                "status": "RESULT_RECONCILED", "state": "SETTLED"}
            assert results == [results[0]] * 3
            assert len(calls) == count
            assert (await restarted.run_once(client))["status"] == "CANCELLED"
    run(scenario())
