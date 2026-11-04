"""Unknown image descriptors retain native source evidence and sibling input."""

import asyncio
import hashlib
import json
import pickle
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from companion import qq_rich
from companion_delivery_protocol.authority import capture_signing_payload
from companion_onebot_bridge import capture, pending_media
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.images import original_images
from companion_onebot_bridge.rich_input import input_digest
from companion_onebot_bridge.spool import encode
from tests.conftest import run
from tests.native_continuous_support import frame, host, until
from tests.sdk_reader_fixture import answer
from tests.support.onebot_events import raw_event
from tests.test_onebot_images import image_segment
from tests.test_prepare_owner_input import native_spool, window

UNAVAILABLE = "这张图片目前读不到，请重新发图或用文字描述一下。"
BAD_IMAGES = [
    image_segment(subtype=99),
    image_segment(subtype="1"),
    image_segment("https://untrusted.invalid/private?token=not-public"),
    {"type": "image", "data": {"url": "https://gchat.qpic.cn/original", "type": "flash"}},
    {"type": "image", "data": {"file": "file:///private/owner.png"}},
    {"type": "image", "data": {}},
    {"type": "image", "data": None},
    {"type": "image"},
    {"type": "image", "data": {}, "extra": "not-owner-text"},
]


@pytest.mark.parametrize("bad", BAD_IMAGES)
def test_native_prepare_preserves_text_and_supported_sibling_sources(core, tmp_path, bad):
    async def scenario():
        async with window(core, tmp_path, media=True, image_count=2) as (client, config, plan):
            spool, runtime = native_spool(core, tmp_path, config, plan)
            good = image_segment("https://gchat.qpic.cn/original", subtype=1)
            raw = raw_event(time=int(core.clock.now.timestamp()), message=[
                {"type": "text", "data": {"text": "原文在前"}}, bad, good,
                {"type": "text", "data": {"text": "原文在后"}}, good,
            ])
            source = encode(raw)
            try:
                with pytest.raises(ValueError):
                    original_images(raw["message"])
                prepared = await capture.prepare_owner_input(runtime, client, None, source,
                    core.clock.now, ("127.0.0.1", 3001))
                event, media = prepared["event"], prepared["media"]
                assert event["payload"]["text"] == "原文在前原文在后"
                assert event["event_id"] == normalize(source, runtime.config)["event_id"]
                assert event["source_event_id"] == normalize(source, runtime.config)[
                    "source_event_id"]
                assert event["payload"]["rich"]["owner_text_positions"] == [0, 3]
                assert event["payload"]["rich"]["segments"][0] == {
                    "index": 1, "type": "image", "actor": "UNVERIFIED",
                    "coverage": "UNSUPPORTED"}
                assert [(item["index"], item["slot"]) for item in media["images"]] == [
                    (2, 0), (4, 1)]
                expected_hash = hashlib.sha256(encode(good).encode()).hexdigest()
                assert all(item["source_sha256"] == expected_hash
                           for item in media["images"])
                assert "private?token" not in encode(event)
                assert "not-owner-text" not in encode(event)
                assert runtime.persist_capture(event, source, received_at=core.clock.now,
                                               peer=("127.0.0.1", 3001), media=media)
                receipt = json.loads(spool.db.execute(
                    "SELECT capture_json FROM window_inputs").fetchone()[0])
                assert receipt["input_sha256"] == input_digest(event["payload"])
                assert receipt["frame_sha256"] == hashlib.sha256(source.encode()).hexdigest()
                Ed25519PublicKey.from_public_bytes(bytes.fromhex(
                    runtime.config.capture_public_key)).verify(bytes.fromhex(receipt["signature"]),
                    capture_signing_payload({key: value for key, value in receipt.items()
                                             if key != "signature"}))
                queued = pending_media._execute(
                    runtime, "SELECT * FROM automatic_media").fetchone()
                assert [(item.index, item.slot) for item in pending_media.manifest(runtime, queued)
                        ] == [(2, 0), (4, 1)]
                assert await runtime.flush_one(client)
                assert not await runtime.flush_one(client)
                assert len(core.store.rows("SELECT * FROM events")) == 1
                assert not core.store.rows("SELECT * FROM model_usage")
                assert not core.store.rows("SELECT * FROM captured_images")
            finally:
                spool.close()
    run(scenario())


