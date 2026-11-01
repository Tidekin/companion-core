"""Original image and memory evidence survive every native fragment boundary."""

import json
from datetime import datetime, timedelta

import httpx
import pytest

from companion import image_spool
from companion.reply_segments import SegmentedReplyConfig
from companion.store import encode
from companion_onebot_bridge.network import private_http_client
from tests.conftest import run
from tests.support.native_image_memory import PREFERENCE, native_image_memory
from tests.support.onebot import provider

PARTS = [
    "图1是红色耳机，我会结合你已确认的轻便需求考虑。",
    "图2是蓝色耳机，图片本身无法证明哪款隔音更好。",
    "如果你补充重量和隔音参数，我可以继续帮你比较。",
]
REPLY = "".join(PARTS)


@pytest.mark.parametrize("no_candidates", [False, True])
def test_multi_image_memory_reply_sends_all_parts_from_one_final_generation(
    core, tmp_path, no_candidates,
):
    async def scenario():
        async with native_image_memory(core, tmp_path, no_candidates=no_candidates, reply=REPLY,
            core_options={"segmented_reply": SegmentedReplyConfig(min_chars=12, interval_ms=500)}
        ) as value:
            assert await value.worker.run_once("native-segmented-image-memory")
            rows = core.store.rows("SELECT * FROM outbox ORDER BY segment_index")
            assert [json.loads(row["content_json"])["text"] for row in rows] == PARTS
            assert len(value.downloads) == len(value.calls) == 2
            final = value.calls[-1]["messages"]
            images = [part for message in final if isinstance(message["content"], list)
                      for part in message["content"] if part["type"] == "image_url"]
            assert len(images) == 2
            assert (PREFERENCE in encode(final)) == (not no_candidates)
            # Generation retains originals only for their existing short lifetime.
            assert len(list(image_spool.directory(core.store).glob("*.raw"))) == 2
            usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
            assert len(usage) == 2 and all(row["state"] == "SETTLED" for row in usage)
            usage_ids = [row["request_id"] for row in usage]
            for index, row in enumerate(rows):
                receipt = json.loads(core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                    ("owner_delivery:" + row["message_id"],))["value"])
                assert receipt["request_ids"] == usage_ids
                assert row["segment_index"] == index
                if index:
                    assert not (await value.entry.send(value.entry.client,
                                                       value.entry.protocol))["worked"]
                    assert len(value.sends) == index
                    assert value.entry.db.execute(
                        "SELECT COUNT(*) FROM window_attempts").fetchone()[0] == index
                    core.clock.advance(0.5)
                assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
                event = {"event_id": value.job["event_id"]}
                assert (await value.entry.state(event))["state"] == (
                    "COMPLETE" if index == 2 else "PENDING")
            assert [item["message"][0]["data"]["text"] for item in value.sends] == PARTS
            assert value.entry.db.execute(
                "SELECT COUNT(*) FROM window_attempts WHERE state='ACK'").fetchone()[0] == 3
            assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
            assert len(core.store.rows("SELECT * FROM memories")) == 2
    run(scenario())


@pytest.mark.parametrize("boundary", ["before_lease", "final_request"])
def test_frozen_fragments_survive_original_expiry_and_cleanup(core, tmp_path, boundary):
    async def scenario():
        async with native_image_memory(core, tmp_path, reply=REPLY,
            core_options={"segmented_reply": SegmentedReplyConfig(min_chars=12, interval_ms=500)}
        ) as value:
            assert await value.worker.run_once("native-fragment-image-expiry")
            captures = core.store.rows("SELECT * FROM captured_images ORDER BY item_id")
            expiry = datetime.fromisoformat(max(row["expires_at"] for row in captures))
            core.clock.now = expiry - timedelta(seconds=1)
            assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
            usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")

            def expire():
                core.clock.now = max(core.clock.now, expiry + timedelta(seconds=1))
                image_spool.cleanup(core.store, core.clock(), retain_completed=True)
                assert not list(image_spool.directory(core.store).glob("*.raw"))

            if boundary == "before_lease":
                expire()
            else:
                core.clock.advance(0.5)

                async def final_request(request):
                    if request.url.path == "/send_private_msg":
                        expire()

                value.entry.protocol.event_hooks["request"].append(final_request)
            assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
            core.clock.advance(0.5)
            assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
            assert [item["message"][0]["data"]["text"] for item in value.sends] == PARTS
            assert [row["state"] for row in core.store.rows(
                "SELECT state FROM outbox ORDER BY segment_index")] == ["SENT"] * 3
            assert core.store.rows("SELECT * FROM captured_images ORDER BY item_id") == captures
            assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
            assert len(value.calls) == len(value.downloads) == 2
    run(scenario())


