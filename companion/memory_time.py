"""Source-bound event times and query predicates in the existing Memory pipeline."""

import json
from datetime import UTC, datetime

from companion.contracts import CoreError
from companion.store import encode


def iso(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def predicate(query):
    """A fixed SQL fragment and bound arguments, never model-supplied SQL."""
    if query is None:
        return "", []
    if query.basis == "occurred":
        return ("AND m.event_time_json IS NOT NULL "
                "AND (m.event_time_json::json->>'start')::timestamptz < %s::timestamptz "
                "AND (m.event_time_json::json->>'end')::timestamptz > %s::timestamptz ",
                [iso(query.end), iso(query.start)])
    if query.basis == "known":
        bound = iso(query.as_of)
        return ("AND m.created_at<=%s AND (m.valid_from IS NULL OR m.valid_from<=%s) "
                "AND (m.valid_to IS NULL OR m.valid_to>%s) AND EXISTS "
                "(SELECT 1 FROM memory_evidence tv JOIN events te USING(event_id) "
                "WHERE tv.memory_id=m.memory_id AND tv.polarity=1 "
                "AND te.ingested_at<=%s AND te.occurred_at<=%s) ", [bound] * 5)
    return ("AND EXISTS (SELECT 1 FROM memory_evidence tv JOIN events te USING(event_id) "
            "WHERE tv.memory_id=m.memory_id AND tv.polarity=1 "
            "AND te.occurred_at>=%s AND te.occurred_at<%s) ",
            [iso(query.start), iso(query.end)])


def validate_query(query, now):
    if query and now is None:
        raise CoreError("MEMORY_TIME_CLOCK_REQUIRED", 400)
    if query and query.as_of and query.as_of > datetime.fromisoformat(now):
        raise CoreError("FUTURE_MEMORY_SNAPSHOT", 400)


def supported(row, events, query):
    """Recheck temporal eligibility against the same live source snapshot."""
    if query is None:
        return True
    if query.basis == "known":
        bound = query.as_of
        return (datetime.fromisoformat(row["created_at"]) <= bound
                and (not row["valid_from"] or datetime.fromisoformat(row["valid_from"]) <= bound)
                and (not row["valid_to"] or bound < datetime.fromisoformat(row["valid_to"]))
                and any(datetime.fromisoformat(event["ingested_at"]) <= bound
                        and datetime.fromisoformat(event["occurred_at"]) <= bound
                        for event in events))
    if query.basis == "mentioned":
        return any(query.start <= datetime.fromisoformat(event["occurred_at"]) < query.end
                   for event in events)
    value = metadata(row, events)
    return bool(value and datetime.fromisoformat(value["start"]) < query.end
                and datetime.fromisoformat(value["end"]) > query.start)


def metadata(row, events):
    raw = row.get("event_time_json")
    if raw is None:
        return None
    value = json.loads(raw)
    if value["event_id"] not in {event["event_id"] for event in events}:
        return None
    return {key: value[key] for key in ("start", "end", "precision", "assertion", "quote")}


def record(candidate):
    value = getattr(candidate, "event_time", None)
    if value is None:
        return None
    return encode({**value.model_dump(mode="json"),
                   "start": iso(value.start), "end": iso(value.end),
                   "event_id": candidate.evidence_event_id,
                   "reference_id": candidate.evidence_reference_id})


def erase(store, event_ids):
    store.execute("UPDATE memories SET event_time_json=NULL WHERE event_time_json IS NOT NULL "
                  "AND event_time_json::json->>'event_id'=ANY(%s::text[])", (list(event_ids),))
