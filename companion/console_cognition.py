"""Owner-session console access to existing cognition, life and executive services.

Every route reuses the native service the internal control plane already calls; the console
adds only its owner session, exact Origin, CSRF and operation receipt. Nothing here creates a
new decision path or recomputes history. Provider reasoning uses native SDK snapshots;
prompt bodies and credentials remain excluded.
"""

import json
import re
from typing import Literal

from fastapi import Header, Request
from pydantic import Field

from companion.affect import feedback_reset_at
from companion.cognition_read import CognitionRead
from companion.contracts import CoreError, StrictModel
from companion.diary import DiaryRepository
from companion.executive import Executive
from companion.executive_protocol import ToolDecision
from companion.goal_protocol import GoalContinuationDecision, GoalDecision
from companion.goals import GoalEngine
from companion.life_protocol import TopicClosure
from companion.memory_context import check_version, read_version
from companion.privacy import redact, sensitive
from companion.reflection import ReflectionDecision, ReflectionService
from companion.transport import digest

COGNITION_KINDS = ("wake", "life", "goal", "diary", "reflection", "nightly", "model", "rem")
GOAL_FIELDS = ("goal_id", "kind", "topic", "state", "priority", "next_due_at", "last_error_code",
               "version", "commitment_state", "deadline", "mode", "created_at", "updated_at",
               "reflection_suggestion_id", "steps")
REFLECTION_FIELDS = ("suggestion_id", "run_id", "kind", "content", "state", "created_at",
                     "updated_at", "local_date")
TOOL_LIMITS = ("max_cost_micro_usd", "max_model_calls", "max_input_bytes", "max_output_tokens",
               "timeout_seconds")
TOOL_FIELDS = ("task_id", "executor_id", "capability", "risk_level", "state", "scope_sha256",
               "reserved_micro_usd", "billed_micro_usd", "dispatched_at", "last_error_code",
               "created_at", "updated_at")


def tool_scope(core, row):
    """What an approval would release, read back from the exact scope bound to its digest.

    Approval binds `scope_sha256`; this view shows the same retained scope only when it still
    matches that digest, its owner sources are still live and the text passes redaction again.
    Endpoint URLs and credentials never appear; the executor target stays a digest.
    """
    if row["redacted"]:
        return {"state": "ERASED"}
    try:
        scope = json.loads(row["scope_json"] or "{}")
    except ValueError:
        return {"state": "UNAVAILABLE"}
    if not scope:
        return {"state": "ERASED"}
    if digest(scope) != row["scope_sha256"]:
        return {"state": "MISMATCH"}
    executive = core.executive
    sources = executive.sources(row["task_id"])
    unavailable = {"state": "UNAVAILABLE", "sources_live": False, "source_count": len(sources)}
    if not sources:
        return unavailable
    try:
        executive.validate_sources(sources)
    except CoreError:
        return unavailable
    args = scope.get("arguments") or {}
    view = {"state": "RECORDED", "capability": scope.get("capability"),
            "workspace": scope.get("workspace"), "sources_live": True,
            "source_count": len(sources),
            "limits": {key: scope.get(key) for key in TOOL_LIMITS}}
    for key in ("path", "text", "prompt"):
        value = args.get(key) or ""
        if not value:
            continue
        if sensitive(value) or redact(value, core.settings) != value:
            view[key] = {"state": "REDACTED", "chars": len(value)}
        else:
            view[key] = {"state": "RECORDED", "value": value, "chars": len(value)}
    return view


class AffectReset(StrictModel):
    confirm: Literal[True]


class MoodControl(StrictModel):
    action: Literal["pause", "resume", "clear", "recompute"]


class SourceGrantAction(StrictModel):
    action: Literal["revoke", "forget"]


class JargonForget(StrictModel):
    term: str = Field(min_length=1, max_length=12)


