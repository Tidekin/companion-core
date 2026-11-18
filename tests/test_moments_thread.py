"""Owner posts and comments are signed native inputs that live under their moment."""

from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import moments
from companion.contracts import MomentRef
from tests.support.moments import seed_moment
from tests.support.native_chat import enroll, packet
from tests.support.owner_console import client as client
from tests.support.owner_input import captured


def owner_with_moments(core, tmp_path, *, granted=True):
    life = {"scope": "owner_private_life_shadow_v1", "authorization_sha256": "c" * 64,
            "conversation_id": "conv_native_owner", "source_event_ids": ["evt_seed_source"],
            "purposes": ["PRIVATE_MOMENT"], "max_items": 2,
            "expires_at": (core.clock.now + timedelta(days=3)).isoformat()}
    purposes = ["chat", "life", *(["moments"] if granted else [])]
    captured(core, tmp_path, purposes=purposes, life_shadow=life)


def send(client, core, headers, authority, key, text, moment):
    body = packet(core, key, authority, text, moment=moment)
    response = client.post("/v1/console/chat/messages", json=body, headers=headers)
    return response, body["capture"]["event_id"]


def test_moment_reference_is_well_formed():
    assert MomentRef(kind="post").model_dump() == {"kind": "post"}
    MomentRef(kind="comment", moment_id="moment_" + "a" * 32)
    for value in ({"kind": "post", "moment_id": "moment_" + "a" * 32}, {"kind": "comment"}):
        with pytest.raises(ValueError):
            MomentRef(**value)


def test_owner_post_and_comment_stay_out_of_the_chat_and_under_their_moment(
    client, core, tmp_path,
):
    owner_with_moments(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    hers = seed_moment(core, "今天读到了一点光谱的知识。")
    posted, post_id = send(client, core, headers, authority, key, "今天去爬山了",
                           {"kind": "post"})
    assert posted.status_code == 202, posted.text
    commented, comment_id = send(client, core, headers, authority, key, "我也想看看",
                                 {"kind": "comment", "moment_id": hers})
    assert commented.status_code == 202, commented.text
    history = client.get("/v1/console/chat/messages", headers=headers).json()["items"]
    assert not {item["event_id"] for item in history} & {post_id, comment_id}
    feed = client.get("/v1/console/chat/moments", headers=headers).json()["items"]
    assert {(item["id"], item["author"]) for item in feed} == {
        (post_id, "owner"), (hers, "companion")}
    by_id = {item["id"]: item for item in feed}
    assert by_id[hers]["comment_count"] == 1 and by_id[post_id]["text"] == "今天去爬山了"
    thread = client.get(f"/v1/console/chat/moments/{hers}/comments", headers=headers).json()
    assert [(item["author"], item["text"]) for item in thread["items"]] == [
        ("owner", "我也想看看")]
    # The comment is an ordinary owner input for memory extraction.
    assert core.store.one("SELECT memory_processed FROM events WHERE event_id=%s",
                          (comment_id,)) is not None


def test_mixed_feed_pages_across_a_shared_timestamp(client, core, tmp_path):
    from tests.support.owner_console import login

    owner_with_moments(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    old = seed_moment(core, "更早")
    core.clock.advance(1)
    with core.store.transaction():
        hers = [seed_moment(core, str(index)) for index in range(199)]
    posted, owner_id = send(client, core, headers, authority, key, "同一时刻", {"kind": "post"})
    assert posted.status_code == 202
    timestamp = core.clock()
    login(client)
    for route, expected, auth in [
        ("/v1/console/chat/moments", [*hers, owner_id], headers),
        ("/v1/console/moments", hers, {}),
    ]:
        cursor, seen = None, []
        while True:
            params = {"limit": 30, **({"before": cursor} if cursor else {})}
            response = client.get(route, params=params, headers=auth)
            assert response.status_code == 200, response.text
            page = response.json()
            seen.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
            assert len(seen) <= len(expected)
        assert seen == [*sorted(expected, reverse=True), old]
        legacy = client.get(route, params={"before": timestamp}, headers=auth).json()
        assert [item["id"] for item in legacy["items"]] == [old]
        assert client.get(route, params={"before": "invalid"}, headers=auth).status_code == 400


def test_a_re_targeted_or_unknown_comment_is_refused(client, core, tmp_path):
    owner_with_moments(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    hers = seed_moment(core, "今天读到了一点光谱的知识。")
    body = packet(core, key, authority, "我也想看看",
                  moment={"kind": "comment", "moment_id": hers})
    body["moment"] = {"kind": "comment", "moment_id": "moment_" + "b" * 32}
    response = client.post("/v1/console/chat/messages", json=body, headers=headers)
    assert response.status_code == 403
    missing, _ = send(client, core, headers, authority, key, "在吗",
                      {"kind": "comment", "moment_id": "moment_" + "c" * 32})
    assert missing.status_code == 404


def test_moment_inputs_need_the_moments_permission(client, core, tmp_path):
    owner_with_moments(core, tmp_path, granted=False)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    refused, _ = send(client, core, headers, authority, key, "今天去爬山了", {"kind": "post"})
    assert refused.status_code == 403


def test_forgetting_the_owner_post_erases_it(client, core, tmp_path):
    owner_with_moments(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    _, post_id = send(client, core, headers, authority, key, "今天去爬山了", {"kind": "post"})
    forgot = client.post(f"/v1/console/chat/moments/{post_id}/forget", json={}, headers=headers)
    assert forgot.status_code == 200, forgot.text
    assert moments.target(core.store, post_id) is None
    assert core.store.one("SELECT redacted FROM events WHERE event_id=%s",
                          (post_id,))["redacted"] == 1
