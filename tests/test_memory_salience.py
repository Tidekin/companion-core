"""Owner mentions and mood only reorder already eligible memories, and erase with sources."""

from companion import memory_salience
from tests.conftest import ingest, run
from tests.support.confirmed_memory import remember


def owner_job(core, number, text):
    key = ingest(core, number, text)["event_id"]
    event = core.store.one("SELECT event_id, body_json FROM events WHERE event_id=%s", (key,))
    return {"kind": "chat", "event_id": key, "created_at": core.clock(),
            "body_json": event["body_json"]}


def mentions(core):
    return [(row["memory_id"], row["event_id"]) for row in core.store.rows(
        "SELECT memory_id, event_id FROM memory_mentions ORDER BY memory_id, event_id")]


def test_only_owner_words_touching_a_recalled_memory_are_recorded(core):
    trip = remember(core, 1, "主人下周要去杭州出差", confirmed=True)
    tea = remember(core, 2, "主人喜欢喝龙井茶", confirmed=True)
    job = owner_job(core, 10, "我下周去杭州出差要带什么")
    rows = core.store.rows("SELECT memory_id, content FROM memories ORDER BY memory_id")
    memory_salience.remember(core.store, job, rows)
    memory_salience.remember(core.store, job, rows)  # Idempotent per message.
    assert mentions(core) == [(trip, job["event_id"])]
    assert tea not in {memory for memory, _ in mentions(core)}


def test_mentions_erase_with_their_message_or_their_memory(core):
    trip = remember(core, 1, "主人下周要去杭州出差", confirmed=True)
    row = core.store.one("SELECT memory_id, content FROM memories WHERE memory_id=%s", (trip,))
    first = owner_job(core, 10, "下周杭州出差好累")
    second = owner_job(core, 11, "杭州出差的酒店订好了")
    for job in (first, second):
        memory_salience.remember(core.store, job, [row])
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [first["event_id"]], core.clock())
    assert mentions(core) == [(trip, second["event_id"])]
    core.memory.decide(trip, "forget", core.clock())
    assert mentions(core) == []


def test_erased_message_cannot_leave_a_late_mention(core):
    trip = remember(core, 1, "主人下周要去杭州出差", confirmed=True)
    job = owner_job(core, 10, "下周杭州出差好累")
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [job["event_id"]], core.clock())
    row = {"memory_id": trip, "content": "主人下周要去杭州出差"}
    memory_salience.remember(core.store, job, [row])
    assert mentions(core) == []


def search(core, query, salience):
    trace = {}
    rows = run(core.memory.search_async(query, now=core.clock(), clock=core.clock, trace=trace,
                                        salience=salience))
    return [row["memory_id"] for row in rows], trace


def test_vivid_memory_moves_up_but_never_adds_or_drops_candidates(core):
    first = remember(core, 1, "主人周末打羽毛球", confirmed=True)
    core.clock.advance(1)
    second = remember(core, 2, "主人周末打羽毛球很开心", confirmed=True)
    base, _ = search(core, "周末羽毛球", None)
    assert set(base) == {first, second}
    low = base[-1]
    row = core.store.one("SELECT memory_id, content FROM memories WHERE memory_id=%s", (low,))
    for number in range(10, 14):
        core.clock.advance(86400)
        memory_salience.remember(core.store, owner_job(core, number, "周末羽毛球打得好累"),
                                 [row])
    boosted, trace = search(core, "周末羽毛球", {"mood": None})
    assert set(boosted) == set(base) and boosted[0] == low
    assert trace["selected"][low]["salience"]["vividness"] > 0
    assert search(core, "周末羽毛球", None)[0] == base  # Without salience nothing changes.
    assert search(core, "完全无关的话题", {"mood": None})[0] == []
