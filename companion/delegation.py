"""Owner-delegated runtime adjustments that the companion may propose or apply.

The owner's configuration stays the default and the bound: an adjustment can only make a
capability run less (fewer wake runs, longer intervals, a temporary pause). It never enables
a capability the owner turned off and never raises spending. Adjustments are runtime state,
take effect at once, are audited, can be reverted, and are discarded when a backup is
restored because the backup cannot prove what happened afterwards.

Delegation modes per capability:
- owner: the companion cannot change anything.
- propose: the companion's adjustment waits for the owner's approval.
- self: within the owner's range the adjustment applies at once and can be reverted.
"""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from companion.contracts import CoreError
from companion.store import encode, uid

# Capability -> field -> (kind, config attribute). "floor" fields may only be lowered from the
# configured value, "ceiling" fields may only be raised (a longer interval means fewer runs).
PAUSE = ("pause", None)


def _floor(attribute):
    return ("floor", attribute)


def _ceiling(attribute):
    return ("ceiling", attribute)


DELEGABLE = {
    "memory.recall": {"pause": PAUSE,
                      "extraction_min_events": _ceiling("extraction_min_events"),
                      "extraction_idle_seconds": _ceiling("extraction_idle_seconds")},
    "diary": {"pause": PAUSE},
    "consolidation": {"pause": PAUSE},
    "reflection": {"pause": PAUSE,
                   "reflection_daily_max_suggestions": _floor("reflection_daily_max_suggestions"),
                   "reflection_max_daily_calls": _floor("reflection_max_daily_calls")},
    "goals": {"pause": PAUSE, "goals_max_active": _floor("goals_max_active")},
    "affect": {"pause": PAUSE},
    "proactive": {"pause": PAUSE, "proactive_daily_limit": _floor("proactive_daily_limit"),
                  "proactive_interval_seconds": _ceiling("proactive_interval_seconds"),
                  "proactive_idle_seconds": _ceiling("proactive_idle_seconds")},
    "wake": {"pause": PAUSE, "wake_max_daily_runs": _floor("wake_max_daily_runs"),
             "wake_min_interval_minutes": _ceiling("wake_min_interval_minutes")},
    "exploration": {"pause": PAUSE,
                    "exploration_daily_candidate_limit":
                        _floor("exploration_daily_candidate_limit"),
                    "exploration_daily_deep_reads": _floor("exploration_daily_deep_reads"),
                    "exploration_daily_requests": _floor("exploration_daily_requests")},
    "expression": {"pause": PAUSE,
                   "expression_cooldown_minutes": _ceiling("expression_cooldown_minutes"),
                   "expression_selection_probability": _floor("expression_selection_probability")},
    "expression_learning": {"pause": PAUSE},
    "memes.semantic": {"pause": PAUSE},
    "reading.web": {"pause": PAUSE}, "reading.bilibili": {"pause": PAUSE},
    "bilibili.video": {"pause": PAUSE}, "qq.images": {"pause": PAUSE},
    "qq.poke": {"pause": PAUSE}, "qq.voice.tts": {"pause": PAUSE},
}
# Capabilities whose runtime already reads the adjustments. The others accept delegation and
# adjustments through the same interface; their consumers are integrated separately.
WIRED = {"diary": {"pause"}, "wake": {"pause", "wake_max_daily_runs", "wake_min_interval_minutes"}}
ENABLED = {"diary": "diary_enabled", "wake": "wake_enabled", "proactive": "proactive_enabled",
           "consolidation": "consolidation_enabled", "reflection": "reflection_enabled",
           "goals": "goals_enabled", "affect": "affect_enabled",
           "exploration": "exploration_enabled",
           "expression": "expression_enabled", "expression_learning": "expression_learning_enabled",
           "memes.semantic": "meme_semantics_enabled", "reading.web": "web_reading_enabled",
           "memory.recall": "memory_enabled"}
MODES = ("owner", "propose", "self")
CURRENT = ("PROPOSED", "ACTIVE")
DAILY_COMPANION_CHANGES = 3
RESUME_HOUR = 8


def _table(store):
    return bool(store.one(
        'SELECT 1 FROM pg_catalog.pg_class WHERE relnamespace=current_schema()::regnamespace AND '
        "relname='capability_delegations' AND relkind IN ('r','p')"
    ))


def delegation(store, capability):
    row = store.one('SELECT capability_delegations.capability AS capability, '
        'capability_delegations.mode AS mode, capability_delegations.allow_pause AS allow_pause, '
        'capability_delegations.ranges_json AS ranges_json, capability_delegations.updated_at AS '
        'updated_at FROM capability_delegations AS capability_delegations WHERE '
        'capability_delegations.capability = %s', (capability,)) \
        if _table(store) else None
    if not row:
        return {"capability": capability, "mode": "owner", "allow_pause": False, "ranges": {}}
    return {"capability": capability, "mode": row["mode"], "allow_pause": bool(row["allow_pause"]),
            "ranges": json.loads(row["ranges_json"]), "updated_at": row["updated_at"]}


