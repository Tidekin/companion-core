import json
import os
import re
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from functools import partial

import psycopg

from companion import (
    budget,
    context_selection,
    conversation_policy,
    dialogue_history,
    image_spool,
    inbox,
    job_queue,
    live_text,
    notices,
    owner_authority,
    owner_delivery,
    owner_input,
    poke,
    private_life,
    qq_images,
    qq_quotes,
    qq_rich,
    reply_quote,
    reply_revision,
    retrieval_work,
    runtime_config,
    style_observation,
    time_context,
    turn_taking,
    watch_ledger,
)
from companion.affect import NEUTRAL, AffectEngine
from companion.commands import command_parts, is_memory_command
from companion.config import Settings
from companion.consolidation import NightlyConsolidator
from companion.content import CONTEXT_RULES, ContentService
from companion.contracts import Candidate, CoreError, MemorySelection
from companion.diary import DiaryService, is_diary_command
from companion.executive import Executive
from companion.exploration import ExplorationService
from companion.expression import ExpressionEngine
from companion.goals import GoalEngine
from companion.growth import ExpressionLearning
from companion.life import LifeService
from companion.media import MediaService
from companion.meme_core import MemeCaptionService
from companion.memory import Memory
from companion.memory_context import MemoryContext
from companion.model import (
    MEMORY_RULES,
    PRINCIPLES,
    RECALL_RULES,
    SOURCE_RULES,
    needs_source_rules,
    principles,
)
from companion.observation import ObservationService
from companion.privacy import epoch, redact
from companion.reading import ReadingService
from companion.reflection import ReflectionService
from companion.reply_segments import split_text
from companion.retrieval import query_scope
from companion.runtime import Runtime
from companion.social import SocialPlanner, proactive_allowed
from companion.store import after, encode, uid, utcnow
from companion.telemetry import emit, splits
from companion.transport import (
    cancel_proactive_work,
    digest,
    enqueue,
    enqueue_reply_parts,
    reconcile_delivery,
    recover_deliveries,
    set_proactive_paused,
)
from companion.voice_flow import VoiceFlow
from companion.wake import WakePlanner
from companion.worker_schedule import claim_scope

MEDIA_CLEANUP_BATCH = 32


@dataclass(frozen=True)
class _ReplyTurn:
    settings: Settings
    job: dict
    authority: owner_authority.OwnerAuthorityView | None
    body: dict
    followup: bool

    @property
    def text(self):
        return self.body.get("text", "")

    def permits(self, purpose):
        return self.authority is None or purpose in self.authority.policy.purposes


