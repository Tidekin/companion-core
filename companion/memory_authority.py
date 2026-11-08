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
    if not owner_authority.required(store, settings):
        return True  # Existing Demo/legacy callers retain their native source checks.
    if not store.in_transaction:
        return store.read_or_write(lambda: source_allowed(
            store, settings, clock, event_id, purpose, allow_commands=allow_commands))
    view = owner_authority.current(store, settings, clock, purpose=purpose)
    return _allowed(store, settings, clock, event_id, purpose, view,
                    allow_commands=allow_commands)


def sources_allowed(store, settings, clock, event_ids, purpose, *, allow_commands=False):
    """Check one finite set with native source bindings from this read-only snapshot.

    No verdict survives this call. Original signatures and recall markers are
    checked for every source, and independent authority custody is checked again
    before returning. Mutable transactions keep the per-source fresh reads.
    """
    from companion.retrieval_work import SOURCE_NODES

    keys = tuple(dict.fromkeys(event_ids))
    if not keys or not owner_authority.required(store, settings):
        return True
    if not store.in_transaction:
        return store.read_or_write(lambda: sources_allowed(
            store, settings, clock, keys, purpose, allow_commands=allow_commands))
    if store.frozen_scope is None or len(keys) > SOURCE_NODES:
        return all(source_allowed(store, settings, clock, key, purpose,
                                  allow_commands=allow_commands) for key in keys)
    view = owner_authority.current(store, settings, clock, purpose=purpose)
    loaded = owner_input._load_sources(store, keys)
    allowed = all(_allowed(store, settings, clock, key, purpose, view,
                          allow_commands=allow_commands, loaded=loaded[key]) for key in keys)
    owner_authority.current(store, settings, clock, purpose=purpose, authority=view.authority)
    return allowed


def _allowed(store, settings, clock, event_id, purpose, view, *, allow_commands, loaded=None):
    receipt = ({"value": loaded["capture_receipt"]} if loaded["capture_receipt"] is not None
               else None) if loaded is not None else store.one(
                   'SELECT value FROM runtime_meta WHERE key = %s',
                   (owner_input.INPUT + event_id,))
    try:
        if receipt is None and purpose == "memory":
            legacy_owner_input.source(store, settings, view, event_id, clock(),
                                      purpose="memory", allow_commands=allow_commands)
        else:
            original = json.loads(receipt["value"]).get("authority") if receipt else None
            owner_input._checked_source(store, settings, view, event_id, clock(), purpose=purpose,
                history=original != view.authority.as_dict(), allow_commands=allow_commands,
                loaded=loaded)
    except (CoreError, ValueError, TypeError, AttributeError):
        return False
    return True
