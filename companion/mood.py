"""Rebuildable companion PAD expression hints over retained native evidence.

No model, event writer, scheduler, personality store or execution authority lives here.
Weights are conservative product rules, not measurements of anyone's emotions.
"""

import json
import re
from contextlib import nullcontext
from datetime import UTC, datetime

from companion import goal_plan, mood_appraisals, owner_authority, owner_input
from companion.contracts import CoreError
from companion.memory_authority import source_allowed
from companion.memory_context import MemorySources, check_version, read_version
from companion.memory_projections import MemoryProjections
from companion.paid_budget import sha
from companion.store import after, encode, uid

VERSION = "mood-2"
RESET_KEY = "mood_reset_at"
PAUSE_KEY = "mood_paused"
DIMENSIONS = ("pleasure", "arousal", "dominance")
HALF_LIVES = {"immediate": 3 * 3600, "undertone": 3 * 86400}
WINDOW = 14 * 86400
LIMIT = 256
INTERACTION_REPLY_WINDOW = 3600
# Whole original messages only. Quotations, commands and generated replies are excluded.
FEEDBACK = {
    **{
        prefix + ending: ("HELPFUL_FEEDBACK", (0.08, 0.02, 0.02))
        for prefix in ("你刚才的回答", "这个回答")
        for ending in ("很有帮助", "帮到我了", "让我很开心")
    },
    **{
        prefix + ending: ("THANKS_FOR_HELP", (0.06, 0.01, 0.02))
        for prefix in ("谢谢你的", "谢谢你刚才的")
        for ending in ("帮助", "回答")
    },
    **{
        prefix + ending: ("UNHELPFUL_FEEDBACK", (-0.04, 0.01, -0.02))
        for prefix in ("你刚才的回答", "这个回答")
        for ending in ("没帮上忙", "让我有点失望", "让我不舒服")
    },
}
# These are explicit owner appraisals of the shared discussion, not emotion
# labels extracted from her reply. Match the entire original utterance only.
INTERACTIONS = {
    text + ending: (reason, delta)
    for reason, delta, texts in (
        (
            "SHARED_IDEA_INTEREST",
            (0.025, 0.02, 0.0),
            (
                "这个想法很有意思",
                "这个想法挺有意思",
                "这个想法挺有趣的",
                "这个思路很有意思",
            ),
        ),
        (
            "DISCUSSION_CURIOSITY",
            (0.01, 0.025, 0.0),
            (
                "这个问题让我很好奇",
                "我对这个问题很好奇",
                "这个问题值得继续探讨",
                "我想继续探讨这个问题",
            ),
        ),
        (
            "PERSPECTIVE_SURPRISE",
            (0.0, 0.03, 0.0),
            (
                "这个角度我没想到",
                "这个角度我真没想到",
                "这个角度真没想到",
            ),
        ),
        ("PERSPECTIVE_SURPRISE", (0.02, 0.03, 0.0), ("这个思路让我眼前一亮",)),
        (
            "VIEW_DISAGREEMENT",
            (0.0, 0.01, 0.0),
            (
                "这个观点我不认同",
                "我对这个观点有不同看法",
                "这个观点我有不同看法",
                "我不太同意这个看法",
            ),
        ),
        (
            "DISCUSSION_PROGRESS",
            (0.025, 0.0, 0.025),
            (
                "这个问题终于理清了",
                "这样就说得通了",
                "这次讨论有进展了",
                "我们把这个问题说清楚了",
            ),
        ),
    )
    for text in texts
    for ending in ("", "，我们继续聊聊", "，想再听听你的看法")
}
CHAT_SIGNALS = FEEDBACK | INTERACTIONS
FILTER_RULES = (
    "这是你自身的暂时表达参考，不是主人的情绪、固定人格或真实情绪体验的证明。"
    "仅可轻微影响措辞、关注点和表达意愿；不必提心情或每轮表演感受，不指定回复。"
    "优先理解当前语境和主人明确偏好；严肃求助、危险、隐私及主人要求暂停时不用心情滤镜。"
    "不覆盖事实、理性判断、承诺、权限、预算或消息次数。"
    "来源只说明记录中的互动或线上阅读，不据此推断主人冷落你，也不编造线下经历；"
    "心情数值不限制你表达在乎和想念。"
)


def stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("mood timestamp requires timezone")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def reset_target(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
        raise CoreError("MOOD_RESET_ID_REQUIRED", 400)
    return "mood_reset:" + request_id


def reset_record(sequence, request_id, now):
    return {
        "seq": sequence,
        "erasure_id": uid("erase"),
        "memory_id": reset_target(request_id),
        "memory_ids": [],
        "event_ids": [],
        "at": stamp(now),
        "target_type": "mood_reset",
    }


def validate_reset_record(record):
    try:
        if (
            record.get("target_type") != "mood_reset"
            or record.get("event_ids") != []
            or record.get("memory_ids") != []
            or not record["memory_id"].startswith("mood_reset:")
        ):
            raise ValueError("invalid mood reset scope")
        reset_target(record["memory_id"][len("mood_reset:") :])
        stamp(record["at"])
    except (CoreError, KeyError, TypeError, AttributeError) as error:
        raise ValueError("invalid mood reset") from error


def reset_at(store):
    row = store.one("SELECT value FROM runtime_meta WHERE key = %s", (RESET_KEY,))
    try:
        return stamp(row["value"]) if row else None
    except (TypeError, ValueError) as error:
        raise CoreError("MOOD_RESET_INVALID", 503) from error


def apply_reset(store, record):
    from companion.privacy import advance_epoch

    if not store.in_transaction:
        raise RuntimeError("MOOD_RESET_REQUIRES_TRANSACTION")
    validate_reset_record(record)
    previous = reset_at(store)
    cutoff = max(stamp(record["at"]), previous or stamp(record["at"]))
    if cutoff != previous:
        store.execute(
            "INSERT INTO runtime_meta (key, value) VALUES (%s, %s) ON CONFLICT(key) DO "
            "UPDATE SET value = excluded.value, record_sequence = DEFAULT",
            (RESET_KEY, cutoff),
        )
        advance_epoch(store)
    return {"state": "RESET", "reset_at": cutoff}


def vector(values):
    return dict(zip(DIMENSIONS, (round(x, 6) for x in values), strict=True))


def fold(signals, at):
    """Bounded impulses with time-based exponential relaxation; replay is deterministic."""
    values = {name: [0.0, 0.0, 0.0] for name in HALF_LIVES}
    previous = at
    seen = set()
    for item in signals:
        time = item["at"]
        if seen:
            seconds = (
                datetime.fromisoformat(time) - datetime.fromisoformat(previous)
            ).total_seconds()
            for layer, half in HALF_LIVES.items():
                values[layer] = [x * 2 ** (-seconds / half) for x in values[layer]]
        seen.add(item["independent_id"])
        for i, delta in enumerate(item["delta"]):
            # One event never changes a dimension by more than .08; saturation is smooth.
            values["immediate"][i] += delta * (1 - abs(values["immediate"][i]) / 0.35)
            if len(seen) >= 3:
                values["undertone"][i] += delta * 0.12 * (1 - abs(values["undertone"][i]) / 0.15)
        previous = time
    seconds = max(
        0.0, (datetime.fromisoformat(at) - datetime.fromisoformat(previous)).total_seconds()
    )
    for layer, half in HALF_LIVES.items():
        values[layer] = [x * 2 ** (-seconds / half) for x in values[layer]]
    return {
        **{name: vector(value) for name, value in values.items()},
        "expression": vector([values["immediate"][i] + values["undertone"][i] for i in range(3)]),
    }


class MoodService:
    def __init__(self, memory, clock):
        self.memory, self.clock = memory, clock
        self.store, self.settings = memory.store, memory.settings

    def _permitted(self, purpose):
        if not owner_authority.required(self.store, self.settings):
            return True
        try:
            owner_authority.current(self.store, self.settings, self.clock, purpose=purpose)
            return True
        except CoreError:
            return False

    def _lineage(self, key, sources, source_check=None):
        rows = list(sources._lineage(key))
        if not MemoryProjections._live_lineage(rows, sources):
            return None
        for row in rows:
            if row["source"] in {"qq.onebot", "native.chat"} and not (
                source_check(row["event_id"])
                if source_check is not None
                else source_allowed(
                    self.store,
                    self.settings,
                    self.clock,
                    row["event_id"],
                    "expression",
                )
            ):
                return None
        return rows

    def _signals(self, at, allowed):
        s, cfg = self.store, self.settings
        if not owner_authority.required(s, cfg):
            return self._collect_signals(at, allowed, None)
        if not s.in_transaction:
            # Reuse the native history reader's synchronous batch on one DB state.
            # This batch never spans an await or a later context revalidation.
            return s.read_or_write(lambda: self._signals(at, allowed))
        return owner_input.batch_sources(
            s, lambda unchanged: self._collect_signals(at, allowed, unchanged))

    def _collect_signals(self, at, allowed, unchanged):
        s, cfg = self.store, self.settings
        sources = MemorySources(self.memory, self.clock())
        view = None
        if owner_authority.required(s, cfg):
            view = owner_authority.current(s, cfg, self.clock, purpose="expression")
            sources.cutoff = max(
                sources.cutoff,
                datetime.fromisoformat(
                    after(self.clock(), -view.policy.source_retention_days * 86400)
                ),
            )

        originals = {encode(view.authority.as_dict()): view} if view else {}

        def batched():
            return view and unchanged is not None and unchanged()

        def permitted(purpose):
            if not batched():
                return self._permitted(purpose)
            # The batch view is the current one on this DB state; the boundary below
            # rechecks live authority and independent files once for every signal.
            return (
                cfg.owner_runtime.mode == "ENABLED"
                and purpose in view.policy.purposes
                and (
                    view.policy.expires_at is None
                    or datetime.fromisoformat(self.clock()) < view.policy.expires_at
                )
            )

        def evidence(ref):
            key = encode(ref)
            if key not in originals:
                originals[key] = owner_authority.evidence(s, cfg, ref)
            return originals[key]

        def source_check(event_id):
            if not batched():
                return source_allowed(s, cfg, self.clock, event_id, "expression")
            now = self.clock()
            if (
                cfg.owner_runtime.mode != "ENABLED"
                or "expression" not in view.policy.purposes
                or view.policy.expires_at is not None
                and datetime.fromisoformat(now) >= view.policy.expires_at
            ):
                raise CoreError("OWNER_AUTHORITY_EXPIRED", 403)
            receipt = s.one(
                "SELECT value FROM runtime_meta WHERE key = %s", (owner_input.INPUT + event_id,)
            )
            try:
                original = json.loads(receipt["value"]).get("authority") if receipt else None
                owner_input._source(
                    s,
                    cfg,
                    view,
                    event_id,
                    now,
                    history=original != view.authority.as_dict(),
                    verify_capture=lambda value: owner_input._verify(s, cfg, value, evidence),
                )
            except (CoreError, ValueError, TypeError, AttributeError):
                return False
            return True

        cutoff = max(after(at, -WINDOW), reset_at(s) or after(at, -WINDOW))
        candidates = s.rows(
            (
                (
                    "SELECT e.event_id,e.occurred_at AS at,'feedback' AS "
                    "kind FROM events e WHERE e.source IN ('qq.onebot','native.chat') AND "
                    "e.actor_id='owner' AND e.redacted=0 AND "
                    "e.event_type='message.received' AND e.ingested_at>%s "
                    'AND e.ingested_at<=%s AND e.occurred_at>%s AND '
                    'e.occurred_at<=%s AND length(e.body_json)<=16000 AND '
                    'rtrim(trim((CASE WHEN e.body_json IS JSON THEN '
                    "(e.body_json::json#>>'{text}') END)),'。！! ') IN "
                    '(SELECT value FROM json_array_elements_text(%s::json) '
                    'AS ids(value)) UNION ALL SELECT w.source_event_id,'
                    "w.updated_at,'watch' FROM watch_ledger w WHERE "
                    "w.state='JUDGED' AND w.rating<>0 AND "
                    "w.depth<>'title_only' AND w.updated_at>%s AND "
                    'w.updated_at<=%s UNION ALL SELECT g.result_event_id,'
                    "g.updated_at,'goal' FROM goals g WHERE g.state='DONE' "
                    'AND g.redacted=0 AND g.result_event_id IS NOT NULL AND '
                    'g.updated_at>%s AND g.updated_at<=%s ORDER BY at DESC,'
                    'event_id,kind LIMIT %s'
                )
            ),
            (cutoff, at, cutoff, at, encode(list(CHAT_SIGNALS)), cutoff, at, cutoff, at, LIMIT + 1),
        )
        signals, used = [], set()
        for candidate in candidates[:LIMIT]:
            key, kind = candidate["event_id"], candidate["kind"]
            lineage = self._lineage(key, sources, source_check)
            if not lineage:
                continue
            event = next(row for row in lineage if row["event_id"] == key)
            # Reset and lookback follow the original event too: a delayed review cannot revive it.
            if (
                not cutoff < stamp(event["ingested_at"]) <= at
                or not cutoff < stamp(event["occurred_at"]) <= at
            ):
                continue
            body = json.loads(event["body_json"])
            identity, detail, reason, delta = key, None, None, None
            if kind == "feedback":
                if (
                    event["actor_id"] != "owner"
                    or body.get("control_command")
                    or body.get("diary_command")
                    or body.get("quote")
                    or body.get("rich")
                ):
                    continue
                text = body.get("text", "")
                if not isinstance(text, str):
                    continue
                utterance = text.strip().rstrip("。！! ")
                rule = CHAT_SIGNALS.get(utterance)
                if not rule:
                    continue
                # Feedback refers to an actual delivered reply in this conversation.
                reply = s.one(
                    (
                        'SELECT o.message_id AS message_id, o.origin_event_id '
                        'AS origin_event_id, o.conversation_id AS '
                        'conversation_id, o.action_type AS action_type, '
                        'o.content_json AS content_json, o.content_sha256 AS '
                        'content_sha256, o.approval_id AS approval_id, o.state '
                        'AS state, o.delivery_key AS delivery_key, '
                        'o.lease_owner AS lease_owner, o.lease_until AS '
                        'lease_until, o.attempt_count AS attempt_count, '
                        'o.provider_message_id AS provider_message_id, '
                        'o.last_error_code AS last_error_code, o.created_at AS '
                        'created_at, o.updated_at AS updated_at, o.sent_at AS '
                        'sent_at, o.lease_token AS lease_token, '
                        'o.next_attempt_at AS next_attempt_at, o.reply_group_id '
                        'AS reply_group_id, o.segment_index AS segment_index, '
                        'o.segment_count AS segment_count, o.reply_group_sha256 '
                        'AS reply_group_sha256, o.segment_interval_ms AS '
                        'segment_interval_ms, o.delivery_lane AS delivery_lane, '
                        'o.native_ack_json AS native_ack_json, o.authority_json '
                        'AS authority_json FROM outbox AS o JOIN events AS '
                        'original ON original.event_id = o.origin_event_id '
                        "WHERE o.conversation_id = %s AND o.state = 'SENT' AND "
                        "o.action_type = 'qq.private.reply' AND o.sent_at <= %s "
                        'AND original.record_sequence < (SELECT record_sequence '
                        'AS rowid FROM events AS events WHERE events.event_id = '
                        '%s) ORDER BY o.sent_at DESC NULLS LAST, '
                        'o.record_sequence DESC NULLS LAST LIMIT 1'
                    ),
                    (event["conversation_id"], candidate["at"], key),
                )
                if not reply or not (
                    prior := self._lineage(
                        reply["origin_event_id"],
                        sources,
                        source_check,
                    )
                ):
                    continue
                # A possibly delivered newer reply makes the feedback target ambiguous.
                # Never silently attribute it to an older known SENT reply instead.
                uncertain = s.one(
                    (
                        'SELECT 1 FROM outbox AS o JOIN events AS original ON '
                        'original.event_id = o.origin_event_id WHERE '
                        'o.conversation_id = %s AND o.action_type = '
                        "'qq.private.reply' AND o.state IN ('SENDING', "
                        "'DELIVERY_UNCERTAIN') AND o.created_at >= %s AND "
                        'o.created_at <= %s AND original.record_sequence < '
                        '(SELECT record_sequence AS rowid FROM events WHERE '
                        'event_id = %s) LIMIT 1'
                    ),
                    (event["conversation_id"], reply["created_at"], candidate["at"], key),
                )
                if uncertain:
                    continue
                if utterance in INTERACTIONS:
                    if body.get("media") or body.get("shared_content"):
                        continue
                    # Deictic phrases such as "this idea" need a recent, adjacent
                    # delivered turn. Never guess across a later topic or a gap.
                    preceding = s.one(
                        (
                            'SELECT event_id FROM events WHERE conversation_id = %s '
                            "AND source IN ('qq.onebot','native.chat') AND actor_id = 'owner' AND "
                            "event_type IN ('message.received', "
                            "'message.media_received') AND record_sequence < "
                            '(SELECT record_sequence AS rowid FROM events WHERE '
                            'event_id = %s) ORDER BY record_sequence DESC NULLS '
                            'LAST LIMIT 1'
                        ),
                        (event["conversation_id"], key),
                    )
                    elapsed = (
                        datetime.fromisoformat(candidate["at"])
                        - datetime.fromisoformat(reply["sent_at"])
                    ).total_seconds()
                    if (
                        not preceding
                        or preceding["event_id"] != reply["origin_event_id"]
                        or not 0 <= elapsed <= INTERACTION_REPLY_WINDOW
                    ):
                        continue
                    kind = "interaction"
                lineage += prior
                identity = "reply:" + reply["message_id"]
                detail, reason, delta = sha(reply), rule[0], rule[1]
            elif kind == "watch":
                if not permitted("exploration"):
                    continue
                detail = s.one(
                    "SELECT watch_ledger.entry_id AS entry_id, "
                    "watch_ledger.source_event_id AS source_event_id, watch_ledger.platform AS "
                    "platform, watch_ledger.item_ref AS item_ref, watch_ledger.depth AS depth, "
                    "watch_ledger.state AS state, watch_ledger.attempts AS attempts, "
                    "watch_ledger.lease_until AS lease_until, watch_ledger.next_attempt_at AS "
                    "next_attempt_at, watch_ledger.model_request_id AS model_request_id, "
                    "watch_ledger.rating AS rating, watch_ledger.take AS take, "
                    "watch_ledger.topic_tags_json AS topic_tags_json, watch_ledger.error_code AS "
                    "error_code, watch_ledger.created_at AS created_at, watch_ledger.updated_at AS "
                    "updated_at FROM watch_ledger AS watch_ledger WHERE "
                    "watch_ledger.source_event_id = %s",
                    (key,),
                )
                observation = s.one(
                    "SELECT observations.event_id AS event_id, "
                    "observations.adapter_id AS adapter_id, observations.source_kind AS "
                    "source_kind, observations.result_status AS result_status, "
                    "observations.source_sha256 AS source_sha256, observations.canonical_url AS "
                    "canonical_url, observations.device_id AS device_id, observations.session_id "
                    "AS session_id, observations.consent_id AS consent_id, "
                    "observations.evidence_json AS evidence_json, "
                    "observations.captured_privacy_epoch AS captured_privacy_epoch, "
                    "observations.redacted AS redacted, observations.created_at AS created_at FROM "
                    "observations AS observations WHERE observations.event_id = %s",
                    (key,),
                )
                usage = s.one(
                    "SELECT state, scenario FROM model_usage WHERE request_id = %s",
                    (detail["model_request_id"],),
                )
                if (
                    event["source"] not in {"observation.bilibili", "observation.heybox"}
                    or not observation
                    or observation["redacted"]
                    or observation["result_status"] not in {"FETCHED", "WATCHED"}
                    or not observation["source_sha256"]
                    or not observation["canonical_url"]
                    or not usage
                    or usage["state"] != "SETTLED"
                    or usage["scenario"] != "watch_review"
                    or type(detail["rating"]) is not int
                    or not -2 <= detail["rating"] <= 2
                ):
                    continue
                identity = "read:" + observation["canonical_url"]
                reason, delta = "COMPLETED_READ_REVIEW", (detail["rating"] * 0.03, 0.0, 0.0)
                detail = sha([detail, observation])
            else:
                if not permitted("goals"):
                    continue
                from companion.goals import GoalEngine

                engine = GoalEngine(s, cfg, self.memory.ledger, self.clock)
                projection = MemoryProjections(self.memory)
                goals = s.rows(
                    "SELECT goals.goal_id AS goal_id, goals.kind AS kind, goals.topic AS topic, "
                    "goals.scope_json AS scope_json, goals.proposal_key AS proposal_key, "
                    "goals.state AS state, goals.priority AS priority, goals.next_due_at AS "
                    "next_due_at, goals.last_error_code AS last_error_code, goals.version AS "
                    "version, goals.result_event_id AS result_event_id, goals.created_at AS "
                    "created_at, goals.updated_at AS updated_at, goals.redacted AS redacted, "
                    "goals.reflection_suggestion_id AS reflection_suggestion_id, "
                    "goals.authority_json AS authority_json FROM goals AS goals WHERE "
                    "goals.result_event_id = %s AND goals.state = 'DONE' AND goals.redacted = 0 "
                    "ORDER BY goals.goal_id NULLS FIRST",
                    (key,),
                )
                found = None
                for goal in goals:
                    roots = engine.source_ids(goal["goal_id"])
                    steps = s.rows(
                        "SELECT goal_steps.step_id AS step_id, goal_steps.goal_id AS goal_id, "
                        "goal_steps.position AS position, goal_steps.scope_json AS scope_json, "
                        "goal_steps.state AS state, goal_steps.attempts AS attempts, "
                        "goal_steps.max_attempts AS max_attempts, goal_steps.lease_token AS "
                        "lease_token, goal_steps.lease_until AS lease_until, "
                        "goal_steps.captured_privacy_epoch AS captured_privacy_epoch, "
                        "goal_steps.outcome_event_id AS outcome_event_id, "
                        "goal_steps.last_error_code AS last_error_code, goal_steps.created_at AS "
                        "created_at, goal_steps.updated_at AS updated_at FROM goal_steps AS "
                        "goal_steps WHERE goal_steps.goal_id = %s ORDER BY goal_steps.position "
                        "NULLS FIRST",
                        (goal["goal_id"],),
                    )
                    if (
                        not roots
                        or not steps
                        or goal_plan.unknown(engine, goal["goal_id"])
                        or [x["position"] for x in steps] != list(range(1, len(steps) + 1))
                        or steps[-1]["outcome_event_id"] != key
                        or not all(
                            projection._completed(goal, step, roots, sources, allowed)
                            for step in steps
                        )
                    ):
                        continue
                    expanded = [self._lineage(root, sources, source_check) for root in roots]
                    expanded += [
                        self._lineage(step["outcome_event_id"], sources, source_check)
                        for step in steps
                    ]
                    if any(not value for value in expanded):
                        continue
                    lineage += [row for value in expanded for row in value]
                    found = sha([goal, steps])
                    break
                if not found:
                    continue
                receipt = s.one(
                    "SELECT canonical_url FROM observations WHERE event_id = %s", (key,)
                )
                identity, detail = "read:" + receipt["canonical_url"], found
                reason, delta = "VERIFIED_GOAL_COMPLETION", (0.03, 0.0, 0.04)
            ids = sorted({row["event_id"] for row in lineage})
            if allowed is not None and not set(ids) <= set(allowed):
                continue
            signal = {
                "event_id": key,
                "source_event_ids": ids,
                "kind": kind,
                "at": stamp(candidate["at"]),
                "reason": reason,
                "delta": list(delta),
                "independent_id": identity,
                "fingerprint": sha([lineage, detail]),
            }
            # Help feedback and discussion appraisals about the same delivered
            # reply are one experience, even when their reason codes differ.
            category = "feedback" if kind == "interaction" else kind
            if (category, identity) not in used:
                signals.append(signal)
                used.add((category, identity))
        signals += self._appraisals(
            at, allowed, cutoff, sources, {x["event_id"] for x in signals}, source_check
        )
        # The same observation's review and verified goal result form one bounded impulse.
        merged = {}
        for item in signals:
            key = item["event_id"]
            if key in merged:
                prior = merged[key]
                prior["delta"] = [
                    max(-0.08, min(0.08, a + b))
                    for a, b in zip(prior["delta"], item["delta"], strict=True)
                ]
                prior["source_event_ids"] = sorted(
                    set(prior["source_event_ids"] + item["source_event_ids"])
                )
                prior["fingerprint"] = sha([prior["fingerprint"], item["fingerprint"]])
                prior["at"] = max(prior["at"], item["at"])
                prior["reason"], prior["kind"] = "REVIEWED_GOAL_COMPLETION", "experience"
            else:
                merged[key] = item
        ordered = sorted(
            merged.values(), key=lambda item: (item["at"], item["event_id"], item["kind"])
        )
        published = ordered[-LIMIT:]
        if view:
            if unchanged is None and s.in_write_transaction:
                # Under sustained unrelated commits, batching conservatively falls
                # back to independent source reads. Recheck published lineage after
                # collection too: a source may have changed after its first read.
                for key in sorted({key for item in published for key in item["source_event_ids"]}):
                    if not self._lineage(key, sources):
                        raise CoreError("PRIVACY_CHANGED", 409)
            # Independent journals, STOP and time remain live at the batch boundary.
            owner_authority.current(
                s, cfg, self.clock, purpose="expression", authority=view.authority
            )
        return published, len(candidates) > LIMIT or len(ordered) > LIMIT

    def _appraisals(self, at, allowed, cutoff, sources, taken, source_check=None):
        """Model-judged owner wording; an exact-phrase signal on the same message wins.

        A row counts once it was recorded by `at`, but acts from when the owner said it:
        a delayed extraction neither refreshes old words nor moves them to another day.
        """
        s = self.store
        signals = []
        for row in s.rows(
            "SELECT mood_appraisals.event_id AS event_id, mood_appraisals.reason AS reason, "
            "mood_appraisals.quote_start AS quote_start, mood_appraisals.quote_length AS "
            "quote_length, mood_appraisals.quote_sha256 AS quote_sha256, "
            "mood_appraisals.request_id AS request_id, mood_appraisals.created_at AS created_at, "
            "mood_appraisals.target_message_id AS target_message_id, mood_appraisals.target_sha256 "
            "AS target_sha256 FROM mood_appraisals AS mood_appraisals WHERE "
            "mood_appraisals.created_at > %s AND mood_appraisals.created_at <= %s ORDER BY "
            "mood_appraisals.created_at NULLS FIRST, mood_appraisals.event_id NULLS FIRST",
            (cutoff, at),
        ):
            key = row["event_id"]
            if key in taken or not mood_appraisals.still_valid(s, row):
                continue
            lineage = self._lineage(key, sources, source_check)
            if not lineage:
                continue
            event = next(item for item in lineage if item["event_id"] == key)
            if (
                not cutoff < stamp(event["ingested_at"]) <= at
                or not cutoff < stamp(event["occurred_at"]) <= at
            ):
                continue
            if row["target_message_id"]:
                # Hurt about a reply also rests on that reply's own input.
                target = s.one(
                    "SELECT action_type, origin_event_id FROM outbox WHERE message_id = %s",
                    (row["target_message_id"],),
                )
                if target["action_type"] == "qq.private.reply":
                    prior = self._lineage(target["origin_event_id"], sources, source_check)
                    if not prior:
                        continue
                    lineage = lineage + prior
            ids = sorted({item["event_id"] for item in lineage})
            if allowed is not None and not set(ids) <= set(allowed):
                continue
            signals.append(
                {
                    "event_id": key,
                    "source_event_ids": ids,
                    "kind": "appraisal",
                    "at": stamp(event["occurred_at"]),
                    "reason": row["reason"],
                    "delta": list(mood_appraisals.REASONS[row["reason"]]),
                    "independent_id": "appraisal:" + key,
                    "fingerprint": sha([lineage, row]),
                }
            )
        return mood_appraisals.capped(signals, self.settings.timezone)

    def snapshot(self, *, at=None, allowed=None):
        at = stamp(at or self.clock())
        if at > stamp(self.clock()):
            raise CoreError("MOOD_TIME_INVALID", 400)
        captured = read_version(self.memory)
        paused = self.store.one(
            "SELECT 1 FROM runtime_meta WHERE key = %s AND value = '1'", (PAUSE_KEY,)
        )
        enabled = bool(self.settings.mood_enabled)
        active = enabled and not paused and self._permitted("expression")
        signals, truncated = self._signals(at, allowed) if active else ([], False)
        # Repeated evidence from the same real event cannot manufacture a lasting undertone.
        state = fold(signals, at)
        if truncated:
            state["undertone"] = vector([0.0, 0.0, 0.0])
            state["expression"] = state["immediate"].copy()
        result = {
            "version": VERSION,
            "evaluated_at": at,
            "enabled": enabled,
            "paused": bool(paused),
            "active": active,
            "reset_at": reset_at(self.store),
            **state,
            "signals": signals,
            "truncated": truncated,
            "confidence": "limited" if signals else "unknown",
            "half_life_seconds": HALF_LIVES,
            "source_event_ids": sorted(
                {key for item in signals for key in item["source_event_ids"]}
            ),
            "allowed": sorted(allowed) if allowed is not None else None,
        }
        result["trend"] = [
            {"at": point, **fold([s for s in signals if s["at"] <= point], point)}
            for point in (after(at, -86400), after(at, -21600), after(at, -3600), at)
        ]
        if truncated:
            for point in result["trend"]:
                point["undertone"] = vector([0.0, 0.0, 0.0])
                point["expression"] = point["immediate"].copy()
        check_version(self.memory, captured)
        return result

    def revalidate(self, proof):
        if self.snapshot(at=proof["evaluated_at"], allowed=proof["allowed"]) != proof:
            raise CoreError("PRIVACY_CHANGED", 409)

    def reference(self, *, at=None, allowed=None, text=""):
        from companion.expression import SERIOUS
        from companion.privacy import sensitive

        if SERIOUS.search(text) or sensitive(text):
            return None
        view = self.snapshot(at=at, allowed=allowed)
        if not view["signals"] or not any(view["expression"].values()):
            return None
        return {
            "rules": FILTER_RULES,
            "state": view["expression"],
            "immediate": view["immediate"],
            "undertone": view["undertone"],
            "confidence": view["confidence"],
            "evaluated_at": view["evaluated_at"],
            "causes": [
                {key: item[key] for key in ("event_id", "at", "reason", "source_event_ids")}
                for item in view["signals"][-8:]
            ],
            "proof": view,
        }

    def control(self, action, requester, *, request_id=None):
        if requester != self.settings.owner_qq_id:
            raise CoreError("OWNER_REQUIRED", 403)
        if action not in {"pause", "resume", "clear", "recompute"}:
            raise CoreError("MOOD_ACTION_INVALID", 400)
        with nullcontext() if self.store.in_transaction else self.store.transaction(
            control_write=True
        ):
            if action == "clear":
                return self.memory.ledger.reset_mood(self.store, request_id, self.clock())
            if action in {"pause", "resume"}:
                from companion.privacy import advance_epoch

                self.store.execute(
                    "INSERT INTO runtime_meta (key, value) VALUES (%s, %s) ON CONFLICT(key) DO "
                    "UPDATE SET value = excluded.value, record_sequence = DEFAULT",
                    (PAUSE_KEY, "1" if action == "pause" else "0"),
                )
                advance_epoch(self.store)
            return {
                "state": {"pause": "PAUSED", "resume": "ACTIVE", "recompute": "RECOMPUTED"}[action],
                "mood": self.snapshot(),
            }


def service_for_store(store, settings, clock, ledger=None):
    from companion.memory import Memory
    from companion.privacy import ErasureLedger

    return MoodService(
        Memory(store, settings, ledger or ErasureLedger(settings.erasure_ledger)), clock
    )