def _bounds(settings, capability, field, ranges):
    """Allowed inclusive [low, high] for an integer field, never past the configured value."""
    kind, attribute = DELEGABLE[capability][field]
    configured = getattr(settings, attribute)
    if kind == "floor":
        low = ranges.get(field, {}).get("min")
        return (configured if low is None else max(0, min(low, configured)), configured)
    high = ranges.get(field, {}).get("max")
    return (configured, configured if high is None
            else max(configured, min(high, _ceiling_limit(attribute, configured))))


def _ceiling_limit(attribute, configured):
    if attribute.endswith("_minutes"):
        return 1440
    if attribute.endswith("_seconds"):
        return 7 * 86400
    return max(configured * 10, 10)


def _number(value, configured):
    return type(value) is int or (isinstance(configured, float) and type(value) is float)


def validate_ranges(settings, capability, ranges):
    if not isinstance(ranges, dict):
        raise CoreError("DELEGATION_RANGES_INVALID", 400)
    clean = {}
    for field, bound in ranges.items():
        kind = DELEGABLE[capability].get(field, (None,))[0]
        if kind not in {"floor", "ceiling"} or not isinstance(bound, dict):
            raise CoreError("DELEGATION_RANGES_INVALID", 400)
        key = "min" if kind == "floor" else "max"
        value = bound.get(key)
        attribute = DELEGABLE[capability][field][1]
        configured = getattr(settings, attribute)
        if set(bound) != {key} or not _number(value, configured):
            raise CoreError("DELEGATION_RANGES_INVALID", 400)
        if (kind == "floor" and not 0 <= value <= configured) or (
            kind == "ceiling" and not configured <= value <= _ceiling_limit(attribute, configured)
        ):
            raise CoreError("DELEGATION_RANGE_OUTSIDE_CONFIGURATION", 400)
        clean[field] = {key: value}
    return clean


def _close(store, capability, now, state, *, fields=None, current=CURRENT):
    marks = ",".join('%s' for _ in current)
    query = (f"UPDATE capability_overrides SET state=%s,decided_at=%s WHERE capability=%s "
             f"AND state IN ({marks})")
    params = [state, now, capability, *current]
    if fields is not None:
        query += " AND field IN (" + ",".join('%s' for _ in fields) + ")"
        params += list(fields)
    return store.execute(query, params)


def set_delegation(store, settings, capability, mode, allow_pause, ranges, now):
    """Owner decision; call inside a transaction."""
    if capability not in DELEGABLE or mode not in MODES or type(allow_pause) is not bool:
        raise CoreError("DELEGATION_INVALID", 400)
    clean = validate_ranges(settings, capability, ranges)
    store.execute(
        'INSERT INTO capability_delegations VALUES (%s, %s, %s, %s, %s) ON CONFLICT(capability) DO '
        'UPDATE SET mode = excluded.mode, allow_pause = excluded.allow_pause, ranges_json = '
        'excluded.ranges_json, updated_at = excluded.updated_at',
        (capability, mode, int(allow_pause), encode(clean), now),
    )
    if mode == "owner":
        _close(store, capability, now, "REVERTED", current=("ACTIVE",))
        _close(store, capability, now, "REJECTED", current=("PROPOSED",))
    else:
        if not allow_pause:
            _close(store, capability, now, "REVERTED", fields=["pause"])
        for field in DELEGABLE[capability]:
            if field != "pause" and field not in clean:
                _close(store, capability, now, "REVERTED", fields=[field])
    store.audit("delegation.configured", capability, "console", now, mode=mode,
                allow_pause=allow_pause, ranges=clean)
    return delegation(store, capability)


def _resume_at(settings, now):
    local = datetime.fromisoformat(now).astimezone(ZoneInfo(settings.timezone))
    resume = local.replace(hour=RESUME_HOUR, minute=0, second=0, microsecond=0)
    if resume <= local:
        resume += timedelta(days=1)
    return resume.astimezone(ZoneInfo("UTC")).isoformat(timespec="microseconds")


def _check(store, settings, capability, field, value, now):
    current = delegation(store, capability)
    if capability not in DELEGABLE or field not in DELEGABLE[capability]:
        raise CoreError("DELEGATION_FIELD_NOT_ALLOWED", 400)
    if current["mode"] == "owner":
        raise CoreError("DELEGATION_OWNER_ONLY", 403)
    if field == "pause":
        if not current["allow_pause"] or value is not True:
            raise CoreError("DELEGATION_FIELD_NOT_ALLOWED", 403)
        attribute = ENABLED.get(capability)
        if attribute and not getattr(settings, attribute):
            raise CoreError("DELEGATION_CAPABILITY_DISABLED", 409)
        return current
    if field not in current["ranges"] or not _number(
        value, getattr(settings, DELEGABLE[capability][field][1])
    ):
        raise CoreError("DELEGATION_FIELD_NOT_ALLOWED", 403)
    low, high = _bounds(settings, capability, field, current["ranges"])
    if not low <= value <= high:
        raise CoreError("DELEGATION_VALUE_OUT_OF_RANGE", 400)
    return current


