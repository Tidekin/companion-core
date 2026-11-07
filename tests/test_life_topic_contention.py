"""Idle Life projection must leave chat writes and verified custody views usable."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

from companion import owner_authority, paid_budget
from companion.contracts import CoreError
from companion.store import Store
from tests.conftest import run
from tests.support.life_history_scope import configure, grant, scope
from tests.support.life_topics import bug
from tests.support.owner_memory import environment


def test_idle_projection_does_not_block_an_independent_writer(core, monkeypatch):
    bug(core)
    core.clock.advance(86400)
    core.life_topics.synchronize()
    assert core.store.one("SELECT state FROM life_topics")["state"] == "DUE"
    entered, release = Event(), Event()
    sources = core.life_topics.sources

    def inspect_source(topic_id):
        entered.set()
        assert release.wait(10), "independent writer did not finish"
        return sources(topic_id)

    monkeypatch.setattr(core.life_topics, "sources", inspect_source)
    with closing(Store(core.settings.database)) as writer, ThreadPoolExecutor(1) as executor:
        writer.execute("PRAGMA busy_timeout=50")
        projection = executor.submit(core.life_topics.synchronize)
        try:
            assert entered.wait(10), "Life did not reach the idle source inspection"
            with writer.transaction():
                writer.execute("INSERT INTO runtime_meta VALUES (?,?)",
                               ("independent_chat_write", "committed"))
        finally:
            release.set()
            projection.result(timeout=10)
        assert writer.one("SELECT value FROM runtime_meta WHERE key=?",
                          ("independent_chat_write",))["value"] == "committed"


def test_idle_native_projection_reuses_the_verified_budget_view(core, tmp_path, monkeypatch):
    async def scenario():
        async with environment(core, tmp_path) as (admit, _, reference):
            configure(core)
            capture, _ = await admit(1, "A synthetic unfinished export task for tomorrow.")
            conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                          (capture.event_id,))["conversation_id"]
            grant(core, reference, scope(core, conversation, [capture.event_id], mode="selected"),
                  "idle-life-projection")
            core.life_topics.synchronize()
            core.clock.advance(61)
            core.life_topics.synchronize()
            before = core.store.rows("SELECT * FROM life_topics")
            assert len(before) == 1 and before[0]["state"] == "DUE"
            paid_budget.forget_views(core.store)

            def current():
                return core.store.read_or_write(lambda: owner_authority.current(
                    core.store, core.settings, core.clock))

            current()
            replays, native = [], paid_budget._journal

            def replay(*args, **kwargs):
                replays.append(1)
                return native(*args, **kwargs)

            monkeypatch.setattr(paid_budget, "_journal", replay)
            for _ in range(3):
                core.life_topics.synchronize()
                current()
            assert core.store.rows("SELECT * FROM life_topics") == before
            assert not replays, "idle metadata writes repeatedly invalidate verified custody"

    run(scenario())


def test_unavailable_native_candidates_share_one_verified_budget_view(core, tmp_path, monkeypatch):
    async def scenario():
        async with environment(core, tmp_path) as (admit, _, reference):
            configure(core)
            capture, _ = await admit(1, "A synthetic unfinished export task for tomorrow.")
            conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                          (capture.event_id,))["conversation_id"]
            grant(core, reference, scope(core, conversation, [capture.event_id], mode="selected"),
                  "unavailable-life-scan")
            core.life_topics.synchronize()
            core.clock.advance(61)
            core.life_topics.synchronize()
            topic = core.store.one("SELECT * FROM life_topics")
            for index in range(20):
                core.store.execute(
                    "INSERT INTO life_topics SELECT ?,conversation_id,?,'reading','DUE',?,"
                    "expires_at,reason_code,revision,0,NULL,created_at,updated_at "
                    "FROM life_topics WHERE topic_id=?",
                    (f"blocked_{index:02d}", f"blocked:{index}",
                     f"2000-01-01T00:00:{index:02d}+00:00", topic["topic_id"]))
            monkeypatch.setattr(core.life_topics, "synchronize", lambda: None)
            checked, replays = [], []
            native = paid_budget._journal

            def replay(*args, **kwargs):
                replays.append(1)
                return native(*args, **kwargs)

            def unavailable(topic_id):
                checked.append(topic_id)
                owner_authority.current(core.store, core.settings, core.clock)
                raise CoreError("LIFE_SOURCE_UNAVAILABLE", 409)

            paid_budget.forget_views(core.store)
            monkeypatch.setattr(paid_budget, "_journal", replay)
            monkeypatch.setattr(core.life_topics, "material", unavailable)
            before = core.store.rows("SELECT * FROM model_usage")
            assert core.life_topics.schedule() is None
            assert len(checked) == 20
            assert len(replays) <= 1, "candidate cursor writes invalidate the same verified ledger"
            assert core.store.rows("SELECT * FROM model_usage") == before
            assert not core.store.rows("SELECT * FROM life_activity_results")

    run(scenario())
