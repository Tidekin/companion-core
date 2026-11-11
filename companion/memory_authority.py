"""Read-only domain routing to the original owner input evidence validators."""

import json

from companion import legacy_owner_input, owner_authority, owner_input
from companion.contracts import CoreError


def source_allowed(store, settings, clock, event_id, purpose, *, allow_commands=False):
    """A current domain grant never upgrades an old source's execution credentials.

    Same-revision raw input uses its domain purpose. Reading a previous v2
    revision additionally needs the explicit history scope. The legacy memory
    codec already defines its separate owner-private memory read scope.
    Retained Memory facts use their own evidence contract, not this raw reader.
    """
    event = store.one("SELECT source,event_type,ingested_at FROM events WHERE event_id=%s",
                      (event_id,))
    if event and event["source"] in {"owner.history", "owner.character"}:
        from companion.history_import import source_allowed as imported_source_allowed

        return imported_source_allowed(store, settings, clock, event, purpose)
    if not owner_authority.required(store, settings):
        return True  # Existing Demo/legacy callers retain their native source checks.
    view = owner_authority.current(store, settings, clock, purpose=purpose)
    receipt = store.one('SELECT value FROM runtime_meta WHERE key = %s',
                        (owner_input.INPUT + event_id,))
    try:
        if receipt is None and purpose == "memory":
            legacy_owner_input.source(store, settings, view, event_id, clock(),
                                      purpose="memory", allow_commands=allow_commands)
        else:
            original = json.loads(receipt["value"]).get("authority") if receipt else None
            owner_input.source(store, settings, view, event_id, clock(), purpose=purpose,
                history=original != view.authority.as_dict(), allow_commands=allow_commands)
    except (CoreError, ValueError, TypeError, AttributeError):
        return False
    return True
