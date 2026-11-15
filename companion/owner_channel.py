"""The owner's private channels: QQ and the first-party native client.

Both carry the same owner, authority and limits. A proactive message goes where the
owner last wrote; replies and their delivered text are read from either channel.
"""

SOURCES = ("qq.onebot", "native.chat")
REPLY_ACTIONS = ("qq.private.reply", "native.private.reply")
PROACTIVE_ACTIONS = ("qq.private.proactive", "native.private.proactive")

SQL_SOURCES = "('qq.onebot','native.chat')"
SQL_REPLY_ACTIONS = "('qq.private.reply','native.private.reply')"
SQL_PROACTIVE_ACTIONS = "('qq.private.proactive','native.private.proactive')"
SQL_PRIVATE_ACTIONS = ("('qq.private.reply','qq.private.proactive',"
                       "'native.private.reply','native.private.proactive')")


def latest_conversation(store):
    """The private owner conversation that received the newest owner message, or None."""
    row = store.one(
        "SELECT e.conversation_id FROM events e JOIN conversations c USING(conversation_id) "
        "WHERE e.actor_id='owner' AND e.privacy_scope='private' AND e.source IN "
        + SQL_SOURCES + " AND e.event_type IN ('message.received','message.media_received') "
        "AND c.platform=e.source AND c.scope='private' AND c.owner_actor_id='owner' "
        "ORDER BY e.record_sequence DESC LIMIT 1"
    )
    return row["conversation_id"] if row else None


def action(platform, kind):
    """The stored Outbox action for a logical 'reply' or 'proactive' on one platform."""
    prefix = "native" if platform == "native.chat" else "qq"
    return f"{prefix}.private.{kind}"


def private_conversations(store, conversation_id):
    """Every private owner conversation when `conversation_id` is one of them, else itself.

    One read: history collection runs inside a snapshot guard, so it stays minimal.
    """
    found = [row["conversation_id"] for row in store.rows(
        "SELECT conversation_id FROM conversations WHERE platform IN " + SQL_SOURCES
        + " AND scope = 'private' AND owner_actor_id = 'owner' ORDER BY conversation_id")]
    return found if conversation_id in found else [conversation_id]


def recent_sources(store, conversation_id, current_event_id, max_turns, *, cutoff_rowid=None):
    """Recent owner turns across QQ and the native client, in their real order.

    The same per-conversation selection runs for each channel; the newest
    `max_turns` of the merged set stay, so the signed history ceiling is unchanged.
    """
    from companion.dialogue_history import recent_context_sources

    conversations = private_conversations(store, conversation_id)
    if len(conversations) == 1:
        return recent_context_sources(store, conversation_id, current_event_id, max_turns,
                                      cutoff_rowid=cutoff_rowid)
    merged = []
    for conversation in conversations:
        merged += recent_context_sources(store, conversation, current_event_id, max_turns,
                                         cutoff_rowid=cutoff_rowid)
    # Each source row carries its global event sequence; no further read is needed.
    return sorted(merged, key=lambda item: item["record_sequence"])[-max_turns:]


LABELS = {"qq.onebot": "在QQ上", "native.chat": "在客户端上"}


def tag_turn(turn, source, platform):
    """Mark the owner message of a turn from another channel with where it was said."""
    label = LABELS.get(platform)
    if not label or not turn:
        return turn
    index = len(turn) - 2 if turn[-1]["role"] == "assistant" else len(turn) - 1
    if index < 0 or turn[index]["role"] != "user" or not isinstance(turn[index]["content"], str):
        return turn
    tagged = list(turn)
    tagged[index] = {**turn[index], "content": "（" + label + "）" + turn[index]["content"]}
    return tagged
