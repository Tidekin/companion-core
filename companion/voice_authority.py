"""Voice reuses the original owner grant, input receipt and Outbox custody."""

import json
from datetime import UTC, datetime

from companion import owner_authority, owner_delivery, owner_input
from companion.contracts import CoreError


def current(store, settings, clock, *, authority=None):
    if not settings.voice.enabled:
        raise CoreError("VOICE_DISABLED", 403)
    view = owner_authority.current(store, settings, clock, purpose="voice", authority=authority)
    if view.policy.capability_policies.get("voice") != settings.voice.authority_sha256():
        raise CoreError("VOICE_SCOPE_REQUIRED", 403)
    return view


def permitted(store, settings, clock):
    try:
        current(store, settings, clock)
    except CoreError as error:
        if error.code in {"VOICE_DISABLED", "OWNER_AUTHORITY_PURPOSE_REQUIRED"}:
            return False
        raise
    return True


def source(store, settings, clock, event_id, *, allow_commands=False):
    view = current(store, settings, clock)
    event, body, job = owner_input.source(store, settings, view, event_id, clock(),
                                         purpose="voice", allow_commands=allow_commands)
    return view, event, body, job


def delivery(store, settings, clock, message_id):
    owner_delivery.check_delivery(store, settings, clock, message_id)
    row = store.one('SELECT outbox.message_id AS message_id, outbox.origin_event_id AS '
        'origin_event_id, outbox.conversation_id AS conversation_id, outbox.action_type AS '
        'action_type, outbox.content_json AS content_json, outbox.content_sha256 AS '
        'content_sha256, outbox.approval_id AS approval_id, outbox.state AS state, '
        'outbox.delivery_key AS delivery_key, outbox.lease_owner AS lease_owner, '
        'outbox.lease_until AS lease_until, outbox.attempt_count AS attempt_count, '
        'outbox.provider_message_id AS provider_message_id, outbox.last_error_code AS '
        'last_error_code, outbox.created_at AS created_at, outbox.updated_at AS updated_at, '
        'outbox.sent_at AS sent_at, outbox.lease_token AS lease_token, outbox.next_attempt_at AS '
        'next_attempt_at, outbox.reply_group_id AS reply_group_id, outbox.segment_index AS '
        'segment_index, outbox.segment_count AS segment_count, outbox.reply_group_sha256 AS '
        'reply_group_sha256, outbox.segment_interval_ms AS segment_interval_ms, '
        'outbox.delivery_lane AS delivery_lane, outbox.native_ack_json AS native_ack_json, '
        'outbox.authority_json AS authority_json FROM outbox AS outbox WHERE outbox.message_id = '
        '%s', (message_id,))
    current(store, settings, clock, authority=json.loads(row["authority_json"]))
    return row


def deadline(view, *bounds):
    times = [datetime.fromisoformat(value) if isinstance(value, str) else value for value in bounds]
    if view.policy.expires_at is not None:
        times.append(view.policy.expires_at)
    return min(times).astimezone(UTC).isoformat(timespec="microseconds")


def saved(row):
    try:
        value = json.loads(row.get("authority_json") or "{}")
        from companion_delivery_protocol.authority import validate_authority_ref

        return validate_authority_ref(value)
    except (TypeError, ValueError):
        # Historical window jobs retain stop/reconciliation custody, but cannot
        # acquire an execution grant from whichever policy happens to be current.
        raise CoreError("VOICE_OWNER_AUTHORITY_REQUIRED", 403) from None
