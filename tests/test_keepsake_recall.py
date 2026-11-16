"""Fresh keepsakes recall as usual; dormant ones wait for the owner's words or a lookup."""

from companion import keepsakes, memory_salience
from companion.store import after
from tests.conftest import ingest, run
from tests.support.confirmed_memory import remember
from tests.support.owner_console import client as client
from tests.support.owner_console import login


def keep(core, memory_id, *, days_ago=0, until=None):
    event_id = core.store.one("SELECT event_id FROM memory_evidence WHERE memory_id=%s",
                              (memory_id,))["event_id"]
    core.store.execute(
        "INSERT INTO keepsakes(event_id, memory_id, excerpt_sha256, reason, kept_at, until) "
        "VALUES (%s, %s, %s, 'SALIENCE', %s, %s)",
        (event_id, memory_id, "a" * 64, after(core.clock(), -days_ago * 86400), until))
    return event_id


def test_states_follow_keep_age_and_recent_owner_mentions(core):
    fresh = remember(core, 1, "主人的生日是三月五号", confirmed=True)
    old = remember(core, 2, "主人小时候住在海边", confirmed=True)
    mentioned = remember(core, 3, "主人养过一只橘猫", confirmed=True)
    keep(core, fresh, days_ago=10)
    keep(core, old, days_ago=400)
    keep(core, mentioned, days_ago=400)
    event = ingest(core, 10, "那只橘猫后来怎么样了")
    job = {"kind": "chat", "event_id": event["event_id"], "created_at": core.clock(),
           "body_json": core.store.one("SELECT body_json FROM events WHERE event_id=%s",
                                       (event["event_id"],))["body_json"]}
    memory_salience.remember(core.store, job, [{"memory_id": mentioned,
                                                "content": "主人养过一只橘猫"}])
    states = keepsakes.states(core.store, [fresh, old, mentioned], core.clock())
    assert {key: value["state"] for key, value in states.items()} == {
        fresh: "FRESH", old: "DORMANT", mentioned: "FRESH"}
    assert keepsakes.states(core.store, [], core.clock()) == {}


def test_expired_keepsake_has_no_state(core):
    memory_id = remember(core, 1, "主人的生日是三月五号", confirmed=True)
    keep(core, memory_id, until=after(core.clock(), -1))
    assert keepsakes.states(core.store, [memory_id], core.clock()) == {}


def test_dormant_keepsake_returns_when_the_owner_words_touch_it(core):
    memory_id = remember(core, 1, "主人小时候住在海边", confirmed=True)
    keep(core, memory_id, days_ago=400)
    rows = run(core.memory.search_async("小时候住在海边", now=core.clock(), clock=core.clock,
                                        salience={"mood": None}))
    assert [row["memory_id"] for row in rows] == [memory_id]
    looked_up = run(core.memory.search_async("小时候住在海边", now=core.clock(),
                                             clock=core.clock))
    assert [row["memory_id"] for row in looked_up] == [memory_id]


def test_console_shows_keepsake_state_and_releases_it_through_retention(client, core):
    memory_id = remember(core, 1, "主人的生日是三月五号", confirmed=True)
    core.settings.raw_event_retention_days = 3
    core.clock.advance(2 * 86400)
    event_id = keep(core, memory_id)
    headers = login(client)
    items = client.get("/v1/console/memories").json()["items"]
    assert items[0]["detail"]["keepsake"] == {"state": "FRESH", "until": None}
    core.clock.advance(2 * 86400)
    path = f"/v1/console/memories/{memory_id}/keepsake/release"
    response = client.post(path, json={}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"memory_id": memory_id, "state": "RELEASED"}
    assert core.store.rows("SELECT * FROM keepsakes") == []
    assert core.store.one("SELECT redacted FROM events WHERE event_id=%s",
                          (event_id,))["redacted"] == 1
    again = client.post(path, json={}, headers={**headers, "Idempotency-Key": "release-again"})
    assert again.status_code == 404
