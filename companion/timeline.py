"""On-demand private recent evidence, rebuilt from SQLite; never a prompt or queue."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from companion.contracts import CoreError
from companion.diary import day_bounds, is_management_command
from companion.observation import expired_sources
from companion.privacy import epoch, redact, sensitive
from companion.store import after


def recent_timeline(store, settings, now, requester, *, topic, days=7, limit=40):
    if requester != settings.owner_qq_id:
        raise CoreError("OWNER_REQUIRED", 403)
    if (
        type(days) is not int
        or not 1 <= days <= 7
        or type(limit) is not int
        or not 1 <= limit <= 40
        or not isinstance(topic, str)
        or not 2 <= len(topic.strip()) <= 80
    ):
        raise CoreError("INVALID_TIMELINE_QUERY", 400)
    captured = epoch(store)
    local_day = datetime.fromisoformat(now).astimezone(ZoneInfo(settings.timezone)).date()
    start = day_bounds(local_day - timedelta(days=days - 1), settings.timezone)[0]
    cutoff = after(now, -86400 * settings.raw_event_retention_days)
    query = topic.strip().casefold()
    rows = store.rows(
        'SELECT e.event_id, e.actor_id, e.event_type, e.source, e.occurred_at, e.conversation_id, '
        'e.body_json, o.result_status, o.source_kind FROM events AS e JOIN conversations AS c '
        'USING (conversation_id) LEFT JOIN observations AS o USING (event_id) WHERE e.redacted = 0 '
        "AND e.privacy_scope = 'private' AND c.scope = 'private' AND c.owner_actor_id = 'owner' "
        'AND e.occurred_at >= %s AND e.occurred_at <= %s AND e.ingested_at >= %s AND '
        "LENGTH(e.body_json) <= 16000 AND ((e.actor_id = 'owner' AND e.source = 'qq.onebot' AND "
        "e.event_type IN ('message.received', 'message.media_received')) OR (e.event_type = "
        "'content.observed' AND o.redacted = 0)) ORDER BY e.occurred_at DESC NULLS LAST, "
        'e.event_id NULLS FIRST LIMIT 201',
        (start, now, cutoff),
    )
    items = []
    for row in rows[:200]:
        if expired_sources(store, [row["event_id"]], cutoff):
            continue
        try:
            body = json.loads(row["body_json"])
        except ValueError:
            continue
        if not isinstance(body, dict) or body.get("control_command") or body.get("diary_command"):
            continue
        text = body.get("text", "")
        if (
            not isinstance(text, str)
            or not text.strip()
            or (row["actor_id"] == "owner" and is_management_command(text))
            or query not in text.casefold()
            or sensitive(text)
            or redact(text, settings) != text
        ):
            continue
        item = {
            "kind": "conversation" if row["actor_id"] == "owner" else "observation",
            "subject": "owner" if row["actor_id"] == "owner" else "self",
            "event_id": row["event_id"],
            "occurred_at": row["occurred_at"],
            "source": row["source"],
            "excerpt": text[:300],
            "evidence_level": "RECEIVED"
            if row["actor_id"] == "owner"
            else ("SIMULATED_" if row["source"] == "observation.simulation" else "")
            + (row["result_status"] or "UNKNOWN"),
            "episode_ids": [
                r["episode_id"]
                for r in store.rows(
                    'SELECT ep.episode_id FROM episode_events AS ee JOIN episodes AS ep USING '
                    "(episode_id) WHERE ee.event_id = %s AND ep.status = 'ACTIVE' ORDER BY "
                    'ep.episode_id NULLS FIRST LIMIT 5',
                    (row["event_id"],),
                )
            ],
            "replies": [],
        }
        if item["kind"] == "conversation":
            for reply in store.rows(
                'SELECT message_id, state, sent_at, content_json FROM outbox WHERE origin_event_id '
                "= %s AND conversation_id = %s AND action_type = 'qq.private.reply' AND "
                'LENGTH(content_json) <= 16000 ORDER BY created_at NULLS FIRST, message_id NULLS '
                'FIRST LIMIT 5',
                (row["event_id"], row["conversation_id"]),
            ):
                fact = {key: reply[key] for key in ("message_id", "state", "sent_at")}
                if reply["state"] == "SENT":
                    try:
                        content = json.loads(reply["content_json"])
                    except ValueError:
                        content = None
                    sent = content.get("text", "") if isinstance(content, dict) else ""
                    if (
                        isinstance(sent, str)
                        and not sensitive(sent)
                        and redact(sent, settings) == sent
                    ):
                        fact["excerpt"] = sent[:300]
                item["replies"].append(fact)
        items.append(item)
    goals = []
    for goal in store.rows(
        'SELECT goal_id, kind, topic, state, last_error_code, result_event_id, updated_at FROM '
        'goals WHERE redacted = 0 ORDER BY updated_at DESC NULLS LAST, goal_id NULLS FIRST LIMIT 50'
    ):
        if (
            query not in goal["topic"].casefold()
            or sensitive(goal["topic"])
            or redact(goal["topic"], settings) != goal["topic"]
            or (goal["state"] in {"DONE", "FAILED", "CANCELLED"} and goal["updated_at"] < start)
        ):
            continue
        ids = [
            r["event_id"]
            for r in store.rows(
                'SELECT event_id FROM goal_evidence WHERE goal_id = %s ORDER BY event_id NULLS '
                'FIRST',
                (goal["goal_id"],),
            )
        ]
        if not ids or expired_sources(store, ids, cutoff):
            continue
        available = store.one(
            'SELECT COUNT(*) AS n FROM events AS e JOIN conversations AS c USING (conversation_id) '
            'WHERE e.event_id IN (SELECT value COLLATE "C" FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s '
            "AS JSON)) AS parsed(value)) AND e.redacted = 0 AND e.privacy_scope = 'private' AND "
            "c.scope = 'private' AND c.owner_actor_id = 'owner' AND e.occurred_at <= %s",
            (json.dumps(ids), now),
        )["n"]
        if available != len(ids):
            continue
        if goal["result_event_id"] and expired_sources(store, [goal["result_event_id"]], cutoff):
            continue
        goals.append({**goal, "source_event_ids": ids})
    if captured != epoch(store):
        raise CoreError("PRIVACY_CHANGED", 409)
    return {
        "projection_version": 1,
        "privacy_epoch": captured,
        "timezone": settings.timezone,
        "since": start,
        "as_of": now,
        "items": items[:limit],
        "goals": goals[:10],
        "truncated": len(rows) > 200 or len(items) > limit or len(goals) > 10,
        "model_calls": 0,
        "auto_share": False,
    }
