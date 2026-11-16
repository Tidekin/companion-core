"""Owner-only moments: Life's settled first-person wording, shown in the native client.

A moment is saved exactly as Life settled it (no second generation), only under the
`moments` purpose, at most `moments_daily_limit` per local day and never in quiet hours.
It never flows to QZone or any public channel. Forgetting goes through the native Ledger;
erasing any of its sources erases it.
"""

import json
from contextlib import nullcontext
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from companion import owner_authority
from companion.contracts import CoreError
from companion.paid_budget import sha
from companion.privacy import advance_epoch, redact, sensitive
from companion.store import encode, uid

STATES = ("PUBLISHED", "HIDDEN", "ERASED")


def _day_bounds(settings, now):
    zone = ZoneInfo(settings.timezone)
    local = datetime.fromisoformat(now).astimezone(zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def save(store, settings, clock, data, body):
    """Consume one retained PRIVATE_MOMENT result inside the caller's Life transaction."""
    from companion.social import quiet_hours

    now = clock()
    owner_authority.current(store, settings, clock, purpose="moments", authority=body.authority)
    result, job = data["result"], data["job"]
    text = result["text"]
    if sha({"text": text}) != body.expected_content_sha256:
        raise CoreError("LIFE_DRAFT_CHANGED", 409)
    if not text.strip() or sensitive(text) or redact(text, settings) != text:
        raise CoreError("MOMENT_SENSITIVE_CONTENT", 400)
    prior = store.one("SELECT moment_id, state FROM moments WHERE source_job_id = %s",
                      (job["job_id"],))
    if prior:
        return {"moment_id": prior["moment_id"], "state": prior["state"], "reused": True}
    if quiet_hours(settings, now):
        raise CoreError("MOMENT_QUIET_HOURS", 409)
    start, end = _day_bounds(settings, now)
    today = store.one(
        "SELECT COUNT(*) AS n FROM moments WHERE created_at >= %s AND created_at < %s",
        (start, end))["n"]
    if today >= settings.moments_daily_limit:
        raise CoreError("MOMENT_DAILY_LIMIT", 429)
    moment_id = uid("moment")
    sources = sorted({*data["decision"].source_event_ids, job["event_id"]})
    store.insert(
        "moments", moment_id=moment_id, source_job_id=job["job_id"], text=text,
        content_sha256=sha({"text": text}), trigger="LIFE",
        source_event_ids_json=encode(sources), state="PUBLISHED", created_at=now,
        updated_at=now,
    )
    store.audit("moment.published", moment_id, job["job_id"], now, trigger="LIFE")
    return {"moment_id": moment_id, "state": "PUBLISHED", "reused": False}


def rows(store, *, before=None, limit=30, include_hidden=False):
    states = ("PUBLISHED", "HIDDEN") if include_hidden else ("PUBLISHED",)
    return store.rows(
        "SELECT moment_id, text, trigger, state, created_at, liked_at, source_event_ids_json "
        "FROM moments "
        "WHERE state = ANY(%s::text[]) AND (CAST(%s AS TEXT) IS NULL OR created_at < %s) "
        "ORDER BY created_at DESC, moment_id DESC LIMIT %s",
        (list(states), before, before, limit),
    )


def items(store, **kwargs):
    return [{"id": row["moment_id"], "text": row["text"], "trigger": row["trigger"],
             "state": row["state"], "created_at": row["created_at"],
             "liked": row["liked_at"] is not None,
             "source_event_ids": json.loads(row["source_event_ids_json"])}
            for row in rows(store, **kwargs)]


def set_liked(store, moment_id, liked, now):
    """The owner's like; liking twice or unliking twice changes nothing."""
    with nullcontext() if store.in_transaction else store.transaction(control_write=True):
        row = store.one("SELECT state, liked_at FROM moments WHERE moment_id = %s FOR UPDATE",
                        (moment_id,))
        if not row or row["state"] != "PUBLISHED":
            raise CoreError("MOMENT_NOT_FOUND", 404)
        if (row["liked_at"] is not None) != liked:
            store.execute("UPDATE moments SET liked_at = %s, updated_at = %s WHERE moment_id = %s",
                          (now if liked else None, now, moment_id))
            store.audit("moment.liked" if liked else "moment.unliked", moment_id, moment_id, now)
        return {"moment_id": moment_id, "liked": liked}


def memories(store, settings, now):
    """Published moments from this calendar day in earlier years (那年今日); no model call."""
    zone = ZoneInfo(settings.timezone)
    today = datetime.fromisoformat(now).astimezone(zone)
    found = []
    for row in rows(store, limit=200, before=_day_bounds(settings, now)[0]):
        local = datetime.fromisoformat(row["created_at"]).astimezone(zone)
        if (local.month, local.day) == (today.month, today.day) and local.year < today.year:
            found.append({"id": row["moment_id"], "text": row["text"], "trigger": row["trigger"],
                          "state": row["state"], "created_at": row["created_at"],
                          "liked": row["liked_at"] is not None,
                          "years_ago": today.year - local.year,
                          "source_event_ids": json.loads(row["source_event_ids_json"])})
    return found


def signature(store):
    """Changes whenever a moment is published, hidden, liked or erased."""
    row = store.one(
        "SELECT COUNT(*) AS n, COUNT(liked_at) AS liked, "
        "COUNT(*) FILTER (WHERE state = 'PUBLISHED') AS shown, MAX(updated_at) AS at "
        "FROM moments")
    return [row["n"], row["liked"], row["shown"], row["at"]]


def set_hidden(store, moment_id, hidden, now):
    with nullcontext() if store.in_transaction else store.transaction(control_write=True):
        row = store.one("SELECT state FROM moments WHERE moment_id = %s FOR UPDATE",
                        (moment_id,))
        if not row or row["state"] == "ERASED":
            raise CoreError("MOMENT_NOT_FOUND", 404)
        state = "HIDDEN" if hidden else "PUBLISHED"
        if row["state"] != state:
            store.execute("UPDATE moments SET state = %s, updated_at = %s WHERE moment_id = %s",
                          (state, now, moment_id))
            store.audit("moment.hidden" if hidden else "moment.shown", moment_id, moment_id, now)
        return {"moment_id": moment_id, "state": state}


def forget(store, ledger, moment_id, now):
    """The owner forgets one moment through the native Ledger, so restores replay it."""
    if not store.one("SELECT 1 FROM moments WHERE moment_id = %s AND state <> 'ERASED'",
                     (moment_id,)):
        raise CoreError("MOMENT_NOT_FOUND", 404)
    ledger.forget_moment(store, moment_id, now)
    return {"moment_id": moment_id, "state": "ERASED"}


def erase_ids(store, moment_ids):
    for moment_id in moment_ids:
        store.execute(
            "UPDATE moments SET text = '', content_sha256 = '', source_event_ids_json = '[]', "
            "state = 'ERASED' WHERE moment_id = %s", (moment_id,))
    if moment_ids:
        advance_epoch(store)


def erase(store, event_ids):
    """A moment dies with its Life job's event or any of its sources."""
    events = sorted(set(event_ids))
    if not events:
        return
    affected = [row["moment_id"] for row in store.rows(
        "SELECT m.moment_id FROM moments m LEFT JOIN jobs j ON j.job_id = m.source_job_id "
        "WHERE m.state <> 'ERASED' AND (j.event_id = ANY(%s::text[]) OR EXISTS("
        "SELECT 1 FROM json_array_elements_text(m.source_event_ids_json::json) AS s(v) "
        "WHERE s.v = ANY(%s::text[])))", (events, events))]
    erase_ids(store, affected)
