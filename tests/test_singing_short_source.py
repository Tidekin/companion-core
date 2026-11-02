"""A native Reader short-link result remains an exact, source-bound singing input."""

import json

import pytest

from companion import singing
from companion.contracts import CoreError
from companion.store import after, encode
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, call, worker_for
from tests.test_reading_followup import SHORT, URL, admit
from tests.test_singing_integration import queue_song, singing_window


def test_reader_short_link_observation_queues_the_resolved_song(core, tmp_path, monkeypatch):
    async def scenario():
        async with singing_window(core, tmp_path, monkeypatch) as (client, config, plan):
            _, _, job = await queue_song(core, client, config, plan, short_link=True)
            recipe = json.loads(job["singing_context_json"])["recipe"]
            observation = core.store.one("SELECT event_id FROM observations")
            assert recipe["source_event_id"] == observation["event_id"]
            assert recipe["bvid"] == URL.rsplit("/", 1)[1]
            assert len(core.store.rows("SELECT * FROM voice_jobs")) == 1
    run(scenario())


@pytest.mark.parametrize("change", [
    "target", "origin", "video", "coherent_target", "missing_resolution", "missing_video",
    "digest", "future",
    "redacted", "event_redacted", "source",
])
def test_short_link_song_rechecks_original_reader_proof(core, tmp_path, monkeypatch, change):
    async def scenario():
        async with singing_window(core, tmp_path, monkeypatch) as (client, config, plan):
            _, _, job = await queue_song(core, client, config, plan, short_link=True)
            original_usage = core.store.rows("SELECT * FROM model_usage")
            observation = core.store.one("SELECT * FROM observations")
            event_id = observation["event_id"]
            evidence = json.loads(observation["evidence_json"])
            reading = evidence["read_observation"]["evidence"]
            if change == "target":
                reading["resolution"]["target_url"] = "https://www.bilibili.com/video/BV1fYpn6SEk9"
            elif change == "origin":
                reading["resolution"]["source_url"] = "https://b23.tv/Other7"
            elif change == "video":
                reading["video"]["bvid"] = "BV1fYpn6SEk9"
            elif change == "coherent_target":
                reading["video"]["bvid"] = "BV1fYpn6SEk9"
                reading["resolution"]["target_url"] = "https://www.bilibili.com/video/BV1fYpn6SEk9"
            elif change == "missing_resolution":
                reading.pop("resolution")
            elif change == "missing_video":
                reading.pop("video")
            elif change == "future":
                evidence["read_observation"]["fetched_at"] = after(core.clock(), 60)
            elif change in {"redacted", "digest"}:
                column, value = ("redacted", 1) if change == "redacted" else (
                    "source_sha256", "f" * 64)
                core.store.execute(f"UPDATE observations SET {column}=? WHERE event_id=?",
                                   (value, event_id))
            elif change == "event_redacted":
                core.store.execute("UPDATE events SET redacted=1 WHERE event_id=?", (event_id,))
            else:
                core.store.execute("UPDATE events SET source='observation.web' WHERE event_id=?",
                                   (event_id,))
            core.store.execute("UPDATE observations SET evidence_json=? WHERE event_id=?",
                               (encode(evidence), event_id))
            with pytest.raises(CoreError):
                singing.check_source(core.store, core.settings, core.clock, job)
            assert core.store.rows("SELECT * FROM model_usage") == original_usage
            assert len(core.store.rows("SELECT * FROM voice_jobs")) == 1
            assert not core.store.rows("SELECT * FROM outbox")
    run(scenario())


def test_unread_short_link_has_no_singing_attempt_or_source_reservation(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with singing_window(core, tmp_path, monkeypatch) as (client, config, plan):
            event, admitted = await admit(client, core, config, plan, 1, "唱这首", card=SHORT)
            provider = Provider(answer(calls=[call("catalog", "reading_catalog", {})]),
                answer(calls=[call("sing", "sing_song", {"source_event_id": event["event_id"]})]),
                answer("需要先确认视频来源。"))
            async with worker_for(core, provider) as worker:
                assert await worker.run_once("unread-short-song", job_id=admitted["job_id"])
            tool = core.store.one("SELECT result_json FROM agent_tool_calls "
                                  "WHERE tool_name='sing_song'")
            result = json.loads(tool["result_json"])
            assert result["state"] == "UNAVAILABLE"
            assert result["error_code"] == "SINGING_EXACT_VIDEO_REQUIRED"
            assert not core.store.rows("SELECT * FROM voice_jobs")
            assert not core.store.rows("SELECT * FROM exploration_requests")
    run(scenario())
