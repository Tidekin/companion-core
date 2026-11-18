"""Voice reuses the original owner grant, input receipt and Outbox custody."""

import json
from datetime import UTC, datetime

from companion import owner_authority, owner_delivery, owner_input
from companion.contracts import CoreError
from companion.outbox import message_by_id


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
    row = message_by_id(store, message_id)
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
