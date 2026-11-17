"""Owner-only moments: Life's settled first-person wording, shown in the native client.

A moment is saved exactly as Life settled it (no second generation), only under the
`moments` purpose, at most `moments_daily_limit` per local day and never in quiet hours.
It never flows to QZone or any public channel. Forgetting goes through the native Ledger;
erasing any of its sources erases it.
"""

import json
from contextlib import nullcontext
from datetime import datetime
from zoneinfo import ZoneInfo

from companion import owner_authority
from companion.contracts import CoreError
from companion.diary import day_bounds
from companion.paid_budget import sha
from companion.privacy import advance_epoch, redact, sensitive
from companion.store import encode, uid

STATES = ("PUBLISHED", "HIDDEN", "ERASED")
# A moment about something she just learned that she rated this important at extraction.
IMPORTANT = 0.9
CONTEXT_HEADING = "这条消息是在动态里（数据）："
REPLY_RULES = {
    "post": "主人发了一条自己的动态，这是你在下面的评论：像朋友看到对方的说说那样，简短自然地回应，"
            "不复述内容，不追问太多。",
    "comment": "主人在评论动态，你在同一条动态下回复：一两句，接着主人的话说，不另起话题；"
               "moment.author 是 companion 时那是你自己发的。",
}


def _day_bounds(settings, now):
    zone = ZoneInfo(settings.timezone)
    local = datetime.fromisoformat(now).astimezone(zone)
    return day_bounds(local.date(), settings.timezone)


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
    trigger = "MEMORY" if store.one(
        "SELECT 1 FROM memory_evidence v JOIN memories m USING (memory_id) "
        "WHERE v.event_id = ANY(%s::text[]) AND v.polarity = 1 AND m.importance >= %s "
        "AND m.status IN ('CONFIRMED','HYPOTHESIS') AND m.confidence > 0 LIMIT 1",
        (sources, IMPORTANT)) else "LIFE"
    store.insert(
        "moments", moment_id=moment_id, source_job_id=job["job_id"], text=text,
        content_sha256=sha({"text": text}), trigger=trigger,
        source_event_ids_json=encode(sources), state="PUBLISHED", created_at=now,
        updated_at=now,
    )
    store.audit("moment.published", moment_id, job["job_id"], now, trigger=trigger)
    from companion import moment_images

    drawing = moment_images.queue(store, settings, clock, moment_id, data["decision"],
                                  body.authority)
    return {"moment_id": moment_id, "state": "PUBLISHED", "reused": False,
            **({"image": "QUEUED"} if drawing else {})}


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
    """Changes whenever a moment is published, hidden, liked, erased or gets its picture."""
    row = store.one(
        "SELECT COUNT(*) AS n, COUNT(liked_at) AS liked, "
        "COUNT(*) FILTER (WHERE state = 'PUBLISHED') AS shown, MAX(updated_at) AS at "
        "FROM moments")
    thread = store.one(
        "SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE e.redacted = 1) AS gone, "
        "(SELECT COUNT(*) FROM outbox o JOIN events x ON x.event_id = o.origin_event_id "
        "WHERE x.source = 'native.chat' AND x.body_json::jsonb ? 'moment' "
        "AND o.state = 'SENT') AS replies FROM events e "
        "WHERE e.source = 'native.chat' AND e.body_json::jsonb ? 'moment'")
    pictures = store.one("SELECT COUNT(*) FILTER (WHERE state = 'READY') AS ready, "
                         "COUNT(*) AS n, MAX(updated_at) AS at FROM moment_images")
    return [row["n"], row["liked"], row["shown"], row["at"],
            thread["n"], thread["gone"], thread["replies"],
            pictures["ready"], pictures["n"], pictures["at"]]


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
    from companion import moment_images

    moment_images.erase(store, moment_ids)
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


_INPUTS = ("SELECT e.event_id, e.occurred_at, e.body_json, e.redacted, e.record_sequence "
           "FROM events e JOIN jobs j USING (event_id) WHERE e.source = 'native.chat' "
           "AND e.actor_id = 'owner' AND j.kind = 'chat' AND e.body_json::jsonb ? 'moment' ")


def _images(body):
    return [item["slot"] for item in body.get("shared_content", []) if item.get("kind") == "image"]


def _owner_post(row):
    body = json.loads(row["body_json"])
    return {"id": row["event_id"], "author": "owner", "text": body.get("text", ""),
            "trigger": "OWNER", "state": "PUBLISHED", "created_at": row["occurred_at"],
            "liked": False, "images": _images(body), "source_event_ids": [row["event_id"]]}


