"""Private event admission and creation of its original reply job."""

from datetime import UTC

import psycopg

from companion import (
    input_debounce,
    input_protocol,
    job_queue,
    live_text,
    owner_authority,
    owner_input,
    private_life,
    qq_rich,
    reply_revision,
)
from companion.affect import AffectEngine
from companion.commands import command_parts, is_memory_command
from companion.content import ContentIntake
from companion.contracts import CoreError
from companion.diary import DiaryRepository, is_diary_command
from companion.life import is_life_command
from companion.privacy import redact
from companion.reading import service_for_store
from companion.store import encode, uid
from companion.transport import (
    cancel_interrupted_replies,
    cancel_proactive_work,
    digest,
    set_proactive_paused,
)


def receive(store, settings, clock, ledger, event, key, *, content_options):
    """Bind the same admission path to the HTTP request's independent connection."""
    reading = service_for_store(store, settings, clock)
    content = ContentIntake(store, settings, clock, reading.exploration.create,
                            options=content_options, video=reading.video)
    return ingest(
        store, settings, clock, ledger, event, key, content=content,
        diary=DiaryRepository(store, settings, clock), affect=AffectEngine(store, settings, clock),
    )


def ingest(s, cfg, clock, ledger, event, key, *, content, diary, affect):
    origin = event.conversation.platform_session_key
    if event.actor.platform_user_id != cfg.owner_qq_id:
        raise CoreError("OWNER_REQUIRED", 403)
    if not origin.endswith(f":FriendMessage:{cfg.owner_qq_id}"):
        raise CoreError("PRIVATE_OWNER_SESSION_REQUIRED", 403)
    if not event.source_event_id.startswith(f"{cfg.bot_qq_id}:{origin}:"):
        raise CoreError("INVALID_SOURCE_EVENT_ID", 400)
    if not key or len(key) > 512:
        raise CoreError("INVALID_IDEMPOTENCY_KEY", 400)
    raw = event.model_dump(mode="json", exclude={"event_id", "trace_id"})
    fingerprint = digest(raw)
    text = redact(event.payload.text, cfg)
    command, arguments = command_parts(text)
    reset_affect = command == "重置互动" and not arguments
    try:
        with input_protocol.admit_unrecalled(
            s, cfg, ledger, clock, event, control_write=reset_affect,
        ):
            now = clock()
            authority = owner_input.check_input(s, cfg, clock, event) if (
                owner_authority.required(s, cfg)) else None
            bounded_text = False if authority else live_text.check_input(
                s, cfg, clock, event)
            native_controls = not bounded_text or private_life.enabled(cfg)
            if (event.payload.rich and not bounded_text
                and cfg.rich_input_scope != "references_v1"):
                raise CoreError("RICH_INPUT_SCOPE_REQUIRED", 403)
            if s.one(
                'SELECT 1 FROM runtime_meta WHERE key = %s', ("erased_event:" + event.event_id,)
            ):
                raise CoreError("EVENT_ERASED", 410)
            existing = s.one(
                'SELECT events.event_id AS event_id, events.schema_version AS schema_version, '
                'events.source AS source, events.source_event_id AS source_event_id, '
                'events.conversation_id AS conversation_id, events.actor_id AS actor_id, '
                'events.event_type AS event_type, events.occurred_at AS occurred_at, '
                'events.ingested_at AS ingested_at, events.privacy_scope AS privacy_scope, '
                'events.body_json AS body_json, events.trace_id AS trace_id, '
                'events.idempotency_key AS idempotency_key, events.fingerprint AS fingerprint, '
                'events.redacted AS redacted, events.memory_processed AS memory_processed, '
                'events.reply_revision AS reply_revision FROM events AS events WHERE '
                '(events.source = %s AND events.source_event_id = %s) OR events.idempotency_key = '
                '%s OR events.event_id = %s',
                (event.source, event.source_event_id, key, event.event_id),
            )
            if existing:
                if existing["fingerprint"] != fingerprint or existing["idempotency_key"] != key:
                    raise CoreError("IDEMPOTENCY_CONFLICT")
                job = s.one(
                    'SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, jobs.kind AS kind, '
                    'jobs.state AS state, jobs.idempotency_key AS idempotency_key, jobs.attempts '
                    'AS attempts, jobs.max_attempts AS max_attempts, jobs.lease_owner AS '
                    'lease_owner, jobs.lease_until AS lease_until, jobs.next_attempt_at AS '
                    'next_attempt_at, jobs.last_error_code AS last_error_code, jobs.created_at AS '
                    'created_at, jobs.updated_at AS updated_at, jobs.lease_token AS lease_token, '
                    'jobs.context_tracked AS context_tracked, jobs.authority_json AS '
                    'authority_json FROM jobs AS jobs WHERE jobs.idempotency_key = %s',
                    ("reply:" + existing["event_id"],),
                )
                return {
                    "event_id": existing["event_id"],
                    "job_id": job["job_id"],
                    "duplicate": True,
                    "state": job["state"],
                    "reply_revision": existing["reply_revision"],
                }
            conversation = s.one(
                'SELECT conversations.conversation_id AS conversation_id, conversations.platform '
                'AS platform, conversations.platform_session_key AS platform_session_key, '
                'conversations.scope AS scope, conversations.owner_actor_id AS owner_actor_id, '
                'conversations.last_event_at AS last_event_at, conversations.created_at AS '
                'created_at, conversations.reply_revision AS reply_revision FROM conversations AS '
                'conversations WHERE conversations.platform = %s AND '
                'conversations.platform_session_key = %s',
                (event.source, origin),
            )
            conversation_id = conversation["conversation_id"] if conversation else uid("conv")
            if not conversation:
                s.insert(
                    "conversations",
                    conversation_id=conversation_id,
                    platform=event.source,
                    platform_session_key=origin,
                    scope="private",
                    owner_actor_id="owner",
                    created_at=now,
                )
            s.execute(
                'UPDATE conversations SET last_event_at = %s WHERE conversation_id = %s',
                (now, conversation_id),
            )
            body = event.payload.model_dump()
            body["text"] = text
            if body.get("rich"):
                for segment in body["rich"]["segments"]:
                    for field in segment.get("fields", []):
                        if field["kind"] != "url":
                            field["value"] = redact(field["value"], cfg)
                    for node in segment.get("body", {}).get("nodes", []):
                        node["text"] = redact(node["text"], cfg)
                        node["nickname"] = redact(node["nickname"], cfg)
                try:
                    qq_rich.validate(body["rich"])
                except ValueError:
                    raise CoreError("INVALID_RICH_INPUT", 400) from None
            diary_command = native_controls and is_diary_command(body["text"])
            control_command = native_controls and (
                is_life_command(body["text"]) or is_memory_command(body["text"])
                or command in {"发送状态", "核对发送", "暂停主动分享", "恢复主动分享"}
                or reset_affect
            )
            if control_command:
                body["control_command"] = True
                body["goal_request"] = command == "安排任务" and arguments.startswith("https://")
            if diary_command:
                body["diary_command"] = True
                body["diary_correction"] = command == "修改日记"
                body["diary_timezone"] = cfg.timezone
            if native_controls and command == "暂停写日记" and not arguments:
                diary.set_paused(True)
            if native_controls and command == "暂停主动分享" and not arguments:
                set_proactive_paused(s, True, now)
            # M0/M1 never fetch attachments; discard URL/ref content that may carry credentials.
            body["media"] = [{"kind": m["kind"], "sha256": m["sha256"]} for m in body["media"]]
            s.insert(
                "events",
                event_id=event.event_id,
                schema_version=1,
                source=event.source,
                source_event_id=event.source_event_id,
                conversation_id=conversation_id,
                actor_id="owner",
                event_type=event.event_type,
                occurred_at=event.occurred_at.astimezone(UTC).isoformat(
                    timespec="microseconds"
                ),
                ingested_at=now,
                privacy_scope="private",
                body_json=encode(body),
                trace_id=event.trace_id,
                idempotency_key=key,
                fingerprint=fingerprint,
                memory_processed=int(diary_command or control_command),
            )
            revision = 0
            debounce = None
            if authority or private_life.enabled(cfg):
                # Read the waiting batch before the new revision supersedes its job.
                debounce = input_debounce.due(s, cfg, conversation_id, event.event_id,
                                              body, now)
                revision = reply_revision.advance(s, conversation_id, event.event_id, now)
            else:
                cancel_interrupted_replies(s, conversation_id, now)
            if authority or private_life.enabled(cfg):
                cancel_proactive_work(s, now)
            job_id = job_queue.enqueue(
                s,
                event.event_id,
                "chat",
                "reply:" + event.event_id,
                now,
                max_attempts=cfg.max_attempts,
                next_attempt_at=debounce or None,
            )
            if authority:
                owner_authority.bind_job(s, cfg, clock, job_id, purpose="chat")
            if (not diary_command and not control_command
                and not body["text"].lstrip().startswith("/")):
                if not authority or "content" in authority.policy.purposes:
                    content.schedule(event.event_id)
            if native_controls and reset_affect:
                affect.reset(cfg.owner_qq_id, event.event_id, ledger)
            if not authority or "affect" in authority.policy.purposes:
                affect.observe(event.event_id)
            s.audit("event.accepted", event.event_id, event.trace_id, now)
            return {
                "event_id": event.event_id,
                "job_id": job_id,
                "duplicate": False,
                "state": "QUEUED",
                "reply_revision": revision,
            }
    except psycopg.IntegrityError as error:
        raise CoreError("IDEMPOTENCY_CONFLICT") from error