def submit(store, settings, capability, field, value, reason, now, *, suggestion_id=None):
    """The companion's adjustment; call inside a transaction. Returns the stored row."""
    current = _check(store, settings, capability, field, value, now)
    local = datetime.fromisoformat(now).astimezone(ZoneInfo(settings.timezone))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        ZoneInfo("UTC")).isoformat(timespec="microseconds")
    if store.one('SELECT COUNT(*) AS n FROM capability_overrides WHERE capability = %s AND '
        'created_at >= %s',
                 (capability, day))["n"] >= DAILY_COMPANION_CHANGES:
        raise CoreError("DELEGATION_DAILY_LIMIT", 429)
    state = "ACTIVE" if current["mode"] == "self" else "PROPOSED"
    if state == "ACTIVE":
        _close(store, capability, now, "SUPERSEDED", fields=[field], current=("ACTIVE",))
    override_id = uid("adjust")
    store.insert(
        "capability_overrides", override_id=override_id, capability=capability, field=field,
        value_json=encode(value), suggestion_id=suggestion_id, reason=reason[:500], state=state,
        created_at=now, decided_at=now if state == "ACTIVE" else None,
        expires_at=_resume_at(settings, now) if field == "pause" else None,
    )
    store.audit("delegation.adjusted" if state == "ACTIVE" else "delegation.proposed",
                override_id, capability, now, field=field)
    return store.one('SELECT capability_overrides.override_id AS override_id, '
        'capability_overrides.capability AS capability, capability_overrides.field AS field, '
        'capability_overrides.value_json AS value_json, capability_overrides.suggestion_id AS '
        'suggestion_id, capability_overrides.reason AS reason, capability_overrides.state AS '
        'state, capability_overrides.created_at AS created_at, capability_overrides.decided_at AS '
        'decided_at, capability_overrides.expires_at AS expires_at FROM capability_overrides AS '
        'capability_overrides WHERE capability_overrides.override_id = %s', (override_id,))


def check_decision(store, settings, override_id, decision):
    """Validate an owner decision without writing; returns the row and the next state."""
    row = store.one('SELECT capability_overrides.override_id AS override_id, '
        'capability_overrides.capability AS capability, capability_overrides.field AS field, '
        'capability_overrides.value_json AS value_json, capability_overrides.suggestion_id AS '
        'suggestion_id, capability_overrides.reason AS reason, capability_overrides.state AS '
        'state, capability_overrides.created_at AS created_at, capability_overrides.decided_at AS '
        'decided_at, capability_overrides.expires_at AS expires_at FROM capability_overrides AS '
        'capability_overrides WHERE capability_overrides.override_id = %s', (override_id,)) \
        if _table(store) else None
    if not row:
        raise CoreError("DELEGATION_ADJUSTMENT_NOT_FOUND", 404)
    if decision == "approve" and row["state"] == "PROPOSED":
        # Approval re-checks the current range: configuration may have changed since.
        if delegation(store, row["capability"])["mode"] == "owner":
            raise CoreError("DELEGATION_OWNER_ONLY", 403)
        _check_value_only(store, settings, row["capability"], row["field"],
                          json.loads(row["value_json"]))
        return row, "ACTIVE"
    if decision == "reject" and row["state"] == "PROPOSED":
        return row, "REJECTED"
    if decision == "revert" and row["state"] == "ACTIVE":
        return row, "REVERTED"
    raise CoreError("DELEGATION_DECISION_INVALID", 409)


def decide(store, settings, override_id, decision, now):
    """Owner approval, rejection or revert; call inside a transaction."""
    row, state = check_decision(store, settings, override_id, decision)
    if state == "ACTIVE":
        _close(store, row["capability"], now, "SUPERSEDED", fields=[row["field"]],
               current=("ACTIVE",))
        store.execute('UPDATE capability_overrides SET expires_at = %s WHERE override_id = %s',
                      (_resume_at(settings, now) if row["field"] == "pause" else None,
                       override_id))
    store.execute('UPDATE capability_overrides SET state = %s, decided_at = %s WHERE override_id = '
        '%s',
                  (state, now, override_id))
    store.audit("delegation.decided", override_id, row["capability"], now, decision=decision)
    return {"state": state}


