"""An ordinary chat may start one slow discovery while the usable library is short."""

from companion import job_queue, meme_retrieval
from companion.meme_core import MemeCaptionService
from companion.meme_retrieval import MemeRetrieval
from tests.conftest import ingest


def retrieval(core):
    return MemeRetrieval(MemeCaptionService(core.store, core.settings, None, core.clock))


def test_a_short_library_replenishes_at_most_once_per_window(core):
    r = retrieval(core)
    assert r._library_short()
    source = ingest(core, 1, "早")
    job_queue.enqueue(core.store, source["event_id"], "meme_discovery", "meme-test-1",
                      core.clock(), max_attempts=1)
    assert not r._library_short()
    core.clock.advance(meme_retrieval.AMBIENT_REPLENISH_SECONDS + 1)
    assert r._library_short()


def test_replenishment_intents_rotate_through_valid_intents(core):
    from typing import get_args

    from companion.expression import MemeIntent

    assert set(meme_retrieval.REPLENISH_INTENTS) == set(get_args(MemeIntent))
    r = retrieval(core)
    first = r._replenish_intent()
    source = ingest(core, 1, "早")
    job_queue.enqueue(core.store, source["event_id"], "meme_discovery", "meme-test-2",
                      core.clock(), max_attempts=1)
    assert r._replenish_intent() != first
