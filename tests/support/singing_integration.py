import json
from contextlib import asynccontextmanager

from companion.core import Core
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.voice_config import VoiceNode
from tests.sdk_reader_fixture import Provider, answer, call, worker_for
from tests.support.exploration import URL
from tests.support.owner_authority import activate
from tests.support.reading_followup import admit, window
from tests.support.voice_jobs import TOKEN, voice_config


@asynccontextmanager
async def singing_window(core, tmp_path, monkeypatch, *, manifest_sha256="b" * 64):
    def unexpected_failure(self, *args):
        raise

    monkeypatch.setattr(Core, "fail_job", unexpected_failure)
    core.settings.voice = voice_config(nodes={"local": VoiceNode(
        token=TOKEN, manifest_sha256=manifest_sha256, capabilities=("tts", "asr", "sing"))},
        singing={"source_adapter_id": "bili"}, job_ttl_seconds=600,
        result_ttl_seconds=300, max_private_blob_bytes=80 * 1024 * 1024)
    async with window(core, tmp_path, monkeypatch) as (client, config, plan):
        core.settings.exploration_daily_bytes = 80 * 1024 * 1024
        plan.policy = OwnerGrantPolicy.model_validate({**plan.policy.model_dump(),
            "purposes": [*plan.policy.purposes, "voice"],
            "capability_policies": {"voice": core.settings.voice.authority_sha256()}})
        active, _ = activate(core, plan.policy, revision=1, operation="enable-native-test-singing")
        plan.authority = active["authority"]
        yield client, config, plan


async def queue_song(core, client, config, plan, *, short_link=False):
    from tests.support.reading_followup import READER_AUTH, SHORT

    event, admitted = await admit(client, core, config, plan, 1, "想听你唱首歌",
                                  card=SHORT if short_link else URL)
    source_event_id = event["event_id"]
    if short_link:
        from source_reader.bilibili import BilibiliReader
        from source_reader.network import PublicHTTP
        from source_reader.worker_v1 import tick
        from tests.support.heybox_reader import mock_pool
        from tests.support.source_reader import META, PLAYER, SUBTITLE, response

        reader = Provider(answer(calls=[call("read-song", "read_content", {"url": SHORT})]))
        async with worker_for(core, reader) as worker:
            assert await worker.run_once("read-short-song", job_id=admitted["job_id"])
        metadata = {**META, "data": {**META["data"], "bvid": URL.rsplit("/", 1)[1],
            "pages": [{"page": 1, "cid": META["data"]["cid"], "duration": 65}]}}
        pool, _ = mock_pool(response(status=302, headers={"Location": URL}),
                           response(metadata), response(PLAYER), response(SUBTITLE))
        authorization = client.headers["Authorization"]
        client.headers["Authorization"] = READER_AUTH["Authorization"]
        try:
            async with PublicHTTP(pool) as public:
                assert await tick(client, BilibiliReader(public, clock=core.clock), "bili",
                                  source="bilibili", clock=lambda: core.clock.now)
        finally:
            client.headers["Authorization"] = authorization
        observation = core.store.one("SELECT * FROM observations")
        assert observation["canonical_url"] == SHORT
        evidence = json.loads(observation["evidence_json"])["read_observation"]["evidence"]
        assert evidence["video"]["bvid"] == URL.rsplit("/", 1)[1]
        assert evidence["resolution"]["target_url"] == URL
        source_event_id = observation["event_id"]
        core.clock.advance(2)
        provider = Provider(answer(calls=[call("sing", "sing_song", {
            "source_event_id": source_event_id})]), answer("该歌曲暂时无法处理。"))
    else:
        provider = Provider(answer(calls=[call("catalog", "reading_catalog", {})]),
            answer(calls=[call("sing", "sing_song", {"source_event_id": source_event_id})]))
    async with worker_for(core, provider) as worker:
        assert await worker.run_once("choose-singing", job_id=admitted["job_id"])
    job = core.store.one("SELECT * FROM voice_jobs")
    assert job is not None, core.store.rows("SELECT tool_name,result_json FROM agent_tool_calls "
                                           "WHERE tool_name='sing_song'")
    assert job["operation"] == "sing" and job["state"] == "QUEUED"
    assert job["reserved_output_bytes"] == 56 * 1024 * 1024
    assert core.store.one("SELECT state FROM jobs")["state"] == "RETRY_WAIT"
    assert len(provider.requests) == (1 if short_link else 2)
    assert not core.store.rows("SELECT * FROM outbox")
    return event, admitted, job


async def select_song(core, admitted, song_id, *, surrounding_text=False):
    core.clock.advance(2)
    parts = [{"kind": "singing", "text": "给你唱一首。", "request_id": song_id}]
    if surrounding_text:
        parts = [{"kind": "text", "text": "这首歌送给你。"}, *parts,
                 {"kind": "text", "text": "唱完啦，我们继续聊。"}]
    provider = Provider(answer(calls=[call("final-song", "reply_with_parts",
        {"parts": parts})]))
    async with worker_for(core, provider) as worker:
        assert await worker.run_once("send-singing", job_id=admitted["job_id"])
    assert core.store.one("SELECT state,last_error_code FROM jobs")["state"] == "SUCCEEDED"
    assert len(provider.requests) == 1
    return core.store.rows("SELECT * FROM outbox ORDER BY segment_index")