def _check_value_only(store, settings, capability, field, value):
    current = delegation(store, capability)
    if field == "pause":
        if not current["allow_pause"]:
            raise CoreError("DELEGATION_FIELD_NOT_ALLOWED", 403)
        return
    if field not in current["ranges"]:
        raise CoreError("DELEGATION_FIELD_NOT_ALLOWED", 403)
    low, high = _bounds(settings, capability, field, current["ranges"])
    if not low <= value <= high:
        raise CoreError("DELEGATION_VALUE_OUT_OF_RANGE", 400)


def _active(store, capability, field, now):
    if not _table(store):
        return None
    row = store.one(
        'SELECT capability_overrides.override_id AS override_id, capability_overrides.capability '
        'AS capability, capability_overrides.field AS field, capability_overrides.value_json AS '
        'value_json, capability_overrides.suggestion_id AS suggestion_id, '
        'capability_overrides.reason AS reason, capability_overrides.state AS state, '
        'capability_overrides.created_at AS created_at, capability_overrides.decided_at AS '
        'decided_at, capability_overrides.expires_at AS expires_at FROM capability_overrides AS '
        'capability_overrides WHERE capability_overrides.capability = %s AND '
        "capability_overrides.field = %s AND capability_overrides.state = 'ACTIVE' ORDER BY "
        'capability_overrides.created_at DESC NULLS LAST LIMIT 1', (capability, field),
    )
    if row and row["expires_at"] and row["expires_at"] <= now:
        return None
    return row


def effective(store, settings, capability, field, now):
    """Configured value unless an active, still-permitted companion adjustment lowers it."""
    configured = getattr(settings, DELEGABLE[capability][field][1])
    row = _active(store, capability, field, now)
    if not row:
        return configured
    current = delegation(store, capability)
    if current["mode"] == "owner" or field not in current["ranges"]:
        return configured
    value = json.loads(row["value_json"])
    low, high = _bounds(settings, capability, field, current["ranges"])
    return value if low <= value <= high else configured


def paused(store, capability, now):
    row = _active(store, capability, "pause", now)
    return bool(row) and delegation(store, capability)["allow_pause"] \
        and delegation(store, capability)["mode"] != "owner"


def expire(store, now):
    if _table(store):
        store.execute("UPDATE capability_overrides SET state = 'EXPIRED', decided_at = %s WHERE "
            "state = 'ACTIVE' AND NOT expires_at IS NULL AND expires_at <= %s",
                      (now, now))


def discard_after_restore(store, now):
    """A restored backup cannot prove later adjustments; return to the owner's configuration."""
    if _table(store):
        store.execute("UPDATE capability_overrides SET state = 'DISCARDED', decided_at = %s WHERE "
            "state IN ('PROPOSED', 'ACTIVE')", (now,))


def erase_suggestions(store, suggestion_ids, now):
    """Erased reflection evidence also withdraws the adjustments that cited it."""
    if not _table(store):
        return
    for suggestion_id in suggestion_ids:
        store.execute(
            "UPDATE capability_overrides SET reason = '', state = CASE WHEN state = 'ACTIVE' THEN "
            "'REVERTED' WHEN state = 'PROPOSED' THEN 'REJECTED' ELSE state END, decided_at = "
            'COALESCE(decided_at, %s) WHERE suggestion_id = %s',
            (now, suggestion_id),
        )


def prompt_scope(store, settings, now):
    """What the companion may adjust today, for the reflection prompt; empty when nothing."""
    scope = []
    for capability, fields in WIRED.items():
        current = delegation(store, capability)
        if current["mode"] == "owner":
            continue
        for field in fields:
            if field == "pause":
                if current["allow_pause"]:
                    scope.append({"capability": capability, "field": "pause", "value": True})
            elif field in current["ranges"]:
                low, high = _bounds(settings, capability, field, current["ranges"])
                scope.append({"capability": capability, "field": field, "min": low, "max": high,
                              "current": effective(store, settings, capability, field, now)})
    return scope


def view(store, settings, now):
    expire(store, now)
    rows = []
    if _table(store):
        rows = store.rows(
            'SELECT override_id, capability, field, value_json, suggestion_id, reason, state, '
            'created_at, decided_at, expires_at FROM capability_overrides ORDER BY created_at DESC '
            'NULLS LAST LIMIT 40'
        )
    capabilities = []
    for capability, fields in DELEGABLE.items():
        current = delegation(store, capability)
        limits = {}
        for field, (kind, attribute) in fields.items():
            if kind == "pause":
                continue
            limits[field] = {"kind": kind, "configured": getattr(settings, attribute),
                             "effective": effective(store, settings, capability, field, now)}
        capabilities.append({**current, "fields": list(fields), "limits": limits,
                             "wired": sorted(WIRED.get(capability, ())),
                             "paused": paused(store, capability, now)})
    return {"capabilities": capabilities, "adjustments": [
        {**row, "value": json.loads(row.pop("value_json"))} for row in rows]}