async def run_worker(core, tmp_path, number, response):
    job = core.store.one("SELECT * FROM jobs ORDER BY rowid DESC LIMIT 1")
    config_path, output = tmp_path / f"recovery-{number}.pickle", tmp_path / f"wire-{number}.json"
    config_path.write_bytes(pickle.dumps((core.settings, core.clock.now, job["job_id"],
                                         [answer(response)], "SUCCEEDED", str(output))))
    child = await asyncio.create_subprocess_exec(sys.executable, "-B",
        str(Path(__file__).with_name("reader_sdk_worker.py")), str(config_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        logs, _ = await asyncio.wait_for(child.communicate(), 45)
        assert child.returncode == 0, logs.decode(errors="replace")
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    return json.loads(output.read_text(encoding="utf-8"))


def test_owner_ws_survives_unknown_images_and_replay_with_independent_worker(core, tmp_path):
    outbound = asyncio.Queue()
    sources = []

    async def source(ws):
        sender = asyncio.create_task(send_frames(ws))
        try:
            await ws.wait_closed()
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    async def send_frames(ws):
        while True:
            await ws.send(await outbound.get())

    async def scenario():
        async with host(core, tmp_path, source,
            core_options={"rich_input_scope": "references_v1"},
            bridge_options={"rich_input_scope": "references_v1"},
            independent_api=True,
        ) as (active, handshakes, sends, calls):
            active.start(upload=True, deliver=False, worker=False)
            messages = [
                [{"type": "text", "data": {"text": "继续处理这段原文"}}, BAD_IMAGES[0]],
                [BAD_IMAGES[6]],
                [{"type": "text", "data": {"text": "下一条仍正常"}}],
            ]
            replies = ["已收到这段原文。", UNAVAILABLE, "下一条也收到了。"]
            for number, (message, reply) in enumerate(zip(messages, replies, strict=True), 1):
                value = frame(number, message=message)
                sources.append(value)
                await outbound.put(value)
                await until(lambda count=number: len(core.store.rows(
                    "SELECT * FROM events")) == count,
                            tasks=active.tasks)
                requests = await run_worker(core, tmp_path, number, reply)
                assert len(requests) == 1
                context = requests[0]["messages"]
                assert not any(isinstance(item["content"], list) for item in context)
                if number < 3:
                    assert "UNSUPPORTED" in encode(context)
                    assert qq_rich.RULES in context[0]["content"]
                if number == 1:
                    assert context[-1]["content"] == "继续处理这段原文"
                assert await active.pump()
                assert active.attempts() == {"ACK": number}
                assert active.runtime.row()["state"] == "ACTIVE"
                assert all(not task.done() for task in active.tasks)
            assert len(handshakes) == 1
            assert [item["message"][0]["data"]["text"] for item in sends] == replies
            assert calls == []  # Models ran only in the independent synthetic child.
            original_receipts = list(active.db.execute(
                "SELECT event_id,capture_json FROM window_inputs ORDER BY rowid"))
            usage = core.store.rows("SELECT * FROM model_usage")
            assert len(usage) == 3 and all(item["state"] == "SETTLED" for item in usage)
            assert not core.store.rows("SELECT * FROM content_items")
            assert not core.store.rows("SELECT * FROM captured_images")
            assert not pending_media.exists(active.runtime)
            assert await active.pause() == []
            active.restart()
            active.start(upload=True, deliver=False, worker=False)
            for value in sources:
                await outbound.put(value)
            await until(lambda: len([item for item in active.events
                                    if item["stage"] == "duplicate_suppressed"]) == 3,
                        tasks=active.tasks)
            assert active.captures() == 3
            assert list(active.db.execute(
                "SELECT event_id,capture_json FROM window_inputs ORDER BY rowid")) == (
                    original_receipts)
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert len(sends) == 3
            assert active.runtime.row()["state"] == "ACTIVE"
            assert all(not task.done() for task in active.tasks)
    run(scenario())


@pytest.mark.parametrize("mutation", ["missing_shared", "source", "index"])
def test_selected_image_reconstruction_rejects_changed_evidence(mutation):
    from companion_onebot_bridge.reference_reader import captured_media
    from tests.support.onebot_events import config

    message = [BAD_IMAGES[0], image_segment()]
    event = normalize(encode(raw_event(message=message)), config(),
        now=raw_event()["time"], rich_input_scope="references_v1",
        image_scope="owner_image_v1", skip_unsupported_images=True)
    if mutation == "missing_shared":
        event["payload"]["shared_content"] = []
    elif mutation == "source":
        event["payload"]["rich"]["segments"][1]["source_sha256"] = "0" * 64
    else:
        event["payload"]["rich"]["segments"][1]["index"] = 63
    with pytest.raises(ValueError, match="CAPTURE_MEDIA_CHANGED"):
        captured_media(event, message)


@pytest.mark.parametrize("failure", ["authority", "identity", "signature", "source"])
def test_unsupported_image_does_not_mask_native_evidence_rejection(core, tmp_path, failure):
    async def scenario():
        async with window(core, tmp_path) as (client, config, plan):
            spool, runtime = native_spool(core, tmp_path, config, plan)
            source = encode(raw_event(time=int(core.clock.now.timestamp()),
                                      message=[BAD_IMAGES[0]]))
            try:
                prepared = await capture.prepare_owner_input(runtime, client, None, source,
                    core.clock.now, ("127.0.0.1", 3001))
                event = prepared["event"]
                if failure == "authority":
                    runtime.stop()
                    with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                        await capture.prepare_owner_input(runtime, client, None, source,
                            core.clock.now, ("127.0.0.1", 3001))
                    return
                if failure == "identity":
                    event["source_event_id"] += "-changed"
                    with pytest.raises(ValueError, match="OWNER_CAPTURE_IDENTITY_CHANGED"):
                        runtime.persist_capture(event, source, received_at=core.clock.now,
                                                peer=("127.0.0.1", 3001))
                    return
                assert runtime.persist_capture(event, source, received_at=core.clock.now,
                                               peer=("127.0.0.1", 3001), media=prepared["media"])
                if failure == "source":
                    changed = json.loads(source)
                    changed["message"][0]["data"]["summary"] = "changed"
                    with pytest.raises(ValueError, match="SOURCE_EVENT_CONFLICT"):
                        runtime.persist_capture(event, encode(changed), received_at=core.clock.now,
                                                peer=("127.0.0.1", 3001))
                else:
                    receipt = json.loads(spool.db.execute(
                        "SELECT capture_json FROM window_inputs").fetchone()[0])
                    receipt["signature"] = "0" * 128
                    response = await client.post("/internal/v1/owner-authority/inputs/approve",
                                                 json=receipt)
                    assert response.status_code == 403, response.text
                assert not core.store.rows("SELECT * FROM events")
                assert not core.store.rows("SELECT * FROM model_usage")
            finally:
                spool.close()
    run(scenario())
