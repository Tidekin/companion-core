import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx

from companion import private_life
from companion.autonomous_selector import native_selector
from companion.core import Core
from companion.image_input import ImageInputPolicy
from companion.model import ModelClient
from companion.reply_segments import SegmentedReplyConfig
from companion_meme_protocol import OFFER_WAIT_SECONDS
from companion_onebot_bridge import app
from companion_onebot_bridge.meme_catalog import key_hash
from companion_onebot_bridge.owner_memes import MemePump
from tests.conftest import run
from tests.support.meme_core import CAPTION
from tests.support.meme_runtime import RULE, public_transport
from tests.support.native_meme_entry import native_entry
from tests.support.next_memes_catalog import png
from tests.support.next_memes_discovery import resolver
from tests.support.onebot_events import raw_event
from tests.support.private_companion import companion_options


def check_signed_caption_and_delivery(
    core, tmp_path, handoff, deferred, delayed, explicit=False, segmented=False, segment_cap=3,
):
    now = datetime.now(UTC).replace(microsecond=0)
    core.clock.now = now
    public_calls, visual_calls, chat_calls = [], [], []
    selected = []
    number, text = 1, "太棒了，成功了"
    asset = png()
    asset_sha = hashlib.sha256(asset).hexdigest()
    caption = {
        **CAPTION,
        "description": "绿色庆祝图片，适合分享成功的开心心情",
        "visible_text": "",
        "tone": "celebratory",
        "suitable": ["celebrate", "playful"],
        "use_when": ["庆祝成功"],
        "tags": ["庆祝", "成功", "开心", "表情包"],
    }

    async def source(ws):
        await ws.send(json.dumps(raw_event(number, text=text, time=int(now.timestamp()))))
        await ws.wait_closed()

    def model_reply(request):
        body = json.loads(request.content)
        messages = body["messages"]
        if isinstance(messages[0]["content"], list):
            visual_calls.append(body)
            assert body["response_format"] == {"type": "json_object"}
            assert len(messages[0]["content"]) == 2
            assert messages[0]["content"][1]["image_url"]["url"].startswith(
                "data:image/png;base64,",
            )
            answer = json.dumps(caption, ensure_ascii=False)
        else:
            chat_calls.append(messages)
            offers = []
            for message in messages:
                content = message["content"]
                if isinstance(content, str) and "候选数据：\n" in content:
                    offers.append(json.JSONDecoder().raw_decode(
                        content.split("候选数据：\n", 1)[1],
                    )[0])
            answer = "合成文字回复。"
            if offers:
                assert len(offers) == 1 and 1 <= len(offers[0]["candidates"]) <= 8
                candidate = offers[0]["candidates"][0]["candidate_id"]
                assert candidate == asset_sha
                selected.append((offers[0]["offer_key"], candidate))
                if segmented:
                    answer = "第一段合成文字。\n第二段合成文字。"
                answer += "⬡" + candidate + "⬡"
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": answer}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        })

    options = companion_options(core)
    options["core_options"].update(
        meme_semantics_enabled=True,
        meme_discovery_enabled=True,
        meme_discovery_daily_download_bytes=32 * 1024 * 1024,
        meme_discovery_daily_storage_bytes=8 * 1024 * 1024,
        meme_caption_daily_micro_usd=50000,
        expression_selection_probability=0.0 if explicit else 1.0,
    )
    options["paid_options"].update(
        max_output_tokens=4096,
        meme_input=ImageInputPolicy(
            approval_sha256="e" * 64,
            estimated_tokens_per_image=100,
            max_bytes_per_image=4000000,
            max_pixels_per_image=16000000,
            max_images_per_request=5,
        ),
    )
    if segmented:
        options["core_options"]["segmented_reply"] = SegmentedReplyConfig(
            mode="semantic", max_segments=segment_cap, interval_ms=0)

    async def scenario():
        nonlocal number, text
        async with native_entry(
            core, tmp_path, source, options=options, rules=(RULE,),
            public=public_transport(public_calls, semantic=False), resolver=resolver,
        ) as (window, client, protocol, handshakes, sends), httpx.AsyncClient(
            transport=httpx.MockTransport(model_reply),
        ) as provider:
            api_core = window.api_core
            assert api_core is not core and api_core.store is not core.store
            assert all(getattr(core.settings, feature + "_enabled")
                       for feature in private_life.FEATURES)
            runtime = window.runtime.memes
            pump = MemePump(window.runtime, client)

            async def delayed_authority_hops(request):
                # The native offer has several independently authorized HTTP hops.
                # Keep all those gates, and exercise more than the old 1.5 s total.
                if request.url.path.startswith(("/internal/v1/owner-authority/",
                                                "/internal/v1/memes/")):
                    await asyncio.sleep(0.4)

            async def unavailable_query(request):
                if request.url.path == "/internal/v1/memes/query":
                    raise httpx.ReadTimeout("synthetic unavailable optional query", request=request)

            if deferred:
                client.event_hooks["request"].append(unavailable_query)

            async def admit_current():
                await window.capture_once(client, protocol)
                stop, wake = asyncio.Event(), asyncio.Event()

                def accepted(payload):
                    assert payload["payload"]["text"] == text
                    stop.set()
                    wake.set()

                await app.upload(client, window.spool, wake, stop,
                    SimpleNamespace(accepted=accepted), runtime=window.runtime, memes=pump)
                if delayed:
                    client.event_hooks["request"].append(delayed_authority_hops)
                event_id = pump.latest()[0]
                row = window.db.execute(
                    "SELECT state,capture_json FROM window_inputs WHERE event_id=?", (event_id,),
                ).fetchone()
                assert row[0] == "CAPTURED"
                signed = json.loads(row[1])
                assert signed["kind"] == "authenticated_onebot_ws" and signed["signature"]
                assert window.db.execute(
                    "SELECT state FROM inbox WHERE key=?", (event_id,),
                ).fetchone()[0] == "accepted"
                task = pump.query_task
                assert task is not None
                pump.tick(idle=False)
                await task
                if delayed:
                    client.event_hooks["request"].remove(delayed_authority_hops)
                pump.tick(idle=False)
                offered = window.db.execute(
                    "SELECT state,offer_json FROM meme_runtime_semantic_offers WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                expected = "DEFERRED" if deferred and number == 1 else "ACCEPTED"
                assert offered is not None and offered[0] == expected, offered
                assert window.db.execute(
                    "SELECT state FROM window_inputs WHERE event_id=?", (event_id,),
                ).fetchone()[0] == "CAPTURED"
                return event_id, json.loads(offered[1]) if offered[1] else None

            try:
                with Core(core.settings, ModelClient(core.settings, provider),
                          core.clock) as worker:
                    assert worker.store is not api_core.store and worker.store is not core.store
                    caption_lane = next(lane for lane in native_selector(worker,
                        "meme-integration").lanes if lane.name == "meme_caption")
                    first, offer = await admit_current()
                    assert (offer is None if deferred else offer["candidates"] == [])
                    assert public_calls == visual_calls == []
                    if deferred:
                        client.event_hooks["request"].remove(unavailable_query)
                        assert api_core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                            ("meme_offer:" + first,)) is None
                        core.clock.advance(OFFER_WAIT_SECONDS)
                    assert await worker.run_once("meme-integration-chat")
                    assert (await window.send(client, protocol))["worked"]
                    assert len(sends) == 1 and len(sends[0]["message"]) == 1
                    assert (await pump.state(pump.latest()))["terminal"]

                    pump.next_run = 0
                    pump.tick(idle=True)
                    await pump.task
                    submitted = core.store.one("SELECT state FROM jobs WHERE kind='meme_discovery'")
                    assert submitted is not None, "optional query failure stranded an empty catalog"
                    assert submitted["state"] == "SUCCEEDED"
                    assert len(public_calls) == 5
                    assert core.store.one(
                        "SELECT state,queries,requests FROM meme_discovery_reservations",
                    ) == {
                        "state": "SETTLED", "queries": 1, "requests": 5,
                    }
                    assert (await runtime.run_once(client))["status"] == "CAPTION_PENDING"
                    assert visual_calls == []
                    if handoff:
                        old_caption = core.store.one("SELECT job_id FROM meme_captions")
                        number, text = 2, "又搞定了，太棒了"
                        replacement, empty = await admit_current()
                        assert empty["candidates"] == []
                        assert await worker.run_once("meme-integration-replacement-chat")
                        assert (await window.send(client, protocol))["worked"]
                        assert len(sends) == 2 and visual_calls == []
                        assert (await caption_lane.run()).progressed
                        assert core.store.one(
                            'SELECT state FROM jobs WHERE job_id=%s', (old_caption["job_id"],),
                        )["state"] == "CANCELLED"
                        next_discovery = await runtime.submit_missing(
                            client, replacement, runtime.missing_offer(replacement),
                            reply_revision=2,
                        )
                        assert next_discovery.get("state") == "QUEUED", next_discovery
                        rediscovered = await runtime.run_once(client)
                        assert rediscovered.get("pending_caption") == [asset_sha], rediscovered
                        assert (await runtime.run_once(client))["status"] == "CAPTION_PENDING"
                        assert (await runtime.run_once(client))["status"] == "CAPTION_PENDING"
                        old = window.db.execute(
                            "SELECT state,outcome_json,usage_json FROM meme_caption_bindings "
                            "WHERE job_id=?", (old_caption["job_id"],),
                        ).fetchone()
                        assert old[0] == "CANCELLED" and old[2] is None
                        assert json.loads(old[1])["usage"] is None
                        assert window.db.execute(
                            "SELECT COUNT(*) FROM meme_caption_bindings",
                        ).fetchone()[0] == 2
                        assert core.store.one(
                            "SELECT COUNT(*) n FROM model_usage WHERE scenario='meme_caption'",
                        )["n"] == 0
                    assert (await caption_lane.run()).progressed
                    assert len(visual_calls) == 1
                    assert (await runtime.run_once(client))["status"] == "CAPTION_READY"
                    assert (await runtime.run_once(client))["status"] == "SEMANTIC_READY"
                    stored = window.db.execute(
                        "SELECT data,metadata FROM meme_assets WHERE sha=?", (asset_sha,),
                    ).fetchone()
                    assert stored[0] == asset
                    acquisition = json.loads(json.loads(stored[1])["acquisition_json"])
                    assert acquisition["archive_member"] == ""
                    assert set(acquisition["source_files"]) == {"manifest", "license", "listing"}
                    assert acquisition["fetch"]["body_sha256"] == asset_sha
                    assert acquisition["source_files"]["license"]["body_sha256"] == (
                        RULE["license_sha256"]
                    )
                    assert window.db.execute(
                        "SELECT state FROM meme_runtime_sources WHERE sha=? ORDER BY rowid DESC",
                        (asset_sha,),
                    ).fetchone()[0] == "INDEXED"
                    assert window.db.execute(
                        "SELECT COUNT(*) FROM meme_semantic_vectors",
                    ).fetchone()[0] == 1
                    caption_row = core.store.one(
                        'SELECT * FROM meme_captions ORDER BY record_sequence DESC LIMIT 1',
                    )
                    assert json.loads(caption_row["source_json"])["acquisition"] == acquisition
                    assert json.loads(caption_row["result_json"])["sampled_frames"][
                        "coverage"
                    ] == "complete"
                    usage = core.store.one(
                        "SELECT * FROM model_usage WHERE scenario='meme_caption'",
                    )
                    assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] > 0
                    assert caption_row["usage_request_id"] == usage["request_id"]
                    cached = window.db.execute("SELECT * FROM meme_caption_cache").fetchone()

                    number = 2 + handoff
                    text = "请发一张表情包" if explicit else "今天又成功了，太开心了"
                    second, offer = await admit_current()
                    assert 1 <= len(offer["candidates"]) <= 8
                    assert offer["candidates"][0]["candidate_id"] == asset_sha
                    assert await worker.run_once("meme-integration-selection")
                    assert selected == [("meme:" + second, asset_sha)]
                    assert (await window.send(client, protocol))["worked"]
                    if segmented:
                        # Independent leases and real OneBot requests, in group order.
                        texts = (["第一段合成文字。", "第二段合成文字。"] if segment_cap == 3 else
                                 ["第一段合成文字。\n第二段合成文字。"])
                        assert sends[-1]["message"] == [
                            {"type": "text", "data": {"text": texts[0]}}]
                        for part in texts[1:]:
                            assert (await window.send(client, protocol))["worked"]
                            assert sends[-1]["message"] == [
                                {"type": "text", "data": {"text": part}}]
                        assert (await window.send(client, protocol))["worked"]
                        assert len(sends[-1]["message"]) == 1
                    extra = len(texts) if segmented else 0
                    assert len(sends) == 2 + handoff + extra
                    image = sends[-1]["message"][0 if segmented else 1]
                    assert image["type"] == "image"
                    encoded = image["data"]["file"].removeprefix("base64://")
                    assert base64.b64decode(encoded) == asset
                    if not segmented:
                        assert "⬡" not in sends[-1]["message"][0]["data"]["text"]
                    assert window.db.execute(
                        "SELECT state FROM meme_pins WHERE key=?", (key_hash("meme:" + second),),
                    ).fetchone()[0] == "SENT"

                    number, text = 3 + handoff, "聊聊今天的茶吧"
                    _, absent = await admit_current()
                    assert absent is None
                    assert await worker.run_once("meme-integration-text-only")
                    assert (await window.send(client, protocol))["worked"]
                    assert len(sends) == 3 + handoff + extra
                    assert len(chat_calls) == len(handshakes) == 3 + handoff
                    assert len(sends[-1]["message"]) == 1
                    assert len(visual_calls) == 1 and len(public_calls) == 5 * (1 + handoff)
                    assert tuple(window.db.execute(
                        "SELECT * FROM meme_caption_cache",
                    ).fetchone()) == tuple(cached)
                    assert len(core.store.rows(
                        "SELECT * FROM model_usage WHERE scenario='meme_caption'",
                    )) == 1
                    assert window.db.execute(
                        "SELECT state,count(*) FROM window_attempts GROUP BY state",
                    ).fetchall() == [("ACK", 3 + handoff + extra)]
                    vector = json.loads(window.db.execute(
                        "SELECT vector_json FROM meme_semantic_vectors").fetchone()[0])
                    assert len(vector) == 512 and any(value != 0 for value in vector)
                    queries = [json.loads(row["value"]) for row in core.store.rows(
                        "SELECT value FROM runtime_meta WHERE key LIKE 'meme_query:%%'")]
                    assert len(queries) == 3 + handoff - deferred
                    embedded = [query["embedding"] for query in queries if query["embedding"]]
                    assert len(embedded) == 2 + handoff - deferred
                    assert all(value["local"] is True and value["dimensions"] == 512
                               for value in embedded)
            finally:
                await pump.close()

    run(scenario())
