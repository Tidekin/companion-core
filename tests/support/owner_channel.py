"""Schema-level private channel setup shared by history and care scenarios."""

from companion.store import uid


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