class StyleCandidateDecision(StrictModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str = Field(default="", max_length=200)


def register_console_cognition_routes(app, prefix, auth, write, reset_affect, grant_action):
    """Writes reuse the console's isolated connection, owner checks and native receipts."""

    @app.get(prefix + "/cognition", dependencies=auth)
    async def cognition(kind: str, before: str | None = None, record_id: str | None = None,
                        limit: int = 30):
        if kind not in COGNITION_KINDS or not 1 <= limit <= 50:
            raise CoreError("INVALID_COGNITION_QUERY", 400)
        if (before and len(before) > 160) or (record_id and len(record_id) > 160):
            raise CoreError("INVALID_COGNITION_QUERY", 400)
        return CognitionRead(app.state.core).page(kind=kind, limit=limit, before=before,
                                                 record_id=record_id)

    @app.get(prefix + "/affect", dependencies=auth)
    async def affect():
        core = app.state.core
        # A disabled feature also returns the neutral snapshot; say which one it is.
        return {**core.affect.snapshot(), "enabled": bool(core.settings.affect_enabled),
                "reset_at": feedback_reset_at(core.store)}

    @app.post(prefix + "/affect/reset", dependencies=auth)
    async def affect_reset(body: AffectReset, request: Request,
                           idempotency_key: str = Header(default="")):
        return await reset_affect(request, idempotency_key, body.model_dump())

    @app.get(prefix + "/mood", dependencies=auth)
    async def mood():
        return app.state.core.mood.snapshot()

    @app.post(prefix + "/mood/control", dependencies=auth)
    async def mood_control(body: MoodControl, request: Request,
                           idempotency_key: str = Header(default="")):
        def apply(core, owner_check):
            from companion.mood import service_for_store

            with core.store.transaction():
                owner_check()
                service = service_for_store(core.store, core.settings, core.clock, core.ledger)
                return service.control(
                    body.action, core.settings.owner_qq_id, request_id=idempotency_key)

        return await write(request, idempotency_key, body.model_dump(), apply)

    @app.get(prefix + "/goals", dependencies=auth)
    async def goals():
        # Scope URLs and authority snapshots stay in the native record.
        core = app.state.core
        items = []
        for row in core.goals.recent():
            item = {key: row[key] for key in GOAL_FIELDS if key in row}
            item["allowed_actions"] = []
            if core.settings.goals_enabled and row["state"] == "WAITING_INPUT":
                try:
                    # Reuse the native plan's source, authority, uncertainty and due checks.
                    plan = core.goals.plan(row["goal_id"])
                    if plan["version"] == row["version"]:
                        item["allowed_actions"] = plan["allowed_actions"]
                except CoreError:
                    # Historical inspection remains available when execution is blocked.
                    pass
            items.append(item)
        return {"items": items}

    @app.post(prefix + "/goals/{goal_id}/decision", dependencies=auth)
    async def goal_decision(goal_id: str, body: GoalDecision, request: Request,
                            idempotency_key: str = Header(default="")):
        def apply(core, owner_check):
            with core.store.transaction():
                owner_check()
                engine = GoalEngine(core.store, core.settings, core.ledger, core.clock)
                return (engine.accept(goal_id) if body.decision == "ACCEPT"
                        else engine.cancel(goal_id))

        return await write(request, idempotency_key, body.model_dump(), apply)

    @app.post(prefix + "/goals/{goal_id}/continuation", dependencies=auth)
    async def goal_continuation(goal_id: str, body: GoalContinuationDecision, request: Request,
                                idempotency_key: str = Header(default="")):
        def apply(core, owner_check):
            with core.store.transaction():
                owner_check()
                return GoalEngine(core.store, core.settings, core.ledger, core.clock).continue_plan(
                    goal_id, body)

        return await write(request, idempotency_key, body.model_dump(mode="json"), apply)

    @app.get(prefix + "/reflections", dependencies=auth)
    async def reflections():
        items = []
        for row in app.state.core.reflections.recent():
            try:
                evidence = json.loads(row["evidence_json"] or "[]")
            except ValueError:
                evidence = []
            items.append({**{key: row[key] for key in REFLECTION_FIELDS if key in row},
                          "evidence": [{"exact_quote": item.get("exact_quote", "")}
                                       for item in evidence if isinstance(item, dict)][:3]})
        return {"items": items}

    @app.post(prefix + "/reflections/{suggestion_id}/decision", dependencies=auth)
    async def reflection_decision(suggestion_id: str, body: ReflectionDecision, request: Request,
                                  idempotency_key: str = Header(default="")):
        def apply(core, owner_check):
            diary = DiaryRepository(core.store, core.settings, core.clock)
            return ReflectionService(core.store, core.settings, None, diary, core.ledger,
                core.clock).decide(suggestion_id, body.decision, owner_check=owner_check)

        return await write(request, idempotency_key, body.model_dump(), apply)

    @app.get(prefix + "/style-candidates", dependencies=auth)
    async def style_candidates():
        from companion import operator_config, style_distill

        core = app.state.core
        configuration = operator_config.view(core.store, core.settings)
        live = next(field["active_value"]["enabled"] for field in configuration["fields"]
                    if field["key"] == "style_live")
        return {"enabled": style_distill.enabled(core.settings),
                "live_enabled": live,
                "items": style_distill.items(core.store, core.clock())}

    @app.post(prefix + "/style-candidates/{pattern_sha256}/decision", dependencies=auth)
    async def style_candidate_decision(pattern_sha256: str, body: StyleCandidateDecision,
                                       request: Request,
                                       idempotency_key: str = Header(default="")):
        from companion import style_distill

        if not re.fullmatch(r"[a-f0-9]{64}", pattern_sha256):
            raise CoreError("STYLE_CANDIDATE_NOT_FOUND", 404)

        def apply(core, owner_check):
            with core.store.transaction():
                owner_check()
                return style_distill.decide(core.store, core.settings, core.clock(),
                                            pattern_sha256, approve=body.decision == "APPROVE",
                                            reason=body.reason)

        return await write(request, idempotency_key, body.model_dump(), apply)

    @app.get(prefix + "/jargon", dependencies=auth)
    async def jargon_items():
        from companion import jargon

        core = app.state.core
        return {"enabled": jargon.enabled(core.settings), "items": jargon.items(core.store)}

    @app.post(prefix + "/jargon/forget", dependencies=auth)
    async def jargon_forget(body: JargonForget, request: Request,
                            idempotency_key: str = Header(default="")):
        from companion import jargon

        def apply(core, owner_check):
            with core.store.transaction():
                owner_check()
                return jargon.forget(core.store, body.term)

        return await write(request, idempotency_key, body.model_dump(), apply)

    @app.get(prefix + "/watch-ledger", dependencies=auth)
    async def watch_ledger_items():
        from companion import watch_ledger

        core = app.state.core
        return {"enabled": watch_ledger.enabled(core.settings),
                "items": [{key: value for key, value in item.items()
                           if key != "source_event_id"}
                          for item in watch_ledger.recent(core.store, limit=20)]}

    @app.post(prefix + "/watch-ledger/{entry_id}/withdraw", dependencies=auth)
    async def watch_ledger_withdraw(entry_id: str, request: Request,
                                    idempotency_key: str = Header(default="")):
        from companion import watch_ledger

        if not re.fullmatch(r"watch_[A-Za-z0-9_-]{1,120}", entry_id):
            raise CoreError("WATCH_ENTRY_NOT_FOUND", 404)

        def apply(core, owner_check):
            with core.store.transaction():
                owner_check()
                return watch_ledger.withdraw(core.store, entry_id, core.clock())

        return await write(request, idempotency_key, {"entry_id": entry_id}, apply)

    @app.get(prefix + "/life/topics", dependencies=auth)
    async def life_topics():
        core = app.state.core
        return core.life_topics.preview(core.settings.owner_qq_id)

    @app.post(prefix + "/life/topics/{topic_id}/close", dependencies=auth)
    async def close_topic(topic_id: str, body: TopicClosure, request: Request,
                          idempotency_key: str = Header(default="")):
        from companion.life_topics import LifeTopics

        def apply(core, owner_check):
            with core.store.transaction():
                owner_check()
                return LifeTopics(core.store, core.settings, core.clock).close(
                    topic_id, body.source_event_id)

        return await write(request, idempotency_key, body.model_dump(), apply)

    @app.get(prefix + "/interests", dependencies=auth)
    async def interests():
        from companion.interest import profile

        core = app.state.core
        return profile(core.store, core.settings, core.clock(), core.settings.owner_qq_id)

    @app.get(prefix + "/expression-learning", dependencies=auth)
    async def expression_learning():
        core = app.state.core
        return core.expression_learning.snapshot(core.settings.owner_qq_id)

    @app.get(prefix + "/tools", dependencies=auth)
    async def tools():
        core = app.state.core
        version = read_version(core.memory)
        rows = core.store.rows(
            "SELECT " + ",".join(TOOL_FIELDS) + ",scope_json,redacted FROM tool_tasks "
            "WHERE redacted=0 ORDER BY rowid DESC LIMIT 30"
        )
        items = []
        for row in rows:
            item = {key: row[key] for key in TOOL_FIELDS}
            item["scope"] = tool_scope(core, row)
            items.append(item)
        check_version(core.memory, version)
        return {"items": items, "enabled": bool(core.executive.config.enabled)}

    @app.post(prefix + "/tools/{task_id}/decision", dependencies=auth)
    async def tool_decision(task_id: str, body: ToolDecision, request: Request,
                            idempotency_key: str = Header(default="")):
        def apply(core, owner_check):
            return Executive(core.store, core.settings, core.clock).decide(
                task_id, body, owner_check=owner_check)

        return await write(request, idempotency_key, body.model_dump(), apply)

    @app.post(prefix + "/tools/{task_id}/revoke", dependencies=auth)
    async def tool_revoke(task_id: str, request: Request,
                          idempotency_key: str = Header(default="")):
        def apply(core, owner_check):
            return Executive(core.store, core.settings, core.clock).revoke(
                task_id, owner_check=owner_check)

        return await write(request, idempotency_key, {}, apply)

    @app.get(prefix + "/source-grants", dependencies=auth)
    async def source_grants():
        return {"items": app.state.core.observations.grants.list(), "limit": 100}

    @app.post(prefix + "/source-grants/{grant_id}", dependencies=auth)
    async def source_grant_action(grant_id: str, body: SourceGrantAction, request: Request,
                                  idempotency_key: str = Header(default="")):
        return await grant_action(request, idempotency_key, body.model_dump(), grant_id)

