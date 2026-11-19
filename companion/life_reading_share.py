"""Project a positively judged, completed self read into its approved private conversation."""

from datetime import datetime

from companion import owner_authority
from companion.contracts import CoreError
from companion.paid_budget import sha
from companion.privacy import redact, sensitive
from companion.store import after

PREFIX = "own-reading:"

# Discovery, owner-sourced goals and unsettled opinions cannot supply independent
# sharing evidence. Use the same predicate at projection, commit and delivery.
_READS = """
    SELECT w.source_event_id AS event_id, w.entry_id, w.depth, w.rating, w.take,
           w.updated_at AS judged_at, w.model_request_id, o.canonical_url,
           encode(sha256(convert_to(o.evidence_json, 'UTF8')), 'hex') AS evidence_sha256,
           e.body_json::jsonb->>'title' AS title
    FROM watch_ledger w JOIN observations o ON o.event_id=w.source_event_id
    JOIN events e ON e.event_id=o.event_id
    JOIN model_usage u ON u.request_id=w.model_request_id
    WHERE w.state='JUDGED' AND w.rating>0 AND w.depth<>'title_only'
      AND u.scenario='watch_review' AND u.state='SETTLED'
      AND o.redacted=0 AND e.redacted=0 AND o.result_status='FETCHED' AND o.source_kind='text'
      AND w.item_ref=o.canonical_url
      AND e.source IN ('observation.bilibili','observation.heybox','observation.web')
      AND e.occurred_at >= %s AND w.updated_at <= %s
      AND EXISTS (
        SELECT 1 FROM goals g WHERE g.kind='self' AND g.state='DONE' AND g.redacted=0
        AND (g.result_event_id=e.event_id OR EXISTS (SELECT 1 FROM goal_steps st
             WHERE st.goal_id=g.goal_id AND st.state='DONE' AND st.outcome_event_id=e.event_id))
        AND NOT EXISTS (
          WITH RECURSIVE roots(id) AS (
            SELECT e.event_id UNION SELECT ge.event_id FROM goal_evidence ge
                                    WHERE ge.goal_id=g.goal_id
            UNION SELECT d.source_event_id FROM event_dependencies d
                  JOIN roots ON d.event_id=roots.id
          )
          SELECT 1 FROM roots JOIN events ancestor ON ancestor.event_id=roots.id
          WHERE ancestor.actor_id='owner'
        )
      )
"""


def conversation(store, settings, now):
    if not owner_authority.required(store, settings):
        return None
    try:
        view = owner_authority.current(store, settings, lambda: now, purpose="life")
        scope = view.policy.life_shadow
    except CoreError:
        return None
    if scope and scope.share_own_readings and scope.expires_at > datetime.fromisoformat(now):
        return scope.conversation_id
    return None


def candidates(store, settings, now):
    target = conversation(store, settings, now)
    if not target or not store.one(
        "SELECT 1 FROM conversations WHERE conversation_id=%s AND scope='private' "
        "AND owner_actor_id='owner' AND platform IN ('native.chat','qq.onebot')", (target,),
    ):
        return []
    rows = store.rows(_READS + """
        AND NOT EXISTS (SELECT 1 FROM life_topic_sources ts WHERE ts.event_id=e.event_id)
        AND NOT EXISTS (SELECT 1 FROM life_topics t WHERE t.source_key=%s || w.entry_id)
        ORDER BY w.updated_at DESC, w.entry_id LIMIT 32
        """, (after(now, -settings.life_loop.topic_ttl_seconds), now, PREFIX))
    return [{**row, "share_conversation": target} for row in rows]


def current(store, settings, now, topic, event_id):
    if conversation(store, settings, now) != topic["conversation_id"]:
        raise CoreError("LIFE_SHADOW_SCOPE_REQUIRED", 403)
    row = store.one(_READS + " AND w.entry_id=%s AND w.source_event_id=%s",
                    (after(now, -settings.life_loop.topic_ttl_seconds), now,
                     topic["source_key"].removeprefix(PREFIX), event_id))
    if not row:
        raise CoreError("LIFE_SOURCE_UNAVAILABLE", 409)
    return row


def with_judgement(source, row, settings):
    if not row["take"] or any(
        sensitive(text) or redact(text, settings) != text
        for text in (row["take"], row["title"] or "")
    ):
        raise CoreError("LIFE_SOURCE_UNAVAILABLE", 409)
    opinion = {key: row[key] for key in ("depth", "rating", "take", "judged_at")}
    proof = {key: row[key] for key in ("entry_id", "model_request_id", "canonical_url",
                                       "evidence_sha256")}
    return {**source, "title": (row["title"] or "")[:200], "own_reading": opinion,
            "source_sha256": sha([source["source_sha256"], opinion, proof])}
