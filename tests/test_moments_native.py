"""The native client reads, likes, hides and forgets moments; 那年今日 needs no model."""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import moments
from companion.store import after
from tests.support.life_expression_consumption import (
    close_provider_clients as close_provider_clients,
)
from tests.support.native_chat import enroll
from tests.support.owner_console import client as client
from tests.test_moments import moment_ready

TEXT = "这段关于恒星颜色的介绍让我想再弄懂光谱与温度的关系。"


def published(core, tmp_path):
    moment_ready(core, tmp_path)
    core.life_topics.consume_due(diary=core.diary)
    return core.store.one("SELECT moment_id FROM moments")["moment_id"]


def test_like_is_idempotent_and_changes_the_feed_signature(core, tmp_path):
    moment_id = published(core, tmp_path)
    before = moments.signature(core.store)
    assert moments.set_liked(core.store, moment_id, True, core.clock())["liked"] is True
    core.clock.advance(1)
    assert moments.set_liked(core.store, moment_id, True, core.clock())["liked"] is True
    assert moments.items(core.store)[0]["liked"] is True
    assert moments.signature(core.store) != before
    moments.set_liked(core.store, moment_id, False, core.clock())
    assert moments.items(core.store)[0]["liked"] is False
    assert core.store.one("SELECT COUNT(*) n FROM audit_log WHERE action='moment.liked'")["n"] == 1


def test_memories_show_the_same_day_from_earlier_years_only(core, tmp_path):
    moment_id = published(core, tmp_path)
    assert moments.memories(core.store, core.settings, core.clock()) == []
    year_ago = after(core.clock(), -365 * 86400)
    core.store.execute("UPDATE moments SET created_at=%s WHERE moment_id=%s",
                       (year_ago, moment_id))
    shown = moments.memories(core.store, core.settings, after(year_ago, 365 * 86400))
    assert [(item["id"], item["years_ago"]) for item in shown] == [(moment_id, 1)]
    core.store.execute("UPDATE moments SET state='HIDDEN' WHERE moment_id=%s", (moment_id,))
    assert moments.memories(core.store, core.settings, after(year_ago, 365 * 86400)) == []


def test_native_routes_read_like_hide_and_forget(client, core, tmp_path):
    moment_id = published(core, tmp_path)
    headers, _ = enroll(client, Ed25519PrivateKey.generate())
    feed = client.get("/v1/console/chat/moments", headers=headers)
    assert feed.status_code == 200, feed.text
    assert [(i["id"], i["text"], i["liked"]) for i in feed.json()["items"]] == [
        (moment_id, TEXT, False)]
    path = f"/v1/console/chat/moments/{moment_id}"
    liked = client.post(path + "/like", json={"liked": True}, headers=headers)
    assert liked.status_code == 200 and liked.json() == {"moment_id": moment_id, "liked": True}
    assert client.get("/v1/console/chat/moments/memories", headers=headers).json() == {
        "items": []}
    hidden = client.post(path + "/visibility", json={"hidden": True}, headers=headers)
    assert hidden.status_code == 200
    assert client.get("/v1/console/chat/moments", headers=headers).json()["items"] == []
    forgot = client.post(path + "/forget", json={}, headers=headers)
    assert forgot.status_code == 200 and forgot.json()["state"] == "ERASED"
    assert client.post(path + "/like", json={"liked": True}, headers=headers).status_code == 404
