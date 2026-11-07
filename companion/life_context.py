"""Bounded views of original evidence, never inferred owner ratings or new memory."""

import json

from companion import affect, interest, owner_authority
from companion.contracts import CoreError
from companion.paid_budget import sha
from companion.privacy import redact, sensitive
from companion.store import encode


def read_targets(settings, sources):
    from companion.content import shared_links
    from companion.sources import web_hosts

    hosts = web_hosts(settings)
    targets = []
    for source in sources:
        for token in [source.get("url", ""), *source["text"].split()]:
            for target in shared_links(token, hosts):
                if target not in targets:
                    targets.append(target)
                if len(targets) == 24:
                    return targets
    return targets


def permitted(store, settings, now, purpose):
    if not owner_authority.required(store, settings):
        return True
    try:
        owner_authority.current(store, settings, lambda: now, purpose=purpose)
        return True
    except CoreError:
        return False


def decision_context(store, settings, sources, authority_sha256, now, *, mood_at=None):
    """Read only evidence already admitted into this exact activity's source set."""
    ids = {source["event_id"] for source in sources}
    result = {"owner_feedback": [], "self_interests": [], "execution_outcomes": []}
    if any(source.get("kind") == "social_content" for source in sources):
        return result
    if settings.mood_enabled:
        from companion.mood import service_for_store

        mood = service_for_store(store, settings, lambda: now).reference(
            at=mood_at, allowed=ids, text="\n".join(s.get("text", "") for s in sources)
        )
        if mood:
            result["mood"] = mood
    if settings.affect_enabled and permitted(store, settings, now, "affect"):
        feedback = [
            affect.owner_feedback(store, settings, event_id, now) for event_id in sorted(ids)
        ]
        latest = {}
        for item in sorted(
            (x for x in feedback if x), key=lambda x: (x["occurred_at"], x["event_id"])
        ):
            latest[item["dimension"]] = item
        result["owner_feedback"] = [latest[key] for key in sorted(latest)]
    goals = store.rows(
        "SELECT DISTINCT g.goal_id AS goal_id, g.kind AS kind, g.topic AS topic, g.scope_json AS "
        "scope_json, g.proposal_key AS proposal_key, g.state AS state, g.priority AS priority, "
        "g.next_due_at AS next_due_at, g.last_error_code AS last_error_code, g.version AS version, "
        "g.result_event_id AS result_event_id, g.created_at AS created_at, g.updated_at AS "
        "updated_at, g.redacted AS redacted, g.reflection_suggestion_id AS "
        "reflection_suggestion_id, g.authority_json AS authority_json FROM goals AS g JOIN "
        "goal_evidence AS e ON g.goal_id = e.goal_id WHERE e.event_id IN (SELECT parsed.value "
        'COLLATE "C" AS _col_0 FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value)) '
        "AND g.kind = 'self' AND g.redacted = 0 ORDER BY g.updated_at DESC NULLS LAST, g.goal_id "
        "NULLS FIRST LIMIT 8",
        (encode(sorted(ids)),),
    )
    anchors = {}
    for goal in goals:
        roots = {
            row["event_id"]
            for row in store.rows(
                "SELECT event_id FROM goal_evidence WHERE goal_id = %s", (goal["goal_id"],)
            )
        }
        if not roots or not roots <= ids:
            continue
        if owner_authority.required(store, settings):
            if (
                not goal["authority_json"]
                or sha(json.loads(goal["authority_json"])) != authority_sha256
            ):
                continue
        steps = store.rows(
            "SELECT state, last_error_code, outcome_event_id FROM goal_steps WHERE "
            "goal_id = %s ORDER BY step_id NULLS FIRST",
            (goal["goal_id"],),
        )
        unknown = any(step["state"] == "EXECUTION_UNCERTAIN" for step in steps) or bool(
            store.one(
                (
                    (
                        'SELECT 1 FROM exploration_requests WHERE '
                        "state='UNKNOWN' AND (CASE WHEN goal_step_json IS JSON "
                        "THEN (goal_step_json::json#>>'{goal_id}') END)=%s "
                        'LIMIT 1'
                    )
                ),
                (goal["goal_id"],),
            )
        )
        if unknown or goal["state"] in {"DONE", "FAILED"}:
            result["execution_outcomes"].append(
                {
                    "goal_id": goal["goal_id"],
                    "version": goal["version"],
                    "state": "UNKNOWN" if unknown else goal["state"],
                    "failure_code": goal["last_error_code"],
                    "requires_reconciliation": unknown,
                    "source_event_ids": sorted(roots),
                }
            )
        if (
            goal["state"] != "DONE"
            or not goal["result_event_id"]
            or goal["result_event_id"] not in ids
            or not settings.memory_enabled
            or not permitted(store, settings, now, "memory")
        ):
            continue
        topic = goal["topic"][:80]
        if sensitive(topic) or redact(topic, settings) != topic:
            continue
        trait = store.one(
            "SELECT trait_id FROM traits WHERE category = 'interest' AND topic = %s", (topic,)
        )
        if not trait:
            continue
        anchor = anchors.setdefault(
            trait["trait_id"],
            {"topic": topic, "goal_id": goal["goal_id"], "results": set(), "sources": set()},
        )
        anchor["results"].add(goal["result_event_id"])
        anchor["sources"].update(roots | {goal["result_event_id"]})
    own_views = settings.watch_ledger.enabled and permitted(store, settings, now, "exploration")
    required = owner_authority.required(store, settings)
    for trait_id, anchor in sorted(anchors.items()):
        # A plan contributes its final completed result once, never one vote per step.
        # Earlier experiences of the same interest count only when this same approval
        # admitted their Life choice; her views need the exploration purpose.
        evidence = []
        for row in interest.exploration_evidence(store, settings, trait_id, now):
            if required:
                goal = store.one(
                    "SELECT authority_json FROM goals WHERE goal_id = %s", (row["goal_id"],)
                )
                if (
                    not goal["authority_json"]
                    or sha(json.loads(goal["authority_json"])) != authority_sha256
                ):
                    continue
            evidence.append(row if own_views else {**row, "judgement": None})
        if not anchor["results"] & {row["event_id"] for row in evidence}:
            continue
        state, sessions, days = interest.tendency(evidence, settings, now)
        result["self_interests"].append(
            {
                "namespace": "self.interest",
                "topic": anchor["topic"],
                "status": state,
                "trait_id": trait_id,
                "goal_id": anchor["goal_id"],
                "independent_sessions": sessions,
                "independent_days": days,
                "owner_suggested_sessions": sum(
                    row["origin"] == "owner_suggested" for row in evidence
                ),
                "view": interest.view(evidence, settings),
                "source_event_ids": sorted(anchor["sources"]),
            }
        )
    if settings.life_loop.autonomy_enabled:
        from companion.life_autonomy import enhance_decision_context

        result = enhance_decision_context(result)
    return result