class Core(Runtime):
    def __init__(self, settings, model=None, clock=utcnow):
        super().__init__(settings, clock)
        self._media_scans = {}
        self.model = model
        try:
            self.memory = Memory(self.store, settings, self.ledger)
            self.memory_context = MemoryContext(self.memory)
            from companion.mood import MoodService

            self.mood = MoodService(self.memory, clock)
            if settings.memory_enabled:
                self.memory.index.warmup()
            self.diary = DiaryService(self.store, settings, model, self.ledger, clock)
            self.observations = ObservationService(self.store, settings, clock)
            self.media = MediaService(self.store, settings, clock, self.observations)
            # Synthetic image provider transport; see image_generation.available.
            self.image_transport = None
            self.affect = AffectEngine(self.store, settings, clock)
            self.expression_learning = ExpressionLearning(self.store, settings, clock)
            self.reflections = ReflectionService(
                self.store, settings, model, self.diary.repo, self.ledger, clock
            )
            self.goals = GoalEngine(self.store, settings, self.ledger, clock)
            self.consolidation = NightlyConsolidator(
                self.store, settings, self.diary, self.reflections, self.goals,
                self.extract_step, clock,
            )
            self.wake = WakePlanner(self.store, settings, self.goals, self.affect, clock)
            self.life = LifeService(
                self.store, settings, self.goals, self.reflections, self.affect, clock,
            )
            self.social = SocialPlanner(self.store, settings, clock)
            from companion.life_topics import LifeTopics

            self.life_topics = LifeTopics(self.store, settings, clock)
            self.exploration = ExplorationService(
                self.store, settings, self.observations, self.goals, self.social, clock,
            )
            from companion.bili_video import BiliVideoService

            self.bili_video = BiliVideoService(self.store, settings, self.exploration, model, clock)
            self.reading = ReadingService(
                self.store, settings, self.exploration, clock, video=self.bili_video,
            )
            self.content = ContentService(
                self.store, settings, self.observations, self.exploration.create, clock,
                model=lambda: self.model, video=self.bili_video,
            )
            self.expression = ExpressionEngine(self.store, settings, clock)
            self.memes = MemeCaptionService(self.store, settings, lambda: self.model, clock)
            self.voice = VoiceFlow(self.store, settings, clock)
            self.executive = Executive(self.store, settings, clock)
        except BaseException:
            self.close()
            raise

    def ingest(self, event, key):
        return inbox.ingest(
            self.store, self.settings, self.clock, self.ledger, event, key,
            content=self.content, diary=self.diary.repo, affect=self.affect,
        )

    def new_job(self, event_id, kind, key, now, *, max_attempts=None):
        return job_queue.enqueue(self.store, event_id, kind, key, now,
                                 max_attempts=(self.settings.max_attempts if max_attempts is None
                                               else max_attempts))

    def recover(self, now):
        from companion.evaluation import recover as recover_evaluation

        s = self.store
        self.exploration.recover()
        recover_evaluation(s, now)
        from companion.rem import recover_interrupted

        recover_interrupted(s, now)
        # The autonomous selector no longer consumes old fixed-timer greetings.
        # Only unadmitted queued work can be retired; retain all model/send custody.
        for job in s.rows(
            (
                'SELECT j.job_id,j.event_id,e.trace_id FROM jobs j JOIN '
                "events e USING(event_id) WHERE j.kind='activity' AND "
                "j.state IN ('QUEUED','RETRY_WAIT') AND "
                "e.source='system.timer' AND "
                "e.event_type='activity.tick' AND "
                "json_typeof(e.body_json::json#>'{notice}') IS NULL AND "
                'NOT EXISTS (SELECT 1 FROM model_usage m WHERE '
                'm.job_id=j.job_id) AND NOT EXISTS (SELECT 1 FROM '
                'outbox o WHERE o.origin_event_id=j.event_id)'
            ),
        ):
            job_queue.release(s, job["job_id"], "CANCELLED", now, reason="LEGACY_TIMER_RETIRED")
            s.audit("activity.legacy_timer_retired", job["job_id"], job["trace_id"], now)
        expired = s.rows('SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, jobs.kind AS '
            'kind, jobs.state AS state, jobs.idempotency_key AS idempotency_key, jobs.attempts AS '
            'attempts, jobs.max_attempts AS max_attempts, jobs.lease_owner AS lease_owner, '
            'jobs.lease_until AS lease_until, jobs.next_attempt_at AS next_attempt_at, '
            'jobs.last_error_code AS last_error_code, jobs.created_at AS created_at, '
            'jobs.updated_at AS updated_at, jobs.lease_token AS lease_token, jobs.context_tracked '
            'AS context_tracked, jobs.authority_json AS authority_json FROM jobs AS jobs WHERE '
            "jobs.state = 'RUNNING' AND jobs.kind IN ('chat', 'activity') AND jobs.lease_until <= "
            '%s', (now,))
        for job in expired:
            for usage in s.rows(
                "SELECT request_id FROM model_usage WHERE job_id=%s AND state='RESERVED'",
                (job["job_id"],),
            ):
                budget.settle(s, self.settings, usage["request_id"])
            state = "QUEUED" if job["attempts"] < job["max_attempts"] else "FAILED"
            from companion.agent_persistence import recoverable

            if not recoverable(s, job["job_id"]):
                state = "FAILED"
            job_queue.release(s, job["job_id"], state, now, reason="WORKER_LEASE_EXPIRED")
        budget.expire_cancelled_reservations(s, self.settings, now)
        from companion.voice_jobs import expire_voice_jobs
        expire_voice_jobs(s, now)
        recover_deliveries(s, now)

    def cleanup_media(self):
        """Return actual file removals from one bounded scan per spool."""
        from companion import agent_images, image_generation

        removed = 0
        for module, kwargs in (
            (image_spool, {"max_bytes": self.settings.content.max_image_bytes,
                           "retain_completed": agent_images.enabled(self.settings)}),
            (image_generation, {}),
        ):
            root = module.directory(self.store)
            if root.is_symlink():
                raise CoreError("LIVE_IMAGE_ORIGINAL_CHANGED", 403)
            scan = self._media_scans.get(module)
            if scan is None:
                if not root.is_dir():
                    continue
                scan = self._media_scans[module] = os.scandir(root)
            try:
                scanned, unlinked = module.cleanup(
                    self.store, self.clock(), entries=scan, limit=MEDIA_CLEANUP_BATCH, **kwargs,
                )
            except BaseException:
                scan.close()
                del self._media_scans[module]
                raise
            removed += unlinked
            if scanned < MEDIA_CLEANUP_BATCH:
                scan.close()
                del self._media_scans[module]
        return removed

    def close(self):
        for scan in getattr(self, "_media_scans", {}).values():
            scan.close()
        if hasattr(self, "_media_scans"):
            self._media_scans.clear()
        super().close()

    def claim_job(self, worker_id, *, job_id=None):
        s, now = self.store, self.clock()
        # Recovery commits independently. Empty polls use a snapshot; an eligible
        # candidate repeats its fences only under the native writer/row lock.
        with s.transaction():
            self.recover(now)
        return s.read_or_write(lambda: self._claim_job(worker_id, job_id, now))

    def _claim_job(self, worker_id, job_id, now):
        s = self.store
        if (owner_authority.required(s, self.settings)
            or private_life.enabled(self.settings)):
            foreground_only = bool(owner_authority.required(s, self.settings)
                                   and self.settings.paid_model
                                   and self.settings.paid_model.concurrency > 1)
            if private_life.busy(s, now, foreground_only=foreground_only) or s.one(
                "SELECT 1 FROM outbox WHERE state = 'SENDING' LIMIT 1",
            ):
                # Input admission and tail cancellation proceed independently;
                # chat waits for its existing model/send responsibility. Separate
                # background work shares admission capacity when more slots exist.
                return None
        if not proactive_allowed(s, self.settings, now):
            cancel_proactive_work(s, now)
        row = s.one(
            'SELECT j.job_id AS job_id, j.event_id AS event_id, j.kind AS kind, j.state AS state, '
            'j.idempotency_key AS idempotency_key, j.attempts AS attempts, j.max_attempts AS '
            'max_attempts, j.lease_owner AS lease_owner, j.lease_until AS lease_until, '
            'j.next_attempt_at AS next_attempt_at, j.last_error_code AS last_error_code, '
            'j.created_at AS created_at, j.updated_at AS updated_at, j.lease_token AS lease_token, '
            'j.context_tracked AS context_tracked, j.authority_json AS authority_json, '
            'e.conversation_id AS conversation_id, e.body_json AS body_json, e.trace_id AS '
            'trace_id, e.redacted AS redacted, e.reply_revision AS reply_revision FROM jobs AS j '
            "JOIN events AS e ON e.event_id = j.event_id WHERE j.state IN ('QUEUED', 'RETRY_WAIT') "
            "AND j.kind IN ('chat', 'activity', 'evaluation', 'rem') AND ((CAST(%s AS TEXT) IS "
            "NULL AND j.kind IN ('chat', 'evaluation')) OR j.job_id = %s) AND (j.next_attempt_at "
            'IS NULL OR j.next_attempt_at <= %s) AND e.redacted = 0 AND NOT EXISTS(SELECT 1 AS "1" '
            'FROM jobs AS b JOIN events AS be ON be.event_id = b.event_id WHERE be.conversation_id '
            "= e.conversation_id AND b.record_sequence < j.record_sequence AND (b.kind = 'chat' OR "
            "(b.kind = 'activity' AND (j.kind <> 'chat' OR b.state = 'RUNNING'))) AND b.state IN "
            "('QUEUED', 'RUNNING', 'RETRY_WAIT')) ORDER BY CASE WHEN j.kind = 'evaluation' THEN 1 "
            'ELSE 0 END NULLS FIRST, j.record_sequence NULLS FIRST LIMIT 1'
            + (' FOR UPDATE OF j SKIP LOCKED' if s.in_write_transaction else ''),
            (job_id, job_id, now),
        )
        if not row:
            return None
        if not s.in_write_transaction:
            raise RuntimeError("POSTGRES_CLAIM_REQUIRES_WRITE_TRANSACTION")
        if row["kind"] in {"activity", "rem"} and (
            budget.chat_pending(s, now) or private_life.enabled(self.settings)
            and private_life.owner_waiting(s, self.settings, now)
        ):
            return None
        if owner_authority.required(s, self.settings):
            try:
                owner_authority.check_job(s, self.settings, self.clock, row,
                    purpose=owner_authority.model_purpose(s, row["kind"], row["job_id"]))
            except CoreError as error:
                job_queue.release(s, row["job_id"], "CANCELLED", now, reason=error.code)
                return None
        if self.memes.waiting_offer(row, now):
            return None
        body = json.loads(row["body_json"])
        if (self.settings.voice.enabled and row["kind"] == "chat"
            and self.voice.input_status(row["event_id"], row["job_id"]).state == "WAITING"):
            return None
        from companion.transport import native_text

        if (
            self.settings.content_enabled
            and row["kind"] == "chat"
            and not native_text(s, row["event_id"])
            and not body.get("diary_command")
            and not body.get("control_command")
        ):
            from companion import agent_images

            if row.get("authority_json") and agent_images.waiting(self, row):
                return None
            if qq_images.enabled(self.settings):
                inflight = s.one(
                    'SELECT m.request_id FROM model_usage AS m JOIN model_admissions AS a USING '
                    "(request_id) WHERE m.job_id = %s AND m.scenario = 'content_vision' AND "
                    "m.state = 'RESERVED' AND a.window_id = %s AND a.lease_until > %s",
                    (row["job_id"], self.settings.live_text_window.window_id, self.clock()),
                )
                if inflight:
                    # The API owns this image request. Validate its existing fences,
                    # then wait without claiming the chat job or authorizing more I/O.
                    live_text.check_model(s, self.settings, self.clock, inflight["request_id"])
                    return None
            if self.content.status(row["event_id"])["pending"]:
                return None
        token = uid("lease")
        try:
            runtime_config.bind(s, self.settings, self.clock, "job:" + row["job_id"])
        except CoreError as error:
            job_queue.finish_unclaimed(s, row["job_id"], "FAILED", now, reason=error.code)
            return None
        job_queue.claim(s, row["job_id"], worker_id, token, after(now, self.settings.lease_seconds),
                        now)
        return {**row, "lease_token": token, "attempts": row["attempts"] + 1}

    def _check_context_snapshots(self, job, snapshots, *, current_request=None, owner_view=None,
                                 read=None, reads=None):
        if not self.store.in_transaction:
            return self.store.read_or_write(lambda: self._check_context_snapshots(
                job, snapshots, current_request=current_request, owner_view=owner_view))
        if reads is None:
            reply_revision.check_job(self.store, job)
        else:
            reply_revision._checked_job(job, reads.reply)
        authority = owner_view if owner_view is not None else self._owner_job_view(job)
        task_key = "job:" + job["job_id"]
        configuration = (runtime_config.check(self.store, self.settings, task_key)
                         if reads is None else runtime_config._checked_task(
                             self.store, self.settings, task_key, reads.configuration))
        task_settings = runtime_config.effective(self.settings, configuration)
        markers = [item for item in snapshots if item["kind"] == "runtime_configuration"]
        if markers and (configuration is None or markers != [{"kind": "runtime_configuration",
                "snapshot_id": runtime_config.identity(self.settings, configuration)}]):
            raise CoreError("CONFIGURATION_CONTEXT_CHANGED", 409)
        self.memes.check_offer(job)
        if private_life.strict(self.store, self.settings):
            live_text.check_job(self.store, self.settings, self.clock, job,
                                current_request=current_request)
        memory_sources = None
        memory_prepared = False
        for snapshot in snapshots:
            try:
                purpose = {"memory": "memory", "character": "expression",
                           "growth": "expression", "affect": "affect",
                           "mood": "expression", "watch": "exploration", "interest": "memory",
                           "voice": "voice", "diary": "diary", "images": "image",
                           "style_observation": "expression"}.get(
                               snapshot["kind"], snapshot.get("purpose"))
                if authority and purpose and purpose not in authority.policy.purposes:
                    raise CoreError("OWNER_AUTHORITY_PURPOSE_REQUIRED", 403)
                if snapshot["kind"] == "memory":
                    now = self.clock()
                    if self.store.in_read_snapshot:
                        if read is not None and not memory_prepared:
                            memory_sources = read.proof_sources(self, snapshots, now)
                            memory_prepared = True
                        elif read is None and memory_sources is None:
                            from companion.memory_context import MemorySources

                            memory_sources = MemorySources(self.memory, now, bounded=True)
                            memory_sources._snapshot_cache()
                        if memory_sources is not None:
                            now = memory_sources.now
                    else:
                        memory_sources = None
                    try:
                        self.memory_context.revalidate(snapshot["proof"], now,
                                                       source_view=memory_sources)
                    except retrieval_work.WorkLimit:
                        if memory_sources is None:
                            raise
                        # Required proofs retain their individual work bounds when
                        # their combined sources exceed this snapshot's shared view.
                        memory_sources = None
                        self.memory_context.revalidate(snapshot["proof"], now)
                elif snapshot["kind"] == "character":
                    from companion.history_import import revalidate_character

                    revalidate_character(self, job, snapshot["proof"])
                elif snapshot["kind"] == "growth":
                    self.expression_learning.revalidate_reply(
                        self.settings, job, self._affect_context(authority), snapshot["proof"])
                elif snapshot["kind"] == "style_observation":
                    style_observation.revalidate_reply(self.store, self.settings, self.clock,
                                                       job, snapshot["proof"])
                elif snapshot["kind"] == "affect":
                    self.affect.revalidate(snapshot["proof"])
                elif snapshot["kind"] == "mood":
                    self.mood.revalidate(snapshot["proof"])
                elif snapshot["kind"] == "watch":
                    watch_ledger.revalidate_reply(self.store, self.memory_context, self.clock,
                                                  snapshot["proof"])
                elif snapshot["kind"] == "interest":
                    from companion.interest import revalidate_reply

                    revalidate_reply(self.store, self.settings, snapshot["proof"])
                elif snapshot["kind"] == "conversation_policy":
                    conversation_policy.revalidate(snapshot["proof"],
                        task_settings.conversation_policy,
                        current_text=json.loads(job["body_json"]).get("text", ""),
                        affect=self._affect_context(authority))
                elif snapshot["kind"] == "voice":
                    self.voice.check_context(job, snapshot["proof"])
                elif snapshot["kind"] == "singing":
                    from companion.agent_singing import revalidate

                    revalidate(self.store, self.settings, self.clock, job, [snapshot])
                elif snapshot["kind"] == "account_actions":
                    from companion import account_action_context

                    account_action_context.revalidate(self, job, snapshot["proof"])
                elif snapshot["kind"] == "diary":
                    self.diary.revalidate_context(snapshot["proof"])
                elif snapshot["kind"] == "sources":
                    self.goals.validate_sources(snapshot["event_ids"])
                elif snapshot["kind"] == "history":
                    owner_input.check_history(self.store, self.settings, self.clock,
                                              job, snapshot["proof"], read=read)
                elif snapshot["kind"] == "context_plan":
                    from companion.context_planner import check, check_bound

                    if reads is None:
                        check(self, job, snapshots)
                    else:
                        check_bound(self.store, job, snapshots, reads.privacy_epoch)
                elif snapshot["kind"] == "images":
                    from companion import agent_images

                    ids = agent_images.context_item_ids(self.store, self.settings, self.clock, job,
                        _item_ids=reads.image_item_ids if reads is not None else None)
                    if ids != snapshot["item_ids"]:
                        raise CoreError("PRIVACY_CHANGED", 409)
            except CoreError as error:
                if error.code == "PRIVACY_CHANGED":
                    # A now-invalid source cancels this response, not the companion window.
                    raise CoreError(reply_revision.CANCELLED, 409) from error
                raise
        return task_settings

    def _owner_job_view(self, job):
        if not owner_authority.required(self.store, self.settings):
            return None
        return owner_authority.check_job(self.store, self.settings, self.clock, job,
            purpose=owner_authority.model_purpose(self.store, job["kind"], job["job_id"]),
            executing=True)

    def _affect_context(self, authority):
        if authority and "affect" not in authority.policy.purposes:
            return {"state": dict(NEUTRAL), "confidence": 0.0, "evidence": [], "expires_at": None}
        return self.affect.snapshot()

    def _notice_context(self, cfg, job, data, authority, snapshots):
        """Word one notice with shared expression and a small authorized dialogue window."""
        from companion.token_measurement import measure_request

        public = "QZONE_DRAFT" in data.get("allowed_destinations", ())
        rules = PRINCIPLES
        if not public and cfg.conversation_policy.mode == "ENABLED":
            rules = conversation_policy.apply_strategy(
                [{"role": "system", "content": rules}], cfg.conversation_policy.strategy,
                guidance=cfg.conversation_guidance)[0]["content"]
        contacts = []
        if not public and (not authority and cfg.mode != "live"
                           or authority and "history" in authority.policy.purposes):
            contacts = notices.prior_contacts(self.store, job, data, self.clock(),
                                              cfg.raw_event_retention_days)
        messages = notices.messages(self.store, cfg, job, self.clock(), rules,
                                    data=data, prior_contacts=contacts)
        retained = []
        sources = set(data["source_events"]) | {item["event_id"] for item in contacts}
        if not public and (not authority or "expression" in authority.policy.purposes):
            style = style_observation.reply_reference(
                self.store, self.settings, self.clock, job, "", material=data)
            if style:
                styled = [{"role": "system", "content": messages[0]["content"] + style["text"]},
                          *messages[1:]]
                tokens = measure_request(cfg, styled, "activity").reserved_input_tokens
                if tokens <= cfg.context_soft_tokens:
                    messages = styled
                    sources.update(style["source_event_ids"])
                    retained.append({"kind": "style_observation", "proof": style["proof"]})

        history = []
        if not public and authority and "history" in authority.policy.purposes:
            purpose = owner_authority.model_purpose(self.store, job["kind"], job["job_id"])
            history = owner_input.history_sources(self.store, cfg, self.clock, job, purpose=purpose)
            if data["kind"] == "life" and (
                authority.policy.life_shadow.source_scope == "selected"
                or data["read_goals_enabled"]
                or set(data["allowed_destinations"]) - {"PRIVATE_MESSAGE_DRAFT"}
            ):
                # A goal or saved expression must remain traceable to its approved roots.
                history = [turn for turn in history if turn["event_id"] in data["source_events"]]
        elif not public and not authority and cfg.mode != "live":
            history = dialogue_history.recent_sent_sources(
                self.store, job["conversation_id"], job["event_id"], cfg.max_recent_messages // 2)
        # The notice contract stays last. History is optional and yields its space
        # before the required reading/decision material.
        clip = turn_taking.excerpt
        focus = context_selection.terms(notices.subject(data))
        ranked = sorted(reversed(history), key=lambda item: context_selection.relevance(
            focus, item["user_text"] + (item["reply_text"] or "")), reverse=True)[:2]
        selected = [item for item in history if item in ranked]
        while selected:
            if authority and data["kind"] == "life":
                scope = authority.policy.history
                owner_sources = [x for x in data["sources"] if x["kind"] == "owner_message"]
                ids = {item["event_id"] for item in selected}
                size = sum(len(x["text"].encode()) for x in owner_sources)
                size += sum(len(clip(item["user_text"]).encode())
                            + len(clip(item["reply_text"] or "").encode()) for item in selected)
                if (len(ids | {x["event_id"] for x in owner_sources}) > scope.max_turns
                    or len(ids | set(data["source_events"])) > cfg.life_loop.source_limit
                    or size > scope.max_utf8_bytes):
                    selected.remove(ranked.pop())
                    continue
            clipped = any(clip(item["user_text"]) != item["user_text"]
                          or clip(item["reply_text"] or "") != (item["reply_text"] or "")
                          for item in selected)
            prior = {"role": "user", "content": "近期私聊片段（历史参考）：\n" + encode([
                {"主人": clip(item["user_text"]), "你": clip(item["reply_text"] or "")}
                for item in selected]) + ("\n“…”处为省略。" if clipped else "")}
            candidate = [messages[0], prior, *messages[1:]]
            tokens = measure_request(cfg, candidate, "activity").reserved_input_tokens
            if tokens <= cfg.context_soft_tokens:
                messages = candidate
                sources.update(item["event_id"] for item in selected)
                if authority:
                    retained.append({"kind": "history",
                                     "proof": owner_input.history_proof(selected)})
                break
            selected.remove(ranked.pop())
        reply_revision.remember_sources(self.store, job, sources)
        self._check_context_snapshots(job, retained)
        if snapshots is not None:
            snapshots.extend(retained)
        return messages

    def _reply_rules(self, turn, model_tools):
        s, cfg, job = self.store, turn.settings, turn.job
        body, text, authority, followup = turn.body, turn.text, turn.authority, turn.followup
        expression_rules, expression_data = self.expression.instruction_parts(job["event_id"]) if (
            turn.permits("expression")) else ("", "")
        policy = (cfg.conversation_policy if job["kind"] == "chat" or followup
                  else conversation_policy.ConversationPolicyConfig(mode="OFF"))
        rules = principles(character_expression=(
            policy.mode == "ENABLED" and policy.strategy == "character_v1")) + (
            MEMORY_RULES if cfg.memory_enabled and turn.permits("memory") else ""
        ) + expression_rules
        if turn.permits("expression") and not private_life.strict(s, cfg):
            from companion.history_import import CHARACTER_RULES, active_character

            if active_character(s):
                rules += CHARACTER_RULES
        if followup:
            rules += "\n" + turn_taking.FOLLOW_UP_RULES
        affect = self._affect_context(authority)
        rules, policy_data, conversation_decision = conversation_policy.prompt_parts(
            rules, policy,
            current_text=text, affect=affect,
            guidance=cfg.conversation_guidance if job["kind"] == "chat" or followup else None,
        )
        dynamic_rules = [expression_data, policy_data]
        if cfg.profile.enabled and job["kind"] == "chat" and turn.permits("profile"):
            from companion import companion_profile

            dynamic_rules.append(companion_profile.prompt_fact(s, cfg))
        if job["kind"] == "chat" and not cfg.chat_time_labels:
            # Stored event times keep a rebuilt context identical to the measured one.
            current_event = s.one((
                'SELECT record_sequence AS rowid, occurred_at FROM '
                'events WHERE event_id = %s'
            ),
                                  (job["event_id"],))
            previous = s.one(
                "SELECT occurred_at FROM events WHERE conversation_id = %s AND actor_id = 'owner' "
                'AND record_sequence < %s AND redacted = 0 ORDER BY record_sequence DESC NULLS '
                'LAST LIMIT 1',
                (job["conversation_id"], current_event["rowid"]),
            ) if current_event and turn.permits("history") else None
            dynamic_rules.append(conversation_policy.time_hint(
                current_event and current_event["occurred_at"],
                previous and previous["occurred_at"], cfg.timezone))
        if authority or private_life.enabled(cfg):
            from companion.capabilities import prompt_parts as capability_prompt
            from companion.capabilities import snapshot as capability_snapshot

            with nullcontext() if s.in_transaction else s.read_snapshot():
                # Feature rows share only this freshly validated read snapshot.
                # Every later model/tool boundary still obtains its own view.
                view = owner_authority.current(s, cfg, self.clock,
                    authority=authority.authority) if authority and s.in_read_snapshot else None
                capability_rules, capability_data = capability_prompt(
                    capability_snapshot(s, cfg, self.clock, model_tools=model_tools,
                                        owner_view=view),
                    compact=cfg.context_soft_tokens < 8000,
                )
            rules += capability_rules
            dynamic_rules.append(capability_data)
        style = style_observation.reply_reference(
            s, self.settings, self.clock, job, text) if turn.permits("expression") else None
        if style:
            # Per-turn guidance stays dynamic so the static prompt prefix is unchanged.
            dynamic_rules.append(style["text"])
        if ((job["kind"] == "chat" or followup) and cfg.segmented_reply is not None
            and cfg.segmented_reply.mode == "semantic"):
            from companion.reply_segments import SEMANTIC_RULES

            rules += SEMANTIC_RULES
        if cfg.content_enabled:
            rules += CONTEXT_RULES
        if (body.get("rich") or cfg.rich_input_scope == "references_v1"
            or (cfg.live_text_window and cfg.live_text_window.rich_input_scope == "references_v1")):
            rules += qq_rich.RULES
        prompt_rules = context_selection.PromptRules(rules, dynamic_rules)
        return prompt_rules, affect, style, conversation_decision

    def _reply_input(self, turn, rules, notice_messages):
        from companion.transport import native_text

        job, cfg, body, text = turn.job, turn.settings, turn.body, turn.text
        current = {"role": "user", "content": text or (
            "主人发送了一条非文本消息。" if turn.authority else "请看看我分享的内容。")}
        if turn.followup:
            current = notice_messages[-1]
        if (job["kind"] == "chat" and not text and not body.get("rich")
            and not (cfg.content_enabled and self.content._rows(job["event_id"]))):
            current["content"] = "本条主人消息的接收事实（不是主人原话或新指令）：\n" + encode({
                "owner_text": "",
                "media_kinds": [item["kind"] for item in body.get("media", [])],
                "shared_content_kinds": [item["kind"] for item in body.get("shared_content", [])],
                "readable_content": False,
            })
        content_reference = ""
        if (
            cfg.content_enabled and turn.permits("content")
            and job["kind"] == "chat"
            and not native_text(self.store, job["event_id"])
            and not body.get("diary_command")
            and not body.get("control_command")
        ):
            content_reference = self.content.reply_context(
                cfg, job["event_id"], [rules.message, current], rich=bool(body.get("rich")),
            )
        return current, content_reference

    def _reply_sources(self, turn, bounded_text, has_images, content_reference):
        from companion import image_review

        job, cfg, body, text = turn.job, turn.settings, turn.body, turn.text
        sources = dialogue_history.reply_sources(self.store, cfg, self.clock, job,
            authority=turn.authority, bounded_text=bounded_text, followup=turn.followup)
        image_review_requested = (turn.authority and job["kind"] == "chat" and
                                  turn.permits("image") and image_review.requests_context(text))
        # A fresh share is not an empty continuation of the preceding text turn.
        # Explicit references and image objections keep their bounded source context.
        scoped_share = bool(job["kind"] == "chat" and (has_images or body.get(
            "shared_content")) and not image_review_requested
            and not context_selection.share_continuation(text) and not any(
                item.get("type") == "reply" for item in (body.get("rich") or {}).get(
                    "segments", [])))
        focus = context_selection.focus(text, sources, shared=scoped_share)
        if scoped_share:
            content_reference, content_focus = context_selection.current_share(
                content_reference, job["event_id"])
            focus |= content_focus
            sources = context_selection.share_history(sources, focus)

        return sources, scoped_share, content_reference, partial(context_selection.relevance, focus)

    def _reply_references(self, turn, selection, related, available, bounded_text, affect,
                         memories, memory_paused, mark):
        s, cfg, job, text = self.store, turn.settings, turn.job, turn.text
        authority = turn.authority
        if turn.permits("expression") and not bounded_text:
            from companion.history_import import character_context

            # An explicitly configured character is the starting point of every turn.
            if character := character_context(self, job):
                selection.include(character["message"], character["source_event_ids"],
                                  proof={"kind": "character", "proof": character["proof"]})
                selection.required.add(id(character["message"]))
        if turn.permits("goals"):
            self.life.add_reply_references(selection, related)
        if cfg.watch_ledger.enabled and job["kind"] == "chat" and turn.permits("exploration"):
            watch_ledger.add_reply_references(s, self.memory_context, self.clock, job, selection,
                                             related, available)
        if (cfg.mood_enabled and job["kind"] == "chat" and turn.permits("expression")
            and affect["state"].get("engagement") != "pause"):
            mood = self.mood.reference(at=job["created_at"], text=text)
            if mood:
                selection.add("你的心情参考（数据）：",
                    {key: mood[key] for key in ("rules", "state", "confidence", "evaluated_at")},
                    mood["proof"]["source_event_ids"], priority=context_selection.MOOD,
                    proofs=[{"kind": "mood", "proof": mood["proof"]}])
        if affect["evidence"]:
            selection.include(
                {"role": "user", "content":
                 "当前交互参考（仅影响表达，不改变人格、事实、权限或承诺）：\n" + encode(affect)},
                {row["event_id"] for row in affect["evidence"]},
                proof={"kind": "affect", "proof": affect})
        learned = self.expression_learning.reply_reference(self.settings, job, affect) if (
            turn.permits("expression")) else None
        mark("context_references")
        # A newer owner message cancels this job at ingestion; stop building for it.
        reply_revision.check_job(s, job)
        if learned:
            selection.include(learned["message"], learned["source_event_ids"],
                              proof={"kind": "growth", "proof": learned["snapshot"]})
        memory_references = []
        used_ids = []
        if cfg.memory_enabled and turn.permits("memory"):
            allowed = live_text.memory_ids(s, cfg, self.clock, job) if bounded_text else None
            facts = self.memory_context.reply_facts(
                cfg, self.clock, text, memories, allowed_ids=allowed,
                include_standing=job["kind"] == "chat" and not bounded_text,
            )
            hits, _, standing, _ = facts
            used_ids = [row["memory_id"] for row in [*standing, *hits]]
            from companion import interest

            interest.add_reply_references(
                s, cfg, self.clock, self.memory_context, selection, bounded_text=bounded_text,
                views=cfg.watch_ledger.enabled and turn.permits("exploration"), related=related,
                available=available,
            )
            memory_references = self.memory_context.add_reply_facts(
                selection, facts, cfg, self.clock, chat=job["kind"] == "chat", paused=memory_paused,
                query=text, allowed_ids=allowed,
            )
        # A bounded window admits only its approved facts, never learned jargon.
        if (job["kind"] == "chat" and cfg.jargon.enabled and turn.permits("memory")
                and not bounded_text):
            from companion import jargon

            terms, term_sources = jargon.context(s, text, cfg.jargon.max_context_terms)
            if terms:
                selection.include({
                    "role": "user",
                    "content": "主人用语参考（根据主人以前的用法推测，不一定准确；只用来理解"
                    "主人的意思，不要模仿、不要解释给主人听，不确定就按字面理解）：\n"
                    + encode(terms),
                }, term_sources)
        # Third-party glossary data: no owner evidence, so no sources or memory purpose.
        if job["kind"] == "chat" and cfg.meme_wiki.enabled and not bounded_text:
            from companion import meme_wiki

            if glossary := meme_wiki.context(cfg, text):
                # It explains the current message: older history is pruned before it.
                selection.messages.append(glossary)
                selection.priorities[id(glossary)] = context_selection.TOPIC
        if (authority and job["kind"] == "chat"
                and {"history", "social", "account_actions"} <= set(authority.policy.purposes)):
            from companion import account_action_context

            if actions := account_action_context.reference(self, job):
                priority = related("昵称 个性签名 头像 改名")
                selection.add(account_action_context.HEADING, actions["data"],
                    actions["source_event_ids"], priority=priority,
                    required=priority == context_selection.RELATED, module="account_actions",
                    proofs=[{"kind": "account_actions", "proof": actions["proof"]}])
        mark("context_memory")
        return memory_references, used_ids

    def _reply_history(self, turn, selection, sources, *, scoped_share, bounded_text,
                       available, turn_ranking, used_memory_ids, mark):
        from companion import image_review, turn_recall

        s, cfg, job, text = self.store, turn.settings, turn.job, turn.text
        authority = turn.authority
        turns = [dialogue_history.turn_messages(source, s, text) for source in sources]
        recalled = recalled_reference = None
        recalled_history = []
        original_order = {source["event_id"]: i for i, source in enumerate(sources)}
        if (job["kind"] == "chat" and authority and not bounded_text and text and not scoped_share
                and cfg.memory_enabled and turn.permits("memory")
                and "history" in authority.policy.purposes):
            recalled, recalled_reference = turn_recall.add_reply_reference(
                s, cfg, self.clock, job, text, sources, turns, authority.policy.history,
                available=available, ranked_ids=turn_ranking, memory_context=self.memory_context,
                selection=selection)
            if recalled:
                recalled_ids = set(recalled["event_ids"])
                recalled_history = [(source, group)
                    for source, group in zip(sources, turns, strict=True)
                    if source["event_id"] in recalled_ids]
                remaining = [(source, group) for source, group in zip(sources, turns, strict=True)
                             if source["event_id"] not in recalled_ids]
                sources, turns = ([source for source, _ in remaining],
                                  [group for _, group in remaining])
        mark("context_turn_recall")
        continuation = None
        if not scoped_share:
            now = self.clock()
            continuation = self.memory_context.reply_continuation(
                self.settings, self.clock, now, self._owner_job_view(job), job, sources,
                used_memory_ids,
                reserved_ids=set(recalled["event_ids"]) if recalled else frozenset(),
                reserved_bytes=recalled["cost_bytes"] if recalled else 0)
        if continuation:
            sources = continuation["history"]
            turns = [dialogue_history.turn_messages(source, s, text) for source in sources]
            selection.include(continuation["message"], continuation["source_event_ids"],
                              proof={"kind": "memory", "proof": continuation["proof"]})
        mark("context_history")
        image_review_target, image_review_proofs = None, []
        if authority and job["kind"] == "chat" and turn.permits("image"):
            # Recalled turns left the ordinary window but stay in the authorized
            # source set: an explicit objection may still target their images.
            window = sorted([*zip(sources, turns, strict=True), *recalled_history],
                            key=lambda pair: original_order.get(pair[0]["event_id"], -1))
            window_sources = [source for source, _ in window]
            image_review_target = image_review.target(self, job, turn.body, window_sources)
            if turn.permits("memory"):
                image_review_proofs = image_review.annotate(
                    self, job, window_sources, [group for _, group in window])
        return dialogue_history.ReplyHistory(sources, turns, continuation, image_review_target,
            image_review_proofs, recalled=recalled, recalled_reference=recalled_reference,
            recalled_history=recalled_history, original_order=original_order)

    def _format_reply_turns(self, turn, rules, current, history, content_reference, selection,
                           notice_messages, has_images):
        s, cfg, job, body = self.store, turn.settings, turn.job, turn.body
        followup, authority = turn.followup, turn.authority
        turns, sources = history.turns, history.sources
        reply_revision.check_job(s, job)
        if job["kind"] == "chat" and any(source.get("reply_complete") is False
                                         and not source.get("reply_text") for source in sources):
            rules.append("\n历史里主人连着发了几条、中间没有你的回复时，前面那几条你还没回上；"
                      "这次一起接住，别漏掉主人问你的话。")
        if any(len(turn) > 2 for turn in turns) and qq_rich.RULES not in rules.message["content"]:
            rules.append(qq_rich.RULES)
        quoting = bool(authority) and job["kind"] == "chat" and cfg.quote_policy != "never"
        if quoting:
            turns = [reply_quote.label_turn(turn, source)
                     for turn, source in zip(turns, sources, strict=True)]
            rules.append(reply_quote.RULES)
        current_input = []
        if (job["kind"] == "chat" and cfg.chat_time_labels) or followup:
            turns = [time_context.stamp_turn(turn, source, cfg.timezone)
                     for turn, source in zip(turns, sources, strict=True)]
            if followup:
                current_input.extend(notice_messages[1:-1])
            else:
                # The owner's stored time keeps a rebuilt chat context identical.
                received = s.one('SELECT occurred_at FROM events WHERE event_id = %s',
                                 (job["event_id"],))
                current_input.append(time_context.now_message(
                    received["occurred_at"] if received else self.clock(), cfg.timezone))
        if content_reference:
            current_input.append({"role": "user", "content": content_reference})
        if cfg.voice.enabled and turn.permits("voice") and job["kind"] == "chat":
            voice_reference = self.voice.for_context(job["event_id"], job)
            if voice_reference is not None:
                from companion.voice_flow import CONTEXT_RULES as VOICE_CONTEXT_RULES

                rules.append("\n" + VOICE_CONTEXT_RULES)
                current_input.append(voice_reference["message"])
                selection.sources.append((voice_reference["message"],
                                          voice_reference["source_event_ids"]))
                selection.proofs.append((voice_reference["message"],
                                         {"kind": "voice", "proof": voice_reference["proof"]}))
        withdrawn = qq_quotes.withdrawn(s, job["event_id"], body)
        if rich := reply_quote.rich_reference(s, self.settings, job, body, withdrawn):
            current_input.append(rich)
        if has_images:
            current["content"] = [
                {"type": "text", "text": current["content"]},
                *qq_images.original_parts(s, cfg, self.clock, job,
                                          skip_slots=qq_quotes.media_slots(body, withdrawn)),
            ]
        if quoting:
            current = reply_quote.label(current, job["event_id"])
        current_input.append(current)
        history.turns = turns
        return current_input

    def _freeze_reply_context(self, turn, fitted, selection, history, memory_references, *,
                              bounded_text, content_reference, style, decision):
        messages = fitted.messages
        visible = {id(message) for message in messages}
        used = [row for ref, row in memory_references if id(ref) in visible]
        continuation = history.continuation
        if continuation and continuation["uses_recalled_memory"] and not used:
            messages[:] = [m for m in messages if m is not continuation["message"]]
        retained_history = history.retained(messages)
        if bounded_text:
            live_text.freeze_context(self.store, turn.settings, self.clock, turn.job,
                                     retained_history, messages, used)
        review_sources, review_proofs = history.include_review_sources(retained_history, messages)
        actual_sources, retained = selection.retained(messages, review_proofs)
        actual_sources.update(source["event_id"] for source in retained_history)
        actual_sources.update(review_sources)
        if content_reference:
            actual_sources.update(self.content.context_source_ids(content_reference))
        if style:
            actual_sources.update(style["source_event_ids"])
        reply_revision.remember_sources(self.store, turn.job, actual_sources)
        if style:
            retained.append({"kind": "style_observation", "proof": style["proof"]})
        if turn.authority and (turn.job["kind"] == "chat" or turn.followup):
            retained.append({"kind": "history",
                             "proof": owner_input.history_proof(retained_history)})
        if decision.mode == "ENABLED":
            retained.append({"kind": "conversation_policy", "proof": decision.receipt()})
        self._check_context_snapshots(turn.job, retained)
        return retained

    def context(self, job, memories=None, memory_paused=False, *, snapshots=None, model_tools=(),
                planning=False, turn_ranking=None):
        s = self.store
        cfg = runtime_config.for_context(s, self.settings, job, snapshots)
        mark = splits(job.get("trace_id"))
        reply_revision.check_job(s, job)
        authority = self._owner_job_view(job)
        planning = (planning and authority is not None and job["kind"] == "chat"
                    and snapshots is not None)
        if job["kind"] == "chat":
            reply_revision.require_reply_source(s, job["event_id"], job.get("reply_revision"))
        followup = (notices.reference(job) or {}).get("kind") == "followup"
        notice_messages = []
        if (material := notices.validate(s, cfg, job, self.clock())) is not None:
            if not followup:
                return self._notice_context(cfg, job, material, authority, snapshots)
            notice_messages = notices.messages(s, cfg, job, self.clock(), PRINCIPLES, data=material)
            reply_revision.remember_sources(s, job, material["source_events"])
        turn = _ReplyTurn(cfg, job, authority, json.loads(job["body_json"]), followup)
        rules, affect, style, decision = self._reply_rules(turn, model_tools)
        from companion.transport import native_text

        if native_text(s, job["event_id"]):
            rules.append("\n当前聊天设备只支持文字，请用文字回复；"
                         "需要说明图片、语音或歌曲时直接描述。", dynamic=True)
        mark("context_rules")
        selection = context_selection.References()
        bounded_text = private_life.strict(s, cfg)
        if turn_taking.offered(s, cfg, job):
            rules.append(turn_taking.SILENCE_RULES, dynamic=True)
        has_images = qq_images.enabled(cfg) and bool(qq_images.manifest(turn.body))
        combined = (bounded_text and cfg.live_text_window.history_scope == "approved_window"
                    and cfg.live_text_window.memory_scope == "approved_facts")
        image_memory = has_images and cfg.live_text_window.memory_scope == "approved_facts"
        if combined:
            rules.append("\n近期对话与长期事实不一致时，说明来源差异和不确定并请主人澄清；"
                                 "不得按相似度、重复次数或新旧顺序投票决定真假。")
        current, content_reference = self._reply_input(turn, rules, notice_messages)
        sources, scoped_share, content_reference, related = self._reply_sources(
            turn, bounded_text, has_images, content_reference)
        available = self.memory_context.reply_source_check(cfg, self.clock, authority)
        memory_refs, used_ids = self._reply_references(turn, selection, related, available,
            bounded_text, affect, memories, memory_paused, mark)
        history = self._reply_history(turn, selection, sources, scoped_share=scoped_share,
            bounded_text=bounded_text, available=available, turn_ranking=turn_ranking,
            used_memory_ids=used_ids, mark=mark)
        current_input = self._format_reply_turns(turn, rules, current, history, content_reference,
                                                selection, notice_messages, has_images)
        mark("context_current")
        if (work := retrieval_work.current()) is not None and work.receipt()["coverage_limited"]:
            rules.append("\n" + retrieval_work.NOTE, dynamic=True)
        if (turn.permits("memory") or turn.permits("history")) and needs_source_rules(
                json.dumps([selection.messages, current_input], ensure_ascii=False, default=str)):
            rules.append(SOURCE_RULES, dynamic=True)
        protected = combined or image_memory or (
            bounded_text and cfg.live_text_window.memory_scope == "approved_facts" and memory_refs)
        fitted = context_selection.fit_reply(cfg, job["kind"], rules.message, selection, history,
            current_input, related, protected=protected, query=turn.text)
        if authority and job["kind"] == "chat" and turn.permits("image"):
            from companion import agent_images, image_review

            if agent_images.enabled(cfg):
                image_review.attach_reply(self, job, fitted.messages, history, current_input[-1],
                    snapshots, capture_memory=cfg.memory_enabled and turn.permits("memory"))
        from companion import image_understanding

        if image_understanding.has_pixels(fitted.messages) or history.review_target:
            rules.append(image_understanding.RULES)
        mark("context_fit")
        retained = self._freeze_reply_context(turn, fitted, selection, history, memory_refs,
            bounded_text=bounded_text, content_reference=content_reference, style=style,
            decision=decision)
        if snapshots is not None:
            snapshots.extend(retained)
            if planning:
                from companion.context_planner import initialize

                initialize(self, job, fitted.messages, fitted.retained_blocks(), snapshots,
                           assembly_dropped=fitted.dropped)
        mark("context_planning")
        return fitted.messages

    async def recall_for_reply(self, job):
        """A bounded LLM relevance check over candidates, never a full-log prompt."""
        reply_revision.check_job(self.store, job)
        authority = self._owner_job_view(job)
        if authority and "memory" not in authority.policy.purposes:
            return [], False
        query = json.loads(job["body_json"]).get("text", "")
        bounded_text = private_life.strict(self.store, self.settings)
        image_context = (
            qq_images.retrieval_text(self.store, self.settings, self.clock, job)
            if qq_images.enabled(self.settings)
            and qq_images.manifest(json.loads(job["body_json"])) else ""
        )
        allowed = live_text.memory_ids(self.store, self.settings, self.clock, job) if (
            bounded_text
        ) else None
        history = live_text.history_sources(self.store, self.settings, self.clock, job) if (
            bounded_text and self.settings.live_text_window.history_scope == "approved_window"
        ) else []
        if authority:
            history = owner_input.history_sources(self.store, self.settings, self.clock, job)
        elif private_life.enabled(self.settings):
            history = private_life.recent_history(self.store, self.settings, job)
        recent = [message for source in history
                  for message in dialogue_history.turn_messages(source, self.store)]
        retrieval_query = "\n".join(filter(None, [
            query, image_context, *(text for source in history
                     for text in (source["user_text"], source["reply_text"])),
        ]))
        retrieval_proof = self.memory_context.pin(
            self.clock(), event_ids={source["event_id"] for source in history},
        )
        try:
            hits = (await self.memory_context.recall_async(
                retrieval_query, self.clock(), memory_ids=allowed, project_query=query,
                clock=self.clock,
                source_check=lambda: self._check_context_snapshots(
                    job, [{"kind": "memory", "proof": retrieval_proof}],
                ),
            ))["rows"] if (
                retrieval_query.strip()
            ) else []
        except CoreError as error:
            if error.code == "PRIVACY_CHANGED":
                # A retrieval snapshot invalidated during inference is a cancelled reply,
                # just like a later context snapshot; it is not a fresh retry or window stop.
                raise CoreError(reply_revision.CANCELLED, 409) from error
            raise
        if not hits:
            if bounded_text:
                live_text.freeze_memory_review(
                    self.store, self.settings, self.clock, job, [], [], history,
                )
            return [], False
        catalog = {f"m{i}": row for i, row in enumerate(hits)}
        messages = [
            {"role": "system", "content": RECALL_RULES},
            {
                "role": "user",
                "content": encode(
                    {
                        "operation": "recall",
                        "query": query,
                        **({"image_observations": image_context} if image_context else {}),
                        **({"recent_dialogue": recent} if recent else {}),
                        "memories": [
                            {
                                "ref": ref,
                                **{
                                    key: row[key]
                                    for key in (
                                        "subject", "kind", "content", "status", "origin",
                                        "needs_review")
                                },
                            }
                            for ref, row in catalog.items()
                        ],
                    }
                ),
            },
        ]
        if bounded_text:
            live_text.freeze_memory_review(
                self.store, self.settings, self.clock, job, hits, messages, history,
            )
        proof = self.memory_context.pin(self.clock(), rows=hits,
                                        event_ids={source["event_id"] for source in history})
        reply_revision.remember_sources(self.store, job, proof["source_event_ids"])
        captured_epoch = epoch(self.store)

        def check_sources(current_request=None):
            # Keep the reply's existing forget/retry contract before checking the
            # stronger source proof. Recalled or superseded jobs remain cancelled.
            self._check_reply_epoch(job, captured_epoch)
            self._check_context_snapshots(job, [{"kind": "memory", "proof": proof}],
                                          current_request=current_request)

        try:
            request_id = budget.reserve(
                self.store, self.settings, job["job_id"], "memory_review", messages, self.clock(),
                clock=self.clock,
            )
            response = await budget.complete(
                self.model,
                self.store,
                self.settings,
                request_id,
                messages,
                "memory_review",
                self.clock,
                source_check=lambda: check_sources(request_id),
            )
            check_sources()
        except Exception as error:
            if not bounded_text and isinstance(error, CoreError) and error.code in {
                "DAILY_BUDGET_EXHAUSTED",
                "UNKNOWN_PRICING",
                "MEMORY_REVIEW_REQUIRES_LIVE_MODEL",
            }:
                self.store.audit(
                    "memory.recall_paused",
                    job["job_id"],
                    job["trace_id"],
                    self.clock(),
                    code=error.code,
                )
                return [], True
            raise
        validation_reason = "schema"
        try:
            selected = MemorySelection.model_validate_json(response.text).relevant
            if len(selected) != len(set(selected)):
                validation_reason = "duplicate_reference"
                raise ValueError("duplicate selection")
            if any(ref not in catalog for ref in selected):
                validation_reason = "unknown_reference"
                raise ValueError("unknown selection")
            rows = [catalog[ref] for ref in selected]
            if bounded_text:
                live_text.select_memory(
                    self.store, self.settings, self.clock, job, request_id, rows,
                )
            return rows, False
        except ValueError as error:
            self.store.audit("memory.recall_invalid", job["job_id"], job["trace_id"], self.clock(),
                             reason=validation_reason, request_id=request_id)
            usage = self.store.one('SELECT state FROM model_usage WHERE request_id = %s',
                                   (request_id,))
            if (not bounded_text and private_life.enabled(self.settings) and job["kind"] == "chat"
                and reply_revision.owner_input(self.store, job["event_id"])
                and not reply_revision.control_job(self.store, job)
                and usage and usage["state"] in {"SETTLED", "UNKNOWN"}):
                # Recall selection is optional context, not a memory write. Drop
                # every candidate and expose unavailability; never retry this paid
                # review or report an invalid selection as a valid empty result.
                self.store.audit(
                    "memory.recall_paused", job["job_id"], job["trace_id"], self.clock(),
                    code="MEMORY_REVIEW_INVALID", reason=validation_reason, request_id=request_id,
                )
                return [], True
            raise CoreError("MEMORY_REVIEW_INVALID", 502) from error

    async def prepare_memories(
        self, candidates, confirmed=False, job_id=None, logical_request_id=None, request_id=None
    ):
        from companion.memory_work import prepare_memories

        return await prepare_memories(self, candidates, confirmed, job_id, logical_request_id,
                                      request_id)

    def add_memories(self, candidates, resolutions, now, confirmed=False):
        """Caller owns the transaction and checks the captured privacy revision first."""
        if owner_authority.required(self.store, self.settings):
            owner_authority.current(self.store, self.settings, self.clock, purpose="memory")
        if private_life.strict(self.store, self.settings):
            raise CoreError("LIVE_TEXT_MEMORY_READ_ONLY", 403)
        private_life.ensure_active(self.store, self.settings, self.clock)
        added = {}
        for i, (candidate, resolution) in enumerate(zip(candidates, resolutions, strict=True)):
            same = resolution["same"]
            resolved = {
                "same": added.get(same, same),
                "conflicts": list(
                    dict.fromkeys(added.get(ref, ref) for ref in resolution["conflicts"])
                ),
                "replaces": [added.get(ref, ref) for ref in resolution["replaces"]],
            }
            if resolved["same"] in resolved["conflicts"]:
                raise CoreError("MEMORY_REVIEW_INVALID", 502)
            memory_id = self.memory.add(candidate, now, confirmed, resolved)
            if memory_id is None:
                raise CoreError("MEMORY_MATCH_STALE", 409)
            added[f"c{i}"] = memory_id
        return list(added.values())

    async def command_reply(self, job):
        text = json.loads(job["body_json"]).get("text", "").strip()
        command, arguments = command_parts(text)
        if not command:
            return None
        now, s = self.clock(), self.store
        if command == "重置互动" and not arguments:
            self.affect.reset(self.settings.owner_qq_id, job["event_id"], self.ledger)
            return "已重置互动反馈；之后的新反馈会重新生效。"
        if owner_authority.required(s, self.settings):
            purpose = ({"目标列表": "goals", "承诺列表": "goals", "接受目标": "goals",
                        "安排任务": "goals", "反思列表": "reflection", "当前交互状态": "affect",
                        "为什么形成偏好": "expression", "恢复自主活动": "life",
                        "恢复主动分享": "proactive", "表达候选": "expression",
                        "批准表达": "expression", "拒绝表达": "expression"}.get(command)
                       or ("memory" if is_memory_command(text)
                           and command != "忘记这一条" else None)
                       or ("diary" if is_diary_command(text)
                           and command not in {"暂停写日记", "删除日记"} else None))
            if purpose:
                owner_authority.check_job(s, self.settings, self.clock, job,
                                          purpose=purpose, executing=True)
        if command in {"读日记", "日记列表", "目标列表", "承诺列表", "反思列表",
                       "当前交互状态", "为什么形成偏好", "你记得什么"}:
            # These service responses do not yet expose complete source receipts.
            # Recall treats them conservatively instead of claiming exact lineage.
            job_queue.mark_context_untracked(s, job["job_id"])
        diary_reply = self.diary.command(text, self.settings.owner_qq_id)
        if diary_reply is not None:
            return diary_reply
        life_reply = self.life.command(text, job["event_id"])
        if life_reply is not None:
            return life_reply
        text = command + (" " + arguments if arguments else "")
        if text == "发送状态":
            uncertain = s.rows(
                "SELECT message_id, created_at FROM outbox WHERE state = 'DELIVERY_UNCERTAIN' "
                'ORDER BY record_sequence NULLS FIRST LIMIT 10'
            )
            return encode(uncertain) if uncertain else "没有待核对的发送。"
        reconciliation = re.fullmatch(r"核对发送\s+(msg_[a-f0-9]+)\s+(已发送|取消)", text)
        if reconciliation:
            outcome = "SENT" if reconciliation[2] == "已发送" else "CANCELLED"
            reconcile_delivery(s, reconciliation[1], outcome, now, self.settings,
                operation_id=job["event_id"],
                operation_sha256=digest({"event_id": job["event_id"],
                                         "message_id": reconciliation[1], "outcome": outcome}))
            return "已记录核对结果，不会重发该条消息。"
        if text in {"暂停主动分享", "恢复主动分享"}:
            with s.transaction():
                set_proactive_paused(s, text.startswith("暂停"), now)
            return "已" + text + "。"
        if not self.settings.memory_enabled:
            return None
        if command == "你记得什么":
            reply_revision.check_job(s, job)
            if arguments:
                target = re.fullmatch(r"历史\s+(mem_[a-f0-9]+)", arguments)
                if not target:
                    return "查看当前记忆用 /你记得什么；查看一条的修订用 /你记得什么 历史 mem_ID。"
                history = self.memory_context.history(target[1], self.clock())
                hits = history["revisions"][:6]
                proof = self.memory_context.pin(self.clock(), rows=hits, historical=True)
                reply_revision.remember_sources(s, job, proof["source_event_ids"])
                self._check_context_snapshots(job, [{"kind": "memory", "proof": proof}])
                return ("修订记录（记录时间；旧修订不是当前事实）：\n" + "\n".join(
                    f"{row['memory_id']} [{row['status']}] {row['created_at']} "
                    + row["content"][:200] + ("…" if len(row["content"]) > 200 else "")
                    for row in hits
                )) if hits else "当前没有可用来源支持的修订记录。"
            hits = self.memory_context.recall("*", self.clock())["rows"]
            proof = self.memory_context.pin(self.clock(), rows=hits)
            reply_revision.remember_sources(s, job, proof["source_event_ids"])
            self._check_context_snapshots(job, [{"kind": "memory", "proof": proof}])
            return (
                "\n".join(
                    f"{m['memory_id']} [{m['subject']}/{m['status']}] {m['content']}" for m in hits
                )
                or "目前还没有长期记忆。"
            )
        if command == "记住这个":
            content = arguments
            evidence_id = job["event_id"]
            if not content:
                prior = s.one(
                    (
                        'SELECT event_id, body_json FROM events WHERE actor_id '
                        "= 'owner' AND event_type IN ('message.received', "
                        "'message.media_received') AND redacted = 0 AND "
                        'conversation_id = %s AND record_sequence < (SELECT '
                        'record_sequence AS rowid FROM events WHERE event_id = '
                        '%s) ORDER BY record_sequence DESC NULLS LAST LIMIT 1'
                    ),
                    (job["conversation_id"], evidence_id),
                )
                if prior:
                    evidence_id = prior["event_id"]
                    content = json.loads(prior["body_json"]).get("text", "")
            if not content or len(content) > 500:
                return "请用“/记住这个：内容”提供 1–500 字的明确事实。"
            candidate = Candidate(
                subject="user",
                kind="fact",
                fact_key=content[:80],
                content=content,
                evidence_event_id=evidence_id,
                evidence_quote=content,
            )
            captured_epoch = epoch(s)
            candidates, resolutions = await self.prepare_memories(
                [candidate], confirmed=True, job_id=job["job_id"]
            )
            if not candidates:
                return "这条内容包含凭据，未保存为记忆。"
            with s.transaction(control_write=True):
                if captured_epoch != epoch(s):
                    raise CoreError("PRIVACY_CHANGED", 503)
                if not s.one(
                    "SELECT 1 FROM jobs WHERE job_id = %s AND state = 'RUNNING' AND lease_token = "
                    '%s AND lease_until > %s',
                    (job["job_id"], job["lease_token"], self.clock()),
                ):
                    raise CoreError("JOB_LEASE_EXPIRED", 409)
                reply_revision.check_job(s, job)
                memory_id = self.add_memories(candidates, resolutions, self.clock(),
                                              confirmed=True)[0]
                review = s.one('SELECT needs_review FROM memories WHERE memory_id = '
                    '%s', (memory_id,))
            if review["needs_review"]:
                return f"已记录：{memory_id}。这与已有记忆存在冲突，请澄清或用“/纠正这一条”修订。"
            return f"已记住：{memory_id}"
        match = re.fullmatch(
            r"(确认这一条|忘记这一条|纠正这一条)\s+(mem_[a-f0-9]+)(?:\s+([\s\S]+))?", text
        )
        if match:
            decision = {"确认这一条": "confirm", "忘记这一条": "forget", "纠正这一条": "correct"}[
                match[1]
            ]
            with s.transaction(control_write=True):
                if live_text.active(s, self.settings):
                    live_text.check_job(s, self.settings, self.clock, job)
                if not s.one(
                    "SELECT 1 FROM jobs WHERE job_id = %s AND state = 'RUNNING' AND lease_token = "
                    '%s AND lease_until > %s',
                    (job["job_id"], job["lease_token"], self.clock()),
                ):
                    raise CoreError("JOB_LEASE_EXPIRED", 409)
                result = self.memory.decide(
                    match[2], decision, self.clock(), match[3], evidence_event_id=job["event_id"],
                )
            return f"已处理：{result['memory_id']} · {result['state']}"
        return None

    def _check_reply_epoch(self, job, captured_epoch):
        if epoch(self.store) == captured_epoch:
            return
        tracked = self.store.one(
            'SELECT context_tracked FROM jobs WHERE job_id = %s', (job["job_id"],),
        )
        if private_life.enabled(self.settings) and tracked and tracked["context_tracked"]:
            # Revalidate the actual prompt sources. An unrelated recall must not
            # turn a current independent reply into a global window failure.
            reply_revision.check_job(self.store, job)
            return
        raise CoreError("PRIVACY_CHANGED", 503)

    async def _run_poke_once(self, worker_id):
        if not poke.enabled(self.settings):
            return False
        try:
            with claim_scope(self.store):
                job = poke.claim_ready(self.store, self.settings, self.clock, worker_id)
        except CoreError:
            return False
        if job is None:
            return False
        try:
            messages = poke.context(self.store, self.settings, job, clock=self.clock)
            request_id = budget.reserve(
                self.store, self.settings, job["job_id"], "qq_poke", messages, self.clock(),
                clock=self.clock,
            )
            response = await budget.complete(
                self.model, self.store, self.settings, request_id, messages, "qq_poke", self.clock,
                source_check=lambda: poke.check_job(
                    self.store, self.settings, None, job, clock=self.clock,
                ),
            )
            decision = poke.decide(response.text, self.settings.poke_policy)
            with self.store.transaction():
                approval = owner_authority.check_job(
                    self.store, self.settings, self.clock, job, purpose="poke", executing=True)
                poke.finish(
                    self.store, self.settings, approval, job, decision, self.clock(),
                )
                self.store.audit("poke.completed", job["job_id"], job["trace_id"], self.clock(),
                                 decision=decision.kind)
        except Exception as error:
            # A notice owns one attempt. Known usage remains settled; uncertainty
            # blocks the ordinary paid admission path and never creates a retry.
            code = error.code if isinstance(error, CoreError) else "PRIVATE_POKE_FAILED"
            with self.store.transaction():
                job_queue.release(self.store, job["job_id"], "FAILED", self.clock(), reason=code,
                                  token=job["lease_token"])
                self.store.audit("poke.failed", job["job_id"], job["trace_id"], self.clock(),
                                 error_code=code)
        return True

    @query_scope()
    async def run_once(self, worker_id="worker", *, job_id=None):
        with claim_scope(self.store):
            job = self.claim_job(worker_id, job_id=job_id)
        if job is not None and job["kind"] == "evaluation":
            from companion.evaluation import run as run_evaluation

            return await run_evaluation(self, job)
        if job is not None and job["kind"] == "rem":
            from companion.rem import run as run_rem

            return await run_rem(self, job)
        if job is None:
            if job_id is not None:
                return False
            if await self._run_poke_once(worker_id):
                return True
            if await self.memes.run_once(worker_id):
                return True
            return False
        started_ns = time.perf_counter_ns()
        emit(
            "t_job_started",
            job["trace_id"],
            elapsed_ms=(
                datetime.fromisoformat(self.clock()) - datetime.fromisoformat(job["created_at"])
            ).total_seconds()
            * 1000,
        )
        request_id = None
        context_snapshots = []
        conversation_store = None
        try:
            reply_revision.check_job(self.store, job)
            authority = self._owner_job_view(job)
            from companion.transport import native_text

            text_only = native_text(self.store, job["event_id"])
            bounded_text = authority is None and live_text.active(self.store, self.settings)
            generated_reply = False
            if bounded_text:
                live_text.check_job(self.store, self.settings, self.clock, job)
            before_command = epoch(self.store)
            reply = (
                await self.command_reply(job)
                if (job["kind"] == "chat"
                    and (not bounded_text or private_life.enabled(self.settings)))
                else None
            )
            reply_revision.check_job(self.store, job)
            captured_epoch = epoch(self.store)
            command, _ = command_parts(json.loads(job["body_json"]).get("text", ""))
            if (
                reply is not None
                and command in {
                    "读日记",
                    "日记列表",
                    "目标列表",
                    "承诺列表",
                    "反思列表",
                    "当前交互状态",
                    "为什么形成偏好",
                    "你记得什么",
                }
                and before_command != captured_epoch
            ):
                raise CoreError("PRIVACY_CHANGED", 503)
            if reply is None and job["kind"] == "chat":
                await self.memes.prepare_offer(job)
            if reply is None and job["kind"] == "chat" and authority is not None:
                from companion.agent_chat import reply as agent_reply

                reply, conversation_store = await agent_reply(self, job, context_snapshots)
                if reply is None:
                    return True
                reply = redact(reply, self.settings)
                generated_reply = True
            elif reply is None:
                if self.settings.memory_enabled and notices.reference(job) is None:
                    from companion import turn_recall
                    from companion.telemetry import phase

                    with phase("recall_memory", job.get("trace_id")):
                        hits, paused_memory = await self.recall_for_reply(job)
                    with phase("recall_turns", job.get("trace_id")):
                        ranking = await turn_recall.prefetch(
                            self.store, self.settings, job,
                            json.loads(job["body_json"]).get("text", ""))
                    self._check_reply_epoch(job, captured_epoch)
                    messages = self.context(job, hits, paused_memory,
                                            snapshots=context_snapshots, turn_ranking=ranking)
                else:
                    messages = self.context(job, snapshots=context_snapshots)
                self._check_reply_epoch(job, captured_epoch)
                self._check_context_snapshots(job, context_snapshots)
                emit(
                    "t_context_ready",
                    job["trace_id"],
                    elapsed_ms=(time.perf_counter_ns() - started_ns) / 1e6,
                )
                request_id = budget.reserve(
                    self.store,
                    self.settings,
                    job["job_id"],
                    job["kind"],
                    messages,
                    self.clock(),
                    clock=self.clock,
                )
                model_started_ns = time.perf_counter_ns()
                emit("t_model_request", job["trace_id"])
                response = await budget.complete(
                    self.model,
                    self.store,
                    self.settings,
                    request_id,
                    messages,
                    job["kind"],
                    self.clock,
                    source_check=lambda: self._check_context_snapshots(
                        job, context_snapshots, current_request=request_id,
                    ),
                )
                model_done_ns = time.perf_counter_ns()
                usage = self.store.one(
                    'SELECT billed_micro_usd, state FROM model_usage WHERE request_id = %s',
                    (request_id,),
                )
                emit(
                    "t_model_done",
                    job["trace_id"],
                    elapsed_ms=(model_done_ns - model_started_ns) / 1e6,
                    budget_micro_usd=usage["billed_micro_usd"] if usage else None,
                    model_usage_state=usage["state"] if usage else None,
                    at_ns=model_done_ns,
                )
                request_id = None
                reply = redact(response.text, self.settings)
                generated_reply = True
            from companion.agent_output import reply_format, song_choice

            voice_reply = (self.settings.voice.enabled and self.settings.voice.reply.mode == "model"
                           and generated_reply and job["kind"] == "chat"
                           and reply_format(self.store, job["event_id"]) == "voice")
            singing_reply = (generated_reply and job["kind"] == "chat"
                             and reply_format(self.store, job["event_id"]) == "singing")
            mixed_reply = (generated_reply and job["kind"] == "chat"
                           and reply_format(self.store, job["event_id"]) == "mixed")
            preview_only = any(s["kind"] == "video_preview_reply" for s in context_snapshots)
            quote_choice = None
            if (authority and generated_reply and not preview_only and job["kind"] == "chat"
                    and not text_only
                    and self.settings.quote_policy != "never"):
                quote_choice, reply = reply_quote.extract(reply)
            with self.store.transaction():
                task_settings = self._check_context_snapshots(job, context_snapshots)
                if bounded_text:
                    live_text.check_job(self.store, self.settings, self.clock, job)
                current = self.store.one('SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, '
                    'jobs.kind AS kind, jobs.state AS state, jobs.idempotency_key AS '
                    'idempotency_key, jobs.attempts AS attempts, jobs.max_attempts AS '
                    'max_attempts, jobs.lease_owner AS lease_owner, jobs.lease_until AS '
                    'lease_until, jobs.next_attempt_at AS next_attempt_at, jobs.last_error_code AS '
                    'last_error_code, jobs.created_at AS created_at, jobs.updated_at AS '
                    'updated_at, jobs.lease_token AS lease_token, jobs.context_tracked AS '
                    'context_tracked, jobs.authority_json AS authority_json FROM jobs AS jobs '
                    'WHERE jobs.job_id = %s', (job["job_id"],))
                if current["state"] != "RUNNING" or current["lease_token"] != job["lease_token"]:
                    return True
                if current["lease_until"] <= self.clock():
                    return True
                self._check_reply_epoch(job, captured_epoch)
                now = self.clock()
                message_ids = []
                reply_is_current = not reply_revision.owner_input(
                    self.store, job["event_id"],
                ) or reply_revision.current(self.store, job["event_id"])
                should_send = reply.strip() not in {"[SKIP]", ""} and proactive_allowed(
                    self.store, self.settings, now
                )
                notice_parts = notices.parts(self.store, self.settings, job, reply, now)
                if notices.shadow(job):
                    self.life_topics.submit_goal(job, self.goals)
                if notice_parts == []:
                    notices.record(self.store, self.settings, job, [], now)
                silent = (reply_is_current and generated_reply and job["kind"] == "chat"
                          and turn_taking.skipped(self.store, task_settings, job, reply))
                if reply_is_current and not silent and (
                    job["kind"] != "activity" or should_send
                    or notices.required(self.store, job["job_id"])
                ):
                    policy = self.settings.segmented_reply
                    intent = (
                        self.expression.intent(job["event_id"], reply, now)
                        if policy and generated_reply and job["kind"] == "chat" and not preview_only
                        and not voice_reply and not singing_reply and not mixed_reply
                        and not text_only
                        and self.settings.expression_enabled
                        and (authority is None or "expression" in authority.policy.purposes)
                        else None
                    )
                    if preview_only:
                        parts = split_text(reply, policy) if policy else [reply]
                        message_ids = enqueue_reply_parts(
                            self.store, job, parts, policy.interval_ms if policy else 0, now)
                    elif mixed_reply:
                        from companion import reply_composition

                        newer = self.store.one((
                            'SELECT 1 FROM events WHERE conversation_id = %s AND '
                            "actor_id = 'owner' AND source IN ('qq.onebot','native.chat') AND "
                            "event_type IN ('message.received', "
                            "'message.media_received') AND record_sequence > "
                            '(SELECT record_sequence AS rowid FROM events WHERE '
                            'event_id = %s) LIMIT 1'
                        ),
                            (job["conversation_id"], job["event_id"]))
                        message_ids = [] if newer else reply_composition.enqueue(self, job, now)
                    elif singing_reply:
                        from companion.agent_singing import selected

                        song_id = song_choice(self.store, job["event_id"])
                        selected(self.store, self.settings, self.clock, job, song_id)
                        song = self.voice.jobs.result(song_id)
                        parts = [reply if index == 0 else f"歌曲（{index + 1}/{len(song.audio)}）"
                                 for index in range(len(song.audio))]
                        message_ids = enqueue_reply_parts(self.store, job, parts, 0, now)
                        for index, message_id in enumerate(message_ids):
                            self.voice.freeze_song(message_id, song_id, song, index)
                    elif voice_reply:
                        message_ids = enqueue_reply_parts(self.store, job, [reply], 0, now)
                    elif notice_parts is not None:
                        action = ("qq.private.reply" if notices.required(self.store, job["job_id"])
                                  else "qq.private.proactive")
                        message_ids = enqueue_reply_parts(
                            self.store, job, notice_parts, policy.interval_ms if policy else 0,
                            now, action=action,
                        ) if notice_parts else []
                        notices.record(self.store, self.settings, job, message_ids, now)
                    elif policy and generated_reply and job["kind"] == "chat" and (
                        intent is None or intent.image is None or authority is not None
                        and not self.store.one(
                            'SELECT 1 FROM generated_images WHERE job_id = %s AND state = '
                            "'SUCCEEDED' AND message_id IS NULL", (job["job_id"],),
                        )
                    ):
                        parts = split_text(intent.text if intent else reply, policy)
                        image = (intent.image.model_dump(exclude_none=True)
                                 if intent is not None and intent.image is not None else None)
                        if image is not None and policy.max_segments != "unlimited":
                            # The picture occupies one of the existing visible-message slots.
                            text_slots = policy.max_segments - 1
                            if len(parts) > text_slots:
                                parts = parts[:text_slots - 1] + ["".join(parts[text_slots - 1:])]
                        if bounded_text:
                            approval, _ = live_text.check_job(
                                self.store, self.settings, self.clock, job,
                            )
                            if len(reply) > approval.max_reply_chars:
                                raise CoreError("LIVE_TEXT_SETTLED_REPLY_REQUIRED", 403)
                            available = live_text.remaining_send_slots(self.store, approval)
                            if available <= 0:
                                raise CoreError("LIVE_TEXT_SEND_ATTEMPT_CAP", 403)
                            if len(parts) > available:
                                parts = [reply]
                        if (len(parts) > 1 or image is not None) and self.store.one(
                            (
                                'SELECT 1 FROM events WHERE conversation_id = %s AND '
                                "actor_id = 'owner' AND event_type IN "
                                "('message.received', 'message.media_received') AND "
                                "source IN ('qq.onebot','native.chat') AND "
                                "record_sequence > (SELECT "
                                'record_sequence AS rowid FROM events WHERE event_id = '
                                '%s) LIMIT 1'
                            ), (job["conversation_id"], job["event_id"]),
                        ):
                            parts = []  # The owner already moved on while the model was answering.
                        quote_to = reply_quote.target(
                            self.store, self.settings, self.clock, job, quote_choice,
                        ) if authority and parts else None
                        message_ids = enqueue_reply_parts(
                            self.store, job, parts, policy.interval_ms, now, reply_to=quote_to,
                            image=image,
                        ) if parts else []
                    else:
                        create_reply = self.expression.enqueue if (
                            authority is None or "expression" in authority.policy.purposes
                        ) else lambda *args, **kwargs: enqueue(self.store, *args, **kwargs)
                        message_ids = [create_reply(
                            job["event_id"], job["conversation_id"], reply, now,
                            action="qq.private.proactive" if job["kind"] == "activity"
                            else "qq.private.reply",
                        )]
                    if (authority and message_ids and generated_reply and not text_only
                            and not voice_reply and not singing_reply and not mixed_reply
                            and job["kind"] == "chat"):
                        from companion import image_generation

                        image_generation.attach(self.store, job, message_ids, now)
                    if authority and message_ids:
                        owner_delivery.record_replies(
                            self.store, self.settings, self.clock, job, message_ids)
                    elif bounded_text and message_ids:
                        live_text.record_replies(
                            self.store, self.settings, self.clock, job, message_ids,
                        )
                if conversation_store is not None:
                    conversation_store.complete()
                job_queue.release(self.store, job["job_id"], "SUCCEEDED", now)
                from companion.native_stream import finish

                finish(self.store, job)
                if message_ids:
                    from companion.native_delivery import publish

                    publish(self.store, self.settings, self.clock, message_ids)
                self.store.audit("job.succeeded", job["job_id"], job["trace_id"], now,
                                 **({"reply_suppressed": True} if not reply_is_current else {}),
                                 **({"reply_skipped": True} if silent else {}))
            emit(
                "t_job_done",
                job["trace_id"],
                elapsed_ms=(time.perf_counter_ns() - started_ns) / 1e6,
            )
        except Exception as error:
            emit("job_failed", job["trace_id"])
            if request_id:
                usage = self.store.one(
                    'SELECT billed_micro_usd, state FROM model_usage WHERE request_id = %s',
                    (request_id,),
                )
                emit(
                    "t_model_failed",
                    job["trace_id"],
                    budget_micro_usd=usage["billed_micro_usd"] if usage else None,
                    model_usage_state=usage["state"] if usage else None,
                )
            self.fail_job(job, error.code if isinstance(error, CoreError) else "INTERNAL_ERROR")
        finally:
            if qq_images.enabled(self.settings):
                self.content.discard_images(job["event_id"])
        return True

    def fail_job(self, job, code):
        now = self.clock()
        with self.store.transaction():
            current = self.store.one('SELECT state, lease_token FROM jobs WHERE job_id = %s',
                                     (job["job_id"],))
            if (not current or current["state"] != "RUNNING"
                or current["lease_token"] != job["lease_token"]):
                return
            from companion.native_stream import interrupt

            interrupt(self, job)
            if code in {reply_revision.SUPERSEDED, reply_revision.SOURCE_RECALLED,
                        reply_revision.CANCELLED, "INPUT_RECALLED", "OWNER_SOURCE_CHANGED",
                        "OWNER_CAPTURE_INVALID", "OWNER_SOURCE_JOB_BINDING_REQUIRED"}:
                job_queue.release(self.store, job["job_id"], "CANCELLED", now, reason=code)
                return
            if notices.retry_required_wording(self.store, self.settings, job, code, now):
                return
            if notices.reference(job) is not None and code in {
                "MODEL_PROVIDER_CONCURRENCY_EXHAUSTED", "BACKGROUND_YIELD_TO_CHAT",
                "DAILY_BUDGET_EXHAUSTED", "MODEL_DAILY_BUDGET_EXHAUSTED",
            } and not self.store.one('SELECT 1 FROM model_usage WHERE job_id = %s',
                                     (job["job_id"],)):
                job_queue.retry_outcome(
                    self.store,
                    job["job_id"],
                    "RETRY_WAIT",
                    now,
                    after(now, 60),
                    reason=code,
                    token=job["lease_token"],
                    refund=True,
                )
                return
            if code == "CONVERSATION_RESTORE_BUSY":
                # Restore has not dispatched a model/tool. Yield this same lease
                # without touching saved requests, paid uncertainty or tool results.
                # A blocked job row falls back to the existing lease-expiry recovery.
                try:
                    with self.store.deadline(.25):
                        self.store.execute(
                            "UPDATE jobs SET state='RETRY_WAIT',lease_token=NULL,"
                            "last_error_code=%s,next_attempt_at=%s,"
                            "attempts=GREATEST(0,attempts-1),updated_at=%s "
                            "WHERE job_id=%s AND state='RUNNING' AND lease_token=%s",
                            (code, after(now, 1), now, job["job_id"], job["lease_token"]))
                except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable):
                    pass
                return
            checkpoint = self.store.one(
                'SELECT phase FROM agent_conversations WHERE job_id = %s', (job["job_id"],))
            admitted = self.store.one('SELECT 1 FROM model_usage WHERE job_id = %s',
                                      (job["job_id"],))
            resumable = (checkpoint and checkpoint["phase"] in {"READY", "WAITING"}
                         and not self.store.one(
                             "SELECT 1 FROM model_usage m WHERE m.job_id=%s "
                             "AND (m.state='RESERVED' "
                             "OR (m.state='UNKNOWN' AND NOT EXISTS(SELECT 1 FROM "
                             "agent_conversations c, jsonb_array_elements("
                             "c.conversation_json::jsonb->'messages') r(value) "
                             "WHERE c.job_id=m.job_id AND r.value->>'kind'='response' "
                             "AND r.value->'metadata'->>'core_request_id'=m.request_id)))",
                             (job["job_id"],))
                         and not self.store.one(
                             'SELECT 1 FROM agent_tool_calls WHERE job_id = %s AND state IN '
                             "('STARTED', 'UNKNOWN')", (job["job_id"],)))
            if code in budget.CAPACITY_WAIT_CODES and (not admitted or resumable):
                # Admission contention is a wait, including between SDK model turns.
                # A READY checkpoint retains paid responses and observed tool results;
                # an uncertain original attempt never authorizes this continuation.
                job_queue.retry_outcome(self.store, job["job_id"], "RETRY_WAIT", now, after(now, 1),
                                        reason=code, token=job["lease_token"], refund=True)
                return
            bounded_text = live_text.active(self.store, self.settings)
            if bounded_text:
                live_text.record_failure(self.store, self.settings, code)
            retryable = code in {
                "MODEL_RETRYABLE",
                "PRIVACY_CHANGED",
                "MEMORY_MATCH_STALE",
                "MEMORY_INDEX_UNAVAILABLE",
                "MEMORY_REVIEW_INVALID",
            }
            state = ("RETRY_WAIT" if retryable and not bounded_text
                     and job["attempts"] < job["max_attempts"] else "FAILED")
            changed = job_queue.retry_outcome(self.store, job["job_id"], state, now,
                                    after(now, 5 * 2 ** (job["attempts"] - 1)), reason=code,
                                    token=job["lease_token"])
            if changed:
                self.store.audit(
                    "job.failed", job["job_id"], job["trace_id"], now, code=code, state=state
                )
                if (state == "FAILED" and job["kind"] == "chat" and not bounded_text
                    and not owner_authority.required(self.store, self.settings)):
                    enqueue(
                        self.store,
                        job["event_id"],
                        job["conversation_id"],
                        f"这次回复暂时失败（{code}）。消息已记录，可以稍后再试。",
                        now,
                    )
                elif state == "FAILED" and job["kind"] == "chat" and not bounded_text:
                    self._failure_notice(job, now)

    def _failure_notice(self, job, now):
        """Tell the owner a reply finally failed; best effort, never blocks the failure."""
        if (reply_revision.control_job(self.store, job)
                or not reply_revision.owner_input(self.store, job["event_id"])
                or self.store.one('SELECT 1 FROM outbox WHERE origin_event_id = %s',
                                  (job["event_id"],))):
            return
        code = self.store.one('SELECT last_error_code FROM jobs WHERE job_id = %s',
                              (job["job_id"],))["last_error_code"]
        self.store.execute("SAVEPOINT failure_notice")
        try:
            message_id = enqueue(self.store, job["event_id"], job["conversation_id"],
                                 owner_delivery.failure_text(code), now,
                                 key=f"failure:{job['job_id']}:v1")
            owner_delivery.record_failure_notice(
                self.store, self.settings, self.clock, job["job_id"], message_id)
            from companion.native_delivery import publish

            publish(self.store, self.settings, self.clock, [message_id])
        except CoreError:
            # The grant or source no longer allows a send; the failure itself stands.
            self.store.execute("ROLLBACK TO SAVEPOINT failure_notice")
        self.store.execute("RELEASE SAVEPOINT failure_notice")

    async def extract_once(self, local_day=None):
        from companion.memory_work import extract_once

        return await extract_once(self, local_day)

    async def extract_step(self, local_day=None, *, on_checkpoint=None):
        from companion.memory_work import extract_step

        return await extract_step(self, local_day, on_checkpoint=on_checkpoint)

    def _owns_memory_lease(self, lease):
        current = self.store.one(
            "SELECT value FROM runtime_meta WHERE key = 'memory_extraction_lease'"
        )
        return bool(
            current and current["value"] == lease and json.loads(lease)["until"] > self.clock()
        )


    def status(self):
        from companion.ops_status import runtime_snapshot

        return runtime_snapshot(self.store, self.settings, self.clock)
