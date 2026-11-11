"""Publish original native Outbox replies atomically; devices synchronize separately."""

from companion import cumulative, owner_delivery, reply_revision
from companion.contracts import CoreError


def publish(store, settings, clock, message_ids):
    if not store.in_write_transaction:
        raise RuntimeError("NATIVE_PUBLICATION_REQUIRES_ENQUEUE_TRANSACTION")
    for message_id in message_ids:
        row = store.one("SELECT o.*, c.platform FROM outbox o JOIN conversations c "
                        "USING(conversation_id) WHERE message_id=%s", (message_id,))
        if row is None or row["platform"] != "native.chat":
            continue
        if row["state"] == "SENT":
            continue
        if row["state"] != "PENDING" or row["action_type"] not in {
            "native.private.reply", "native.private.proactive",
        }:
            raise CoreError("NATIVE_REPLY_PUBLICATION_REQUIRED", 409)
        owner_delivery.check_delivery(store, settings, clock, message_id)
        reply_revision.require_reply_source(store, row["origin_event_id"])
        now = clock()
        cumulative.admit_operation(store, settings, now, "send", message_id,
                                   row["content_sha256"])
        store.execute("UPDATE outbox SET state='SENT',sent_at=%s,updated_at=%s,"
                      "provider_message_id=%s WHERE message_id=%s AND state='PENDING'",
                      (now, now, message_id, message_id))
        cumulative.send_result(store, message_id, "SENT", now)
        store.audit("native.message.available", message_id, "native-chat", now)
