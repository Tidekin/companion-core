"""Shared Core reads of persisted delivery state."""


def message_by_id(store, message_id):
    """Read the original delivery projection within the current transaction."""
    return store.one(
        "SELECT outbox.message_id AS message_id, outbox.origin_event_id AS origin_event_id, "
        "outbox.conversation_id AS conversation_id, outbox.action_type AS action_type, "
        "outbox.content_json AS content_json, outbox.content_sha256 AS content_sha256, "
        "outbox.approval_id AS approval_id, outbox.state AS state, outbox.delivery_key AS "
        "delivery_key, outbox.lease_owner AS lease_owner, outbox.lease_until AS lease_until, "
        "outbox.attempt_count AS attempt_count, outbox.provider_message_id AS "
        "provider_message_id, outbox.last_error_code AS last_error_code, outbox.created_at AS "
        "created_at, outbox.updated_at AS updated_at, outbox.sent_at AS sent_at, "
        "outbox.lease_token AS lease_token, outbox.next_attempt_at AS next_attempt_at, "
        "outbox.reply_group_id AS reply_group_id, outbox.segment_index AS segment_index, "
        "outbox.segment_count AS segment_count, outbox.reply_group_sha256 AS "
        "reply_group_sha256, outbox.segment_interval_ms AS segment_interval_ms, "
        "outbox.delivery_lane AS delivery_lane, outbox.native_ack_json AS native_ack_json, "
        "outbox.authority_json AS authority_json FROM outbox AS outbox WHERE outbox.message_id "
        "= %s",
        (message_id,),
    )
