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
