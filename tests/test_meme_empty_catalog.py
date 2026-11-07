"""Empty-catalog discovery keeps the original signed source and HTTP admission."""

import json

import httpx
import pytest

from companion_onebot_bridge.meme_runtime import MemeRuntime
from companion_onebot_bridge.owner_memes import MemePump
from tests.conftest import run
from tests.support.meme_runtime import RULE
from tests.support.native_meme import NativeCase, capture
from tests.support.next_memes_catalog import metadata, png


@pytest.mark.parametrize("text,expected,query", [
    ("太棒了，成功了", "QUEUED", None),
    ("今天喝茶", "NOT_NEEDED", None),
    ("别发表情包，太棒了", "NOT_NEEDED", None),
    ("手术成功了", "NOT_NEEDED", None),
    ("/表情 太棒了", "NOT_NEEDED", None),
    ("我很难过，想抱抱", "NOT_NEEDED", "accepted-null"),
    ("我很难过，想抱抱", "NOT_NEEDED", "deferred-null"),
])
def test_bootstrap_without_offer_uses_only_admitted_meme_intent(core, tmp_path, text,
                                                              expected, query):
    fixture = NativeCase(core, tmp_path, rules=(RULE,), semantic=True)
    try:
        runtime = MemeRuntime(fixture.owner)
        event = capture(runtime, text=text)

        async def scenario():
            async with fixture.client(runtime) as client:
                admitted = await runtime.window.flush_one(client)
                assert admitted
                assert runtime.db.execute("SELECT body FROM inbox").fetchone()[0] is None
                pump = MemePump(runtime.window, client)
                try:
                    async def lose_offer(request):
                        if request.url.path == "/internal/v1/memes/offers":
                            raise httpx.ReadTimeout("synthetic lost null offer", request=request)

                    if query == "deferred-null":
                        client.event_hooks["request"].append(lose_offer)
                    pump.admitted(event["event_id"], payload=admitted)
                    if query:
                        await pump.query_task
                    else:
                        pump.interrupt()
                    offer = pump.discovery_offer(event["event_id"])
                    value = await runtime.submit_missing(client, event["event_id"], offer,
                                                         reply_revision=1)
                    assert value.get("state", value.get("status")) == expected
                    if expected == "QUEUED":
                        again = await runtime.submit_missing(client, event["event_id"], offer,
                                                             reply_revision=1)
                        assert again["duplicate"] is True and again["job_id"] == value["job_id"]
                finally:
                    await pump.close()

        run(scenario())
        assert len(core.store.rows("SELECT * FROM jobs WHERE kind='meme_discovery'")) == (
            1 if expected == "QUEUED" else 0
        )
        assert not core.store.rows("SELECT * FROM model_usage")
        assert not core.store.rows("SELECT * FROM outbox")
        saved = core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                               ("meme_offer:" + event["event_id"],))
        if query == "accepted-null":
            assert json.loads(saved["value"])["offer"] is None
        else:
            assert saved is None
    finally:
        fixture.spool.close()


@pytest.mark.parametrize("source", ["pending", "changed", "superseded", "populated"])
def test_bootstrap_does_not_replace_source_or_existing_catalog(core, tmp_path, source):
    fixture = NativeCase(core, tmp_path, rules=(RULE,), semantic=True)
    try:
        runtime = MemeRuntime(fixture.owner)
        event = capture(runtime)

        async def scenario():
            async with fixture.client(runtime) as client:
                pump = MemePump(runtime.window, client)
                admitted = None
                if source != "pending":
                    admitted = await runtime.window.flush_one(client)
                    assert admitted
                if source == "changed":
                    admitted["payload"]["text"] = "太棒了，篡改的输入"
                elif source == "superseded":
                    capture(runtime, 43)
                elif source == "populated":
                    runtime.catalog.add(png(), metadata(), check=runtime.window.verify)
                try:
                    if source == "changed":
                        with pytest.raises(ValueError, match="MEME_INPUT_INVALID"):
                            pump.admitted(event["event_id"], payload=admitted)
                    else:
                        pump.admitted(event["event_id"], payload=admitted)
                        pump.interrupt()
                        assert pump.discovery_offer(event["event_id"]) is None
                finally:
                    await pump.close()

        run(scenario())
        assert not core.store.rows("SELECT * FROM jobs WHERE kind='meme_discovery'")
        assert not core.store.rows("SELECT * FROM model_usage")
    finally:
        fixture.spool.close()