def owner_posts(store, *, before=None, limit=30):
    return [_owner_post(row) for row in store.rows(
        _INPUTS + "AND e.redacted = 0 AND e.body_json::jsonb #>> '{moment,kind}' = 'post' "
        "AND (CAST(%s AS TEXT) IS NULL OR e.occurred_at < %s) "
        "ORDER BY e.occurred_at DESC LIMIT %s", (before, before, limit))]


def require_target(store, moment_id):
    """A comment targets her published moment or the owner's standing post."""
    if target(store, moment_id) is None:
        raise CoreError("MOMENT_NOT_FOUND", 404)


def target(store, moment_id):
    """The moment a comment or post is about, or None once it is gone."""
    if moment_id.startswith("moment_"):
        row = store.one("SELECT moment_id, text, created_at, source_event_ids_json FROM moments "
                        "WHERE moment_id = %s AND state = 'PUBLISHED'", (moment_id,))
        return row and {"id": row["moment_id"], "author": "companion", "text": row["text"],
                        "created_at": row["created_at"],
                        "source_event_ids": json.loads(row["source_event_ids_json"])}
    row = store.one(_INPUTS + "AND e.event_id = %s AND e.redacted = 0 "
                    "AND e.body_json::jsonb #>> '{moment,kind}' = 'post'", (moment_id,))
    return row and _owner_post(row)


def thread_events(store, moment_id, *, before_event=None):
    """Owner comments under one moment, oldest first, optionally only before one event."""
    return store.rows(
        _INPUTS + "AND e.redacted = 0 AND e.body_json::jsonb #>> '{moment,kind}' = 'comment' "
        "AND e.body_json::jsonb #>> '{moment,moment_id}' = %s "
        "AND (CAST(%s AS TEXT) IS NULL OR e.record_sequence < (SELECT record_sequence FROM "
        "events WHERE event_id = %s)) ORDER BY e.record_sequence LIMIT 60",
        (moment_id, before_event, before_event))


def replies(store, event_id):
    """Her delivered reply text to one owner post or comment."""
    texts = []
    for row in store.rows("SELECT content_json FROM outbox WHERE origin_event_id = %s "
                          "AND state = 'SENT' ORDER BY record_sequence", (event_id,)):
        value = json.loads(row["content_json"] or "{}")
        if value.get("text"):
            texts.append(value["text"])
    return "\n".join(texts)


def comments(store, moment_id):
    """The thread under a moment: her reply to an owner post, then each comment and reply."""
    if target(store, moment_id) is None:
        raise CoreError("MOMENT_NOT_FOUND", 404)
    items = []
    if not moment_id.startswith("moment_") and (text := replies(store, moment_id)):
        items.append({"author": "companion", "text": text, "reply_to": moment_id})
    for row in thread_events(store, moment_id):
        body = json.loads(row["body_json"])
        items.append({"id": row["event_id"], "author": "owner", "text": body.get("text", ""),
                      "created_at": row["occurred_at"], "images": _images(body)})
        if text := replies(store, row["event_id"]):
            items.append({"author": "companion", "text": text, "reply_to": row["event_id"]})
    return items


def comment_counts(store, moment_ids):
    if not moment_ids:
        return {}
    return {row["moment_id"]: row["n"] for row in store.rows(
        "SELECT e.body_json::jsonb #>> '{moment,moment_id}' AS moment_id, COUNT(*) AS n "
        "FROM events e WHERE e.source = 'native.chat' AND e.redacted = 0 "
        "AND e.body_json::jsonb #>> '{moment,kind}' = 'comment' "
        "AND e.body_json::jsonb #>> '{moment,moment_id}' = ANY(%s::text[]) GROUP BY 1",
        (sorted(moment_ids),))}


def feed(store, *, before=None, limit=30):
    """Her moments and the owner's posts together, newest first, with comment counts."""
    from companion import moment_images

    mine = items(store, before=before, limit=limit)
    ready, pending = moment_images.slots(store, [item["id"] for item in mine])
    mine = [{**item, "author": "companion", "images": ready.get(item["id"], []),
             **({"image_pending": True} if item["id"] in pending else {})} for item in mine]
    merged = sorted([*mine, *owner_posts(store, before=before, limit=limit)],
                    key=lambda item: item["created_at"], reverse=True)[:limit]
    counts = comment_counts(store, [item["id"] for item in merged])
    return [{**item, "comment_count": counts.get(item["id"], 0)} for item in merged]


def latest_unseen(store, after):
    """Her newest published moment after a time, for the chat page's preview card."""
    row = store.one("SELECT moment_id, text, created_at FROM moments WHERE state = 'PUBLISHED' "
                    "AND (CAST(%s AS TEXT) IS NULL OR created_at > %s) "
                    "ORDER BY created_at DESC LIMIT 1", (after, after))
    return row and {"id": row["moment_id"], "text": row["text"], "created_at": row["created_at"]}