@pytest.mark.parametrize("failure", ["memory_drift", "image_evidence_drift", "unknown_send"])
@pytest.mark.parametrize("expired", [False, True])
def test_joint_evidence_loss_or_unknown_send_stops_remaining_fragments(
    core, tmp_path, failure, expired,
):
    async def scenario():
        async with native_image_memory(core, tmp_path, reply=REPLY,
            core_options={"segmented_reply": SegmentedReplyConfig(min_chars=12, interval_ms=500)}
        ) as value:
            assert await value.worker.run_once("native-fragment-fence")
            if expired:
                expiry = max(row["expires_at"] for row in core.store.rows(
                    "SELECT expires_at FROM captured_images"))
                core.clock.now = datetime.fromisoformat(expiry) - timedelta(seconds=1)
            assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
            usage = core.store.rows("SELECT * FROM model_usage")
            core.clock.advance(2 if expired else 0.5)
            if expired:
                image_spool.cleanup(core.store, core.clock(), retain_completed=True)
                assert not list(image_spool.directory(core.store).glob("*.raw"))
            if failure == "memory_drift":
                core.store.execute("UPDATE memories SET content='已改变' WHERE memory_id=?",
                                   (value.memory_id,))
            elif failure == "image_evidence_drift":
                row = core.store.one("SELECT item_id,proof_json FROM captured_images")
                proof = json.loads(row["proof_json"])
                proof["signature"] = "0" * 128
                core.store.execute("UPDATE captured_images SET proof_json=? WHERE item_id=?",
                                   (encode(proof), row["item_id"]))

            def uncertain(request):
                response = provider(request, value.sends)
                if request.url.path == "/send_private_msg":
                    raise httpx.ReadTimeout("synthetic lost acknowledgement", request=request)
                return response

            if failure == "unknown_send":
                async with private_http_client(value.entry.config.onebot_http_url,
                    value.entry.config.onebot_token, transport=httpx.MockTransport(uncertain)
                ) as protocol:
                    assert (await value.entry.send(value.entry.client, protocol))["worked"]
                assert value.entry.runtime.row()["state"] == "STOPPED"
                assert value.entry.db.execute("SELECT COUNT(*) FROM window_attempts "
                    "WHERE state='UNKNOWN'").fetchone()[0] == 1
                with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                    value.entry.runtime.verify()
                deliveries = core.store.rows("SELECT * FROM outbox")
                with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                    await value.entry.send(value.entry.client, value.entry.protocol)
                assert core.store.rows("SELECT * FROM outbox") == deliveries
            else:
                assert not (await value.entry.send(value.entry.client,
                                                   value.entry.protocol))["worked"]
                assert not (await value.entry.send(value.entry.client,
                                                   value.entry.protocol))["worked"]
            assert len(value.sends) == (2 if failure == "unknown_send" else 1)
            assert len(value.calls) == 2
            assert core.store.rows("SELECT * FROM model_usage") == usage
            states = [row["state"] for row in core.store.rows(
                "SELECT state FROM outbox ORDER BY segment_index")]
            assert states[0] == "SENT" and states[2] == "CANCELLED"
            assert states[1] == ("DELIVERY_UNCERTAIN" if failure == "unknown_send" else "CANCELLED")
    run(scenario())


def test_joint_reply_keeps_full_text_with_native_two_segment_limit(core, tmp_path):
    async def scenario():
        async with native_image_memory(core, tmp_path, reply=REPLY,
            core_options={"segmented_reply": SegmentedReplyConfig(
                min_chars=12, interval_ms=500, max_segments=2)}
        ) as value:
            assert await value.worker.run_once("native-two-fragment-limit")
            rows = core.store.rows("SELECT * FROM outbox ORDER BY segment_index")
            texts = [json.loads(row["content_json"])["text"] for row in rows]
            assert texts == [PARTS[0], PARTS[1] + PARTS[2]]
            assert "".join(texts) == REPLY
            assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
            core.clock.advance(0.5)
            assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
            assert len(value.sends) == len(value.calls) == 2
    run(scenario())
