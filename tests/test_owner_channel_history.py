"""Recent owner turns from QQ and the native client form one ordered history."""

from companion import owner_channel
from companion.dialogue_history import recent_context_sources
from companion.store import uid
from tests.support.dialogue_context import current_job, sent_history


def move_to_native(core, number):
    """Re-home one synthetic turn in a native owner conversation (schema-level fixture)."""
    event = core.store.one("SELECT * FROM events WHERE event_id=%s", (f"evt_test_{number}",))
    qq = core.store.one("SELECT * FROM conversations WHERE conversation_id=%s",
                        (event["conversation_id"],))
    native = core.store.one("SELECT conversation_id FROM conversations WHERE platform="
                            "'native.chat'")
    if native is None:
        values = {key: value for key, value in qq.items() if key != "record_sequence"}
        values.update(conversation_id=uid("conv"), platform="native.chat",
                      platform_session_key="native:owner")
        core.store.insert("conversations", **values)
        native = {"conversation_id": values["conversation_id"]}
    for table in ("events", "outbox"):
        column = "event_id" if table == "events" else "origin_event_id"
        core.store.execute(f"UPDATE {table} SET conversation_id=%s WHERE {column}=%s",
                           (native["conversation_id"], event["event_id"]))
    core.store.execute("UPDATE events SET source='native.chat' WHERE event_id=%s",
                       (event["event_id"],))
    return native["conversation_id"]


def test_turns_from_both_channels_merge_in_order_within_the_same_ceiling(core):
    sent_history(core, 1, "我在QQ上说的第一句")
    core.clock.advance(60)
    sent_history(core, 2, "我在客户端上说的")
    core.clock.advance(60)
    sent_history(core, 3, "又回QQ说了一句")
    native = move_to_native(core, 2)
    core.store.execute("UPDATE outbox SET sent_at=created_at WHERE sent_at IS NULL")
    job = current_job(core)
    only_qq = recent_context_sources(core.store, job["conversation_id"], job["event_id"], 8)
    assert [item["event_id"] for item in only_qq] == ["evt_test_1", "evt_test_3"]
    merged = owner_channel.recent_sources(core.store, job["conversation_id"], job["event_id"], 8)
    assert [item["event_id"] for item in merged] == ["evt_test_1", "evt_test_2", "evt_test_3"]
    assert merged[1]["conversation_id"] == native
    # The signed ceiling still bounds the merged set: the newest turns stay.
    newest = owner_channel.recent_sources(core.store, job["conversation_id"], job["event_id"], 2)
    assert [item["event_id"] for item in newest] == ["evt_test_2", "evt_test_3"]


def test_a_non_owner_conversation_is_never_merged(core):
    assert owner_channel.private_conversations(core.store, "conv_missing") == ["conv_missing"]


def test_another_channel_turn_says_where_it_was_said():
    turn = [{"role": "user", "content": "看这个"}, {"role": "assistant", "content": "好"}]
    tagged = owner_channel.tag_turn(turn, {}, "qq.onebot")
    assert tagged[0]["content"] == "（在QQ上）看这个" and turn[0]["content"] == "看这个"
    assert owner_channel.tag_turn(turn, {}, "native.chat")[0]["content"] == "（在客户端上）看这个"
    assert owner_channel.tag_turn(turn, {}, "system.timer") is turn
