"""Singing shares native Reader reservations and cannot replay uncertain source work."""

import pytest

from companion.contracts import CoreError
from companion.exploration import METERED_USAGE_SQL
from companion.voice_jobs import VoiceJobs
from companion.voice_protocol import VoiceLeaseRef
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, call, worker_for
from tests.test_reading_followup import admit
from tests.test_singing_integration import queue_song, singing_window
from tests.test_voice_jobs import claim, control


def test_model_bilibili_song_query_uses_original_reader_queue(core, tmp_path, monkeypatch):
    async def scenario():
        async with singing_window(core, tmp_path, monkeypatch) as (client, bridge, plan):
            _, admitted = await admit(client, core, bridge, plan, 1, "找首人是猫唱给我听")
            provider = Provider(answer(calls=[call("find-song", "discover_content", {
                "source": "bilibili", "query": "人是猫"})]))
            async with worker_for(core, provider) as worker:
                assert await worker.run_once("find-native-song", job_id=admitted["job_id"])
            reading = core.store.one("SELECT * FROM exploration_requests")
            assert reading["query"] == "人是猫" and reading["operation"] == "discover"
            assert reading["state"] == "QUEUED" and len(provider.requests) == 1
            assert not core.store.rows("SELECT * FROM voice_jobs")
    run(scenario())


@pytest.mark.parametrize("started", [False, True])
def test_source_reservation_release_and_unknown_liability(core, tmp_path, monkeypatch, started):
    async def scenario():
        async with singing_window(core, tmp_path, monkeypatch) as (client, bridge, plan):
            _, admitted, job = await queue_song(core, client, bridge, plan)
            core.settings.exploration_daily_requests = 4
            assert not core.exploration.budget_available()
            jobs = VoiceJobs(core.store, core.settings, core.clock)
            if started:
                _, leased = claim(jobs)
                ref = VoiceLeaseRef.from_lease(leased.lease)
                jobs.input("local", ref)
                control(jobs, ref, "UNKNOWN", "QUARANTINED")
            else:
                with core.store.transaction():
                    jobs.withdraw(job["job_id"])
            row = core.store.one("SELECT * FROM voice_jobs")
            assert row["reserved_output_bytes"] == 0 and row["singing_context_json"] == "{}"
            used = core.store.one("SELECT COALESCE(SUM(accounted_requests),0) n "
                                 f"FROM ({METERED_USAGE_SQL})")
            assert used["n"] == (4 if started else 0)
            assert row["state"] == ("UNKNOWN" if started else "CANCELLED")
            assert core.exploration.budget_available() is (not started)
            with core.store.transaction(), pytest.raises(CoreError,
                match="SINGING_ORIGINAL_ATTEMPT_EXISTS"):
                original = core.store.one("SELECT * FROM jobs WHERE job_id=?",
                                           (admitted["job_id"],))
                jobs.create_sing(original, job["event_id"], 1, "another-tool-call")
            assert len(core.store.rows("SELECT * FROM voice_jobs")) == 1
    run(scenario())
