"""Native lease yields to pending replies without changing the original Bridge lease."""

import pytest

from companion.transport import enqueue
from tests.conftest import run
from tests.support.meme_native_discovery import native_case, release_foreground
from tests.support.meme_service import claim, discovery_environment, submit


@pytest.mark.parametrize("state", ["PENDING", "LEASED", "SENDING"])
def test_native_outbox_wait_preserves_the_original_unclaimed_attempt(
    core, tmp_path, monkeypatch, state
):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch) as c:
            await c.retrieval.prepare(c.job)
            release_foreground(c)  # Unit precondition; process test executes the real chat.
            event = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                   (c.event["event_id"],))
            message = enqueue(core.store, c.event["event_id"], event["conversation_id"],
                              "Synthetic pending reply", core.clock())
            # Only the projection state varies here; real send/ACK transitions have process proof.
            core.store.execute("UPDATE outbox SET state=? WHERE message_id=?", (state, message))
            original = core.store.one("SELECT * FROM jobs WHERE kind='meme_discovery'")
            assert original["state"] == "QUEUED" and original["attempts"] == 0
            meta = core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                                  ("meme_discovery:" + original["job_id"],))
            reply = core.store.rows("SELECT * FROM outbox")
            for _ in range(2):
                assert not await c.service.run_once("native-waits-for-" + state)
                assert core.store.one("SELECT * FROM jobs WHERE job_id=?",
                                      (original["job_id"],)) == original
                assert core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                                      ("meme_discovery:" + original["job_id"],)) == meta
                assert core.store.rows("SELECT * FROM outbox") == reply
                assert not c.upstream.calls
                for table in ("meme_discovery_reservations", "meme_captions", "meme_assets",
                              "model_usage", "model_admissions"):
                    assert not core.store.rows("SELECT * FROM " + table)

    run(scenario())


def test_original_bridge_discovery_lease_keeps_its_pending_reply_behavior(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with discovery_environment(core, tmp_path, monkeypatch) as (event, _, _):
            original = submit(core, event)
            source = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                    (event["event_id"],))
            enqueue(core.store, event["event_id"], source["conversation_id"],
                    "Synthetic original Bridge reply", core.clock())
            assert core.store.one("SELECT state FROM outbox")["state"] == "PENDING"
            assert claim(core)["job_id"] == original["job_id"]
            assert not core.store.rows("SELECT * FROM meme_discovery_reservations")
            assert not core.store.rows("SELECT * FROM model_usage")

    run(scenario())
