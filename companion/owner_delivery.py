"""Current owner permission over native Outbox records and their original task evidence."""

import json
from datetime import datetime

from companion import (
    cumulative,
    notices,
    owner_authority,
    owner_input,
    paid_budget,
    reply_revision,
    transport,
)
from companion.contracts import CoreError
from companion.privacy import epoch
from companion.private_life import event_digest
from companion.store import encode

DELIVERY = "owner_delivery:"
FAILURE_NOTICE = "failure_notice"
FAILURE_TEXTS = {"SOURCE_GRANT_REQUIRED": "这个链接我现在读不了，得先在控制台给我开读取授权。"}
FAILURE_TEXT = "刚才那条没回出去，你再说一次吧。"


def failure_text(code):
    """Fixed Core wording for a chat reply that finally failed; never model output."""
    return FAILURE_TEXTS.get(code, FAILURE_TEXT)


def _job(store, job_id):
    return store.one('SELECT j.job_id AS job_id, j.event_id AS event_id, j.kind AS kind, j.state '
        'AS state, j.idempotency_key AS idempotency_key, j.attempts AS attempts, j.max_attempts AS '
        'max_attempts, j.lease_owner AS lease_owner, j.lease_until AS lease_until, '
        'j.next_attempt_at AS next_attempt_at, j.last_error_code AS last_error_code, j.created_at '
        'AS created_at, j.updated_at AS updated_at, j.lease_token AS lease_token, '
        'j.context_tracked AS context_tracked, j.authority_json AS authority_json, '
        'e.conversation_id AS conversation_id, e.body_json AS body_json FROM jobs AS j JOIN events '
        'AS e ON j.event_id = e.event_id WHERE j.job_id = %s', (job_id,))


def _snapshots(store, job, *, preview=False):
    if job["kind"] != "chat" or reply_revision.control_job(store, job):
        return []
    row = store.one('SELECT phase, snapshots_json FROM agent_conversations WHERE job_id = %s',
                     (job["job_id"],))
    try:
        snapshots = json.loads(row["snapshots_json"]) if row else []
        phases = {"OUTPUT", "COMPLETE"}
        if preview:
            phases |= {"MODEL_PENDING", "READY", "WAITING", "STOPPED"}
        if not row or row["phase"] not in phases or not isinstance(snapshots, list):
            raise ValueError("original context snapshots required")
        return snapshots
    except (ValueError, KeyError, TypeError) as error:
        raise CoreError(reply_revision.CANCELLED, 409) from error


def _history_proof(store, job, snapshots):
    if job["kind"] != "chat" or reply_revision.control_job(store, job):
        return []
    try:
        history = [item["proof"] for item in snapshots if item["kind"] == "history"]
        if len(history) != 1:
            raise ValueError("original history snapshot required")
        return history[0]
    except (ValueError, KeyError, TypeError) as error:
        raise CoreError(reply_revision.CANCELLED, 409) from error


def _images(store, settings, clock, job, snapshots, *, executing, _item_ids=None):
    from companion import agent_images

    try:
        saved = [item["item_ids"] for item in snapshots if item["kind"] == "images"]
        # A frozen text reply consumes the signed evidence, not original pixels.
        actual = agent_images.context_item_ids(store, settings, clock, job, executing=executing,
                                               _item_ids=_item_ids)
        if saved != ([actual] if actual else []):
            raise ValueError("original image snapshots changed")
    except (ValueError, KeyError, TypeError, CoreError) as error:
        raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403) from error


def _characters(store, settings, clock, snapshots):
    from companion.history_import import validate_character_proof

    try:
        for snapshot in snapshots:
            if snapshot["kind"] == "character":
                validate_character_proof(store, settings, clock, snapshot["proof"])
    except (ValueError, KeyError, TypeError, CoreError) as error:
        raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403) from error


def _memories(store, settings, clock, view, snapshots):
    from companion.memory import Memory
    from companion.memory_context import MemoryContext
    from companion.privacy import ErasureLedger

    try:
        proofs = [item["proof"] for item in snapshots if item["kind"] == "memory"]
        if proofs:
            owner_authority.current(store, settings, clock, purpose="memory",
                                     authority=view.authority)
            # Revalidation uses the original rows and full evidence roots. It
            # neither searches the embedding index nor invokes a model.
            context = MemoryContext(Memory(store, settings, ErasureLedger(settings.erasure_ledger)))
            for proof in proofs:
                context.revalidate(proof, clock())
    except (ValueError, KeyError, TypeError, CoreError) as error:
        raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403) from error


def _readings(store, settings, clock, job, snapshots, *, executing):
    from companion.agent_reading import revalidate_results

    try:
        revalidate_results(store, settings, clock, job, snapshots, executing=executing)
    except (ValueError, KeyError, TypeError, CoreError) as error:
        raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403) from error


def _sources(store, settings, clock, job, *, executing=False, preview=False):
    from companion.conversation_guard import read

    if job is None:
        raise CoreError("OWNER_DELIVERY_JOB_REQUIRED", 403)
    reads = read(store, job)
    view = owner_authority.check_job(store, settings, clock, job,
        purpose="poke" if job["kind"] == "poke" else owner_authority.model_purpose(
            store, job["kind"], job["job_id"]),
        executing=executing, _reads=reads)
    reply_revision._checked_job(job, reads.reply)
    from companion import runtime_config

    # Older prepared deliveries retain their original responsibility. New tasks
    # carry a signed configuration binding as well as their existing source proof.
    if reads.configuration is not None:
        runtime_config._bound_snapshot(store, settings, "job:" + job["job_id"],
                                        reads.configuration["binding"],
                                        reads.configuration["snapshot"])
    snapshots, history = [], []
    if job["kind"] == "chat":
        owner_input._checked_source(store, settings, view, job["event_id"], clock(),
                                     allow_commands=True, _loaded=reads.source)
        snapshots = _snapshots(store, job, preview=preview)
        history = _history_proof(store, job, snapshots)
        owner_input.check_history(store, settings, clock, job, history)
        _images(store, settings, clock, job, snapshots, executing=executing,
                 _item_ids=reads.image_item_ids)
        _memories(store, settings, clock, view, snapshots)
        _characters(store, settings, clock, snapshots)
        _readings(store, settings, clock, job, snapshots, executing=executing)
        from companion.agent_singing import revalidate

        revalidate(store, settings, clock, job, snapshots)
    elif job["kind"] == "activity":
        notices.validate(store, settings, job, clock())
    elif job["kind"] == "poke":
        from companion import poke

        poke.check_job(store, settings, view, job, clock=clock)
    else:
        raise CoreError("OWNER_DELIVERY_JOB_REQUIRED", 403)
    return view, snapshots, _context_hashes(history, snapshots)


def _context_hashes(history, snapshots):
    return {"history_sha256": paid_budget.sha(history),
            "context_sha256": paid_budget.sha(snapshots)}


def _usage(store, job, view, snapshots, message_id, *, group=None):
    from companion import authority_custody
    from companion.transport import reply_group

    preview = next((s for s in snapshots if s["kind"] == "video_preview_reply"), None)
    unavailable = (_preview_request(store, job, group or reply_group(store, message_id), preview)
                   if preview else None)
    rows = store.rows('SELECT m.request_id, m.state, m.scenario, a.authority_json, '
        'a.measurement_json, a.payload_sha256, a.snapshot_json, a.contract_sha256, '
        'a.journal_path, a.journal_id, '
        "CASE WHEN m.state='UNKNOWN' THEN EXISTS(SELECT 1 FROM agent_conversations AS c, "
        "jsonb_array_elements(c.conversation_json::jsonb->'messages') AS response(value) "
        "WHERE c.job_id=m.job_id AND c.phase IN ('OUTPUT','COMPLETE') "
        "AND response.value->>'kind'='response' "
        "AND response.value->'metadata'->>'core_request_id'=m.request_id) "
        "OR (m.scenario='activity' AND EXISTS(SELECT 1 FROM life_activity_results l "
        "WHERE l.job_id=m.job_id AND l.state='SHADOW' AND l.text<>'')) "
        "OR (m.scenario='qq_poke' AND EXISTS(SELECT 1 FROM poke_batches p "
        "WHERE p.job_id=m.job_id AND p.decision_sha256 IS NOT NULL)) ELSE false END "
        'AS response_recorded FROM model_usage '
        'AS m LEFT JOIN model_admissions AS a USING (request_id) WHERE m.job_id = %s ORDER BY '
        'm.record_sequence NULLS FIRST', (job["job_id"],))
    if (any(not (row["state"] == "SETTLED" or (
            row["state"] == "UNKNOWN" and (row["response_recorded"]
                or row["scenario"] != ("qq_poke" if job["kind"] == "poke" else job["kind"])
                or (
                row["scenario"] == "content_video" and row["request_id"] == unavailable))))
            or not row["authority_json"]
            or json.loads(row["authority_json"]) != view.authority.as_dict() for row in rows)
        or not reply_revision.control_job(store, job)
        and not any(row["scenario"] == ("qq_poke" if job["kind"] == "poke" else job["kind"])
                    for row in rows)):
        raise CoreError("OWNER_DELIVERY_SETTLED_MODEL_REQUIRED", 403)
    authority_custody.check_admissions(store, view, rows)
    return [row["request_id"] for row in rows]


def _preview_request(store, job, group, proof):
    """Only the settled parent tool call's pre-dispatch text may use this exception."""
    tool = store.one('SELECT t.metadata_json, t.result_json, c.output_text FROM agent_tool_calls '
        'AS t JOIN agent_conversations AS c USING (job_id) JOIN model_usage AS m ON m.request_id = '
        't.model_request_id AND m.job_id = t.job_id JOIN model_usage AS v ON v.job_id = t.job_id '
        "AND v.request_id = %s AND v.scenario = 'content_video' WHERE t.job_id = %s AND "
        "t.tool_call_id = %s AND t.tool_name = 'understand_video' AND t.state = 'RETURNED' AND "
        "c.request_id = m.request_id AND m.state IN ('SETTLED','UNKNOWN') AND m.scenario = "
        "'chat'", (proof["request_id"], job["job_id"], proof["call_id"]))
    if not tool or not group:
        raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403)
    metadata = json.loads(tool["metadata_json"] or "{}")
    if (metadata.get("request_id") != proof["request_id"] or metadata.get("kind") != "qq_animation"
        or metadata.get("preview_sha256") != proof["text_sha256"]
        or json.loads(tool["result_json"] or "{}") != {"state": "UNAVAILABLE", "reason": "HTTP_503"}
        or paid_budget.sha(group["text"]) != proof["text_sha256"]
        or group["text"] != tool["output_text"]
        or any(set(json.loads(item["content_json"])) != {"text"} for item in group["rows"])):
        raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403)
    return proof["request_id"]


def _material(store, row, job, view, requests, context_hashes, *, meme_binding=None):
    from companion.agent_output import reply_format, song_choice

    event = store.one('SELECT events.event_id AS event_id, events.schema_version AS '
        'schema_version, events.source AS source, events.source_event_id AS source_event_id, '
        'events.conversation_id AS conversation_id, events.actor_id AS actor_id, events.event_type '
        'AS event_type, events.occurred_at AS occurred_at, events.ingested_at AS ingested_at, '
        'events.privacy_scope AS privacy_scope, events.body_json AS body_json, events.trace_id AS '
        'trace_id, events.idempotency_key AS idempotency_key, events.fingerprint AS fingerprint, '
        'events.redacted AS redacted, events.memory_processed AS memory_processed, '
        'events.reply_revision AS reply_revision FROM events AS events WHERE events.event_id = '
        '%s', (row["origin_event_id"],))
    actions = ({"qq.private.reply", "qq.private.poke"} if job["kind"] == "poke"
               else {"qq.private.reply", "qq.private.proactive"})
    native = store.one("SELECT 1 FROM conversations WHERE conversation_id = %s AND "
                       "platform = 'native.chat'", (row["conversation_id"],))
    # A native conversation carries native actions, whether the origin is the owner's
    # native input or a system activity scheduled for that conversation.
    if (event and event["source"] == "native.chat") or (native and job["kind"] != "poke"):
        actions = {"native.private.reply", "native.private.proactive"}
    if (not event or row["origin_event_id"] != job["event_id"]
        or row["conversation_id"] != job["conversation_id"]
        or row["action_type"] not in actions
        or row["content_sha256"] != paid_budget.sha(json.loads(row["content_json"]))):
        raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403)
    return {"authority": view.authority.as_dict(), "job_id": job["job_id"],
            "privacy_epoch": epoch(store),
            "message_id": row["message_id"], "event_id": row["origin_event_id"],
            "event_sha256": event_digest(event), "content_sha256": row["content_sha256"],
            "delivery_key": row["delivery_key"], "action_type": row["action_type"],
            **context_hashes,
            "request_ids": requests,
            **({"reply_format": "voice"} if job["kind"] == "chat"
               and "voice" in view.policy.purposes
                and reply_format(store, job["event_id"], row) == "voice" else {}),
            **({"reply_format": "singing",
                 "singing_request_id": song_choice(store, job["event_id"], row)}
                if job["kind"] == "chat" and reply_format(store, job["event_id"], row) == "singing"
               else {}),
            **_composition_material(store, job),
            **({"meme_binding_version": meme_binding["version"],
                "meme_binding_sha256": paid_budget.sha(meme_binding)}
               if meme_binding is not None else {})}


def _composition_material(store, job):
    from companion.agent_output import composition

    output = composition(store, job["event_id"]) if job["kind"] == "chat" else None
    return ({"reply_composition_sha256": paid_budget.sha(output.model_dump(mode="json"))}
            if output is not None else {})


def _meme_binding(store, settings, clock, job, row):
    from companion.meme_core import MemeCaptionService
    from companion.meme_retrieval import MemeRetrieval, core_owned

    image = json.loads(row["content_json"]).get("image")
    if not core_owned(settings) or image is None:
        return None
    return MemeRetrieval(MemeCaptionService(store, settings, None, clock)).selected(
        job, image, executing=job["state"] == "RUNNING")


def _failure_view(store, settings, clock, job):
    if (job is None or job["kind"] != "chat" or job["state"] != "FAILED"
            or reply_revision.control_job(store, job)):
        raise CoreError("OWNER_DELIVERY_JOB_REQUIRED", 403)
    view = owner_authority.check_job(store, settings, clock, job, purpose="chat")
    owner_input.source(store, settings, view, job["event_id"], clock(), allow_commands=True)
    return view


def _failure_material(store, row, job, view):
    event = store.one('SELECT events.event_id AS event_id, events.schema_version AS '
        'schema_version, events.source AS source, events.source_event_id AS source_event_id, '
        'events.conversation_id AS conversation_id, events.actor_id AS actor_id, events.event_type '
        'AS event_type, events.occurred_at AS occurred_at, events.ingested_at AS ingested_at, '
        'events.privacy_scope AS privacy_scope, events.body_json AS body_json, events.trace_id AS '
        'trace_id, events.idempotency_key AS idempotency_key, events.fingerprint AS fingerprint, '
        'events.redacted AS redacted, events.memory_processed AS memory_processed, '
        'events.reply_revision AS reply_revision FROM events AS events WHERE events.event_id = '
        '%s', (row["origin_event_id"],))
    content = {"text": failure_text(job["last_error_code"])}
    if (not event or row["origin_event_id"] != job["event_id"]
            or row["conversation_id"] != job["conversation_id"]
            or row["action_type"] != (
                "native.private.reply" if event["source"] == "native.chat" else "qq.private.reply")
            or json.loads(row["content_json"]) != content
            or row["content_sha256"] != paid_budget.sha(content)):
        raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403)
    return {"kind": FAILURE_NOTICE, "authority": view.authority.as_dict(),
            "job_id": job["job_id"], "failure_code": job["last_error_code"],
            "privacy_epoch": epoch(store), "message_id": row["message_id"],
            "event_id": row["origin_event_id"], "event_sha256": event_digest(event),
            "content_sha256": row["content_sha256"], "delivery_key": row["delivery_key"],
            "action_type": row["action_type"]}


def bind_material(store, message_id, authority, material):
    """Pin the checked owner grant and its original evidence to the same outbox item."""
    transport.bind_authority(store, message_id, authority)
    store.insert("runtime_meta", key=DELIVERY + message_id, value=encode(material))


def record_failure_notice(store, settings, clock, job_id, message_id):
    """Bind Core's fixed failure wording to the failed job's own current owner grant.

    It carries no model output, so it needs no settled model call; it is valid only
    while the job stays FAILED and its original owner input is still admitted.
    """
    if not store.in_transaction:
        raise RuntimeError("owner delivery recording requires the enqueue transaction")
    job = _job(store, job_id)
    view = _failure_view(store, settings, clock, job)
    row = store.one('SELECT outbox.message_id AS message_id, outbox.origin_event_id AS '
        'origin_event_id, outbox.conversation_id AS conversation_id, outbox.action_type AS '
        'action_type, outbox.content_json AS content_json, outbox.content_sha256 AS '
        'content_sha256, outbox.approval_id AS approval_id, outbox.state AS state, '
        'outbox.delivery_key AS delivery_key, outbox.lease_owner AS lease_owner, '
        'outbox.lease_until AS lease_until, outbox.attempt_count AS attempt_count, '
        'outbox.provider_message_id AS provider_message_id, outbox.last_error_code AS '
        'last_error_code, outbox.created_at AS created_at, outbox.updated_at AS updated_at, '
        'outbox.sent_at AS sent_at, outbox.lease_token AS lease_token, outbox.next_attempt_at AS '
        'next_attempt_at, outbox.reply_group_id AS reply_group_id, outbox.segment_index AS '
        'segment_index, outbox.segment_count AS segment_count, outbox.reply_group_sha256 AS '
        'reply_group_sha256, outbox.segment_interval_ms AS segment_interval_ms, '
        'outbox.delivery_lane AS delivery_lane, outbox.native_ack_json AS native_ack_json, '
        'outbox.authority_json AS authority_json FROM outbox AS outbox WHERE outbox.message_id = '
        '%s', (message_id,))
    if row is None or row["state"] != "PENDING" or json.loads(row["authority_json"]):
        raise CoreError("OWNER_DELIVERY_ADMISSION_REQUIRED", 403)
    material = _failure_material(store, row, job, view)
    bind_material(store, message_id, view.authority.as_dict(), material)


def record_replies(store, settings, clock, job, message_ids):
    """Pin replies created by the still-running native model job."""
    if job["kind"] not in {"chat", "activity"}:
        raise CoreError("OWNER_DELIVERY_JOB_REQUIRED", 403)
    return _record_replies(store, settings, clock, job, message_ids, executing=True)


def record_poke_reply(store, settings, clock, job, message_ids):
    """The settled native interaction pins its decision before its running job completes."""
    if job["kind"] != "poke" or len(message_ids) != 1:
        raise CoreError("PRIVATE_POKE_DELIVERY_REQUIRED", 403)
    return _record_replies(store, settings, clock, job, message_ids, executing=True)


def record_life_draft(store, settings, clock, job, message_ids):
    """Consume settled Life wording under its independent current private-send purpose."""
    original = _job(store, job["job_id"])
    if (not original or original["state"] != "SUCCEEDED" or not notices.shadow(original)):
        raise CoreError("LIFE_SETTLED_DRAFT_REQUIRED", 409)
    # notices.validate uses LifeTopics.delivery_material for completed Life jobs:
    # source/wording integrity, current life+proactive and conversation revision.
    return _record_replies(store, settings, clock, original, message_ids, executing=False)


def _record_replies(store, settings, clock, job, message_ids, *, executing):
    """Pin each native Outbox item in the transaction that creates its complete group."""
    from companion.transport import reply_group, segment_envelopes

    if not store.in_transaction:
        raise RuntimeError("owner delivery recording requires the enqueue transaction")
    if not message_ids:
        return
    view, snapshots, context_hashes = _sources(store, settings, clock, job, executing=executing)
    first = store.one('SELECT action_type FROM outbox WHERE message_id=%s', (message_ids[0],))
    group = None if first and first["action_type"] == "qq.private.poke" else reply_group(
        store, message_ids[0])
    requests = _usage(store, job, view, snapshots, message_ids[0], group=group)
    if group:
        envelopes = segment_envelopes(group)
        if (len(group["text"]) > view.policy.max_reply_chars
            or [row["message_id"] for row in group["rows"]] != message_ids):
            raise CoreError("OWNER_DELIVERY_GROUP_CHANGED", 403)
        policy = settings.segmented_reply
        from companion.agent_output import reply_format, song_choice

        mixed = job["kind"] == "chat" and reply_format(store, job["event_id"]) == "mixed"
        is_song = job["kind"] == "chat" and reply_format(store, job["event_id"]) == "singing"
        if mixed:
            from companion.reply_composition import check_group, check_item

            check_group(store, settings, clock, job, group["rows"])
            for part in group["rows"]:
                check_item(store, settings, job, part)
        if is_song:
            from companion.agent_singing import selected

            manifest = selected(store, settings, clock, job, song_choice(store, job["event_id"]))
            if (len(message_ids) != len(manifest["segments"])
                or any(item["interval_ms"] != 0 for item in envelopes.values())):
                raise CoreError("OWNER_DELIVERY_GROUP_CHANGED", 403)
        elif group["segmented"] and (
            policy is None and not mixed
            or policy is not None and policy.max_segments != "unlimited"
            and len(message_ids) > policy.max_segments
            or any(item["interval_ms"] != (policy.interval_ms if policy else 0)
                   for item in envelopes.values())):
            raise CoreError("OWNER_DELIVERY_GROUP_CHANGED", 403)
    for message_id in message_ids:
        row = store.one('SELECT outbox.message_id AS message_id, outbox.origin_event_id AS '
            'origin_event_id, outbox.conversation_id AS conversation_id, outbox.action_type AS '
            'action_type, outbox.content_json AS content_json, outbox.content_sha256 AS '
            'content_sha256, outbox.approval_id AS approval_id, outbox.state AS state, '
            'outbox.delivery_key AS delivery_key, outbox.lease_owner AS lease_owner, '
            'outbox.lease_until AS lease_until, outbox.attempt_count AS attempt_count, '
            'outbox.provider_message_id AS provider_message_id, outbox.last_error_code AS '
            'last_error_code, outbox.created_at AS created_at, outbox.updated_at AS updated_at, '
            'outbox.sent_at AS sent_at, outbox.lease_token AS lease_token, outbox.next_attempt_at '
            'AS next_attempt_at, outbox.reply_group_id AS reply_group_id, outbox.segment_index AS '
            'segment_index, outbox.segment_count AS segment_count, outbox.reply_group_sha256 AS '
            'reply_group_sha256, outbox.segment_interval_ms AS segment_interval_ms, '
            'outbox.delivery_lane AS delivery_lane, outbox.native_ack_json AS native_ack_json, '
            'outbox.authority_json AS authority_json FROM outbox AS outbox WHERE outbox.message_id '
            '= %s', (message_id,))
        if row is None or row["state"] != "PENDING" or json.loads(row["authority_json"]):
            raise CoreError("OWNER_DELIVERY_ADMISSION_REQUIRED", 403)
        material = _material(store, row, job, view, requests, context_hashes,
                              meme_binding=_meme_binding(store, settings, clock, job, row))
        if job["kind"] == "poke":
            from companion import poke

            poke.check_delivery(store, settings, view, message_id, clock=clock)
        bind_material(store, message_id, view.authority.as_dict(), material)


def check_delivery(store, settings, clock, message_id, *, check=None):
    if not store.in_transaction:
        return store.read_or_write(
            lambda: check_delivery(store, settings, clock, message_id, check=check))
    from companion.social import proactive_allowed
    from companion.transport import check_segment_ready, reply_group

    row = store.one('SELECT o.message_id AS message_id, o.origin_event_id AS origin_event_id, '
        'o.conversation_id AS conversation_id, o.action_type AS action_type, o.content_json AS '
        'content_json, o.content_sha256 AS content_sha256, o.approval_id AS approval_id, o.state '
        'AS state, o.delivery_key AS delivery_key, o.lease_owner AS lease_owner, o.lease_until AS '
        'lease_until, o.attempt_count AS attempt_count, o.provider_message_id AS '
        'provider_message_id, o.last_error_code AS last_error_code, o.created_at AS created_at, '
        'o.updated_at AS updated_at, o.sent_at AS sent_at, o.lease_token AS lease_token, '
        'o.next_attempt_at AS next_attempt_at, o.reply_group_id AS reply_group_id, o.segment_index '
        'AS segment_index, o.segment_count AS segment_count, o.reply_group_sha256 AS '
        'reply_group_sha256, o.segment_interval_ms AS segment_interval_ms, o.delivery_lane AS '
        'delivery_lane, o.native_ack_json AS native_ack_json, o.authority_json AS authority_json, '
        'c.scope AS scope, c.owner_actor_id AS owner_actor_id FROM outbox AS o JOIN conversations '
        'AS c ON o.conversation_id = c.conversation_id WHERE o.message_id = %s', (message_id,))
    saved = store.one('SELECT value FROM runtime_meta WHERE key = %s', (DELIVERY + message_id,))
    if not row or not saved or (row["scope"], row["owner_actor_id"]) != ("private", "owner"):
        raise CoreError("OWNER_DELIVERY_BINDING_REQUIRED", 403)
    material = json.loads(saved["value"])
    job = _job(store, material["job_id"])
    group = None
    if material.get("kind") == "reply_recovery":
        from companion import reply_recovery

        view = reply_recovery.check(store, settings, clock, row, material, check)
    elif material.get("kind") == FAILURE_NOTICE:
        view = _failure_view(store, settings, clock, job)
        if (json.loads(row["authority_json"]) != view.authority.as_dict()
                or material != _failure_material(store, row, job, view)):
            raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403)
    else:
        view, snapshots, context_hashes = _sources(store, settings, clock, job)
        # Reuse only inside this exact transaction/snapshot. Every later RPC
        # still re-reads current authorization, sources, lease and segment state.
        if row["reply_group_id"] and store.frozen_scope is not None:
            group = reply_group(store, message_id)
        requests = _usage(store, job, view, snapshots, message_id, group=group)

        if (job["state"] != "SUCCEEDED"
            or json.loads(row["authority_json"]) != view.authority.as_dict()
            or material != _material(store, row, job, view, requests, context_hashes,
                meme_binding=_meme_binding(store, settings, clock, job, row))):
            raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403)
    reply_revision.check_delivery(store, message_id)
    if job["kind"] == "poke":
        from companion import poke

        poke.check_delivery(store, settings, view, message_id, clock=clock)
    if problem := notices.delivery_problem(store, settings, message_id, clock()):
        raise CoreError(problem, 403)
    proactive = row["action_type"] in {"qq.private.proactive", "native.private.proactive"}
    if proactive and not proactive_allowed(
        store, settings, clock(),
    ):
        raise CoreError("PROACTIVE_SEND_PAUSED", 409)
    if row["reply_group_id"]:
        now = clock()
        if not row["action_type"].startswith("native."):
            check_segment_ready(store, message_id, now, group=group)
        state = cumulative.check(store, settings, now)
        if state["policy"]:
            group = group if group is not None else reply_group(store, message_id)
            remaining = sum(part["state"] in {"PENDING", "RETRY_WAIT", "LEASED", "SENDING"}
                and "send:" + cumulative.digest(part["message_id"]) not in state["operations"]
                for part in group["rows"])
            # Read-only allowance for the whole remaining group. Actual attempts are
            # recorded individually by send_check; an ACK prefix is already accounted.
            projected = cumulative.liability(now, settings.paid_model.window_id,
                row["origin_event_id"], "RESERVED", send_attempts=remaining)
            if remaining and cumulative.violations(state, projected):
                raise CoreError("CUMULATIVE_LIMIT_EXHAUSTED", 429)
    if check is not None:
        if (check.authority != view.authority.as_dict()
            or check.event_id != row["origin_event_id"]
            or check.content_sha256 != row["content_sha256"]
            or check.lease_token != row["lease_token"] or row["state"] != "SENDING"
            or not row["lease_until"] or row["lease_until"] <= clock()):
            raise CoreError("OWNER_DELIVERY_LEASE_LOST", 409)
        if reply_revision.owner_input(store, row["origin_event_id"]):
            if check.reply_revision is None:
                raise CoreError("OWNER_DELIVERY_REVISION_REQUIRED", 403)
            reply_revision.require_reply_source(store, check.event_id, check.reply_revision)
    deadlines = [datetime.fromisoformat(row["lease_until"])] if row["lease_until"] else []
    if view.policy.expires_at:
        deadlines.append(view.policy.expires_at)
    return {"approved": True, **({"lease_until": min(deadlines).isoformat()} if deadlines else {})}


def asset_lease(store, clock, message_id, body, permission):
    """Check the asset request inside the caller's authorized delivery transaction."""
    row = store.one('SELECT outbox.message_id AS message_id, outbox.origin_event_id AS '
        'origin_event_id, outbox.conversation_id AS conversation_id, outbox.action_type AS '
        'action_type, outbox.content_json AS content_json, outbox.content_sha256 AS '
        'content_sha256, outbox.approval_id AS approval_id, outbox.state AS state, '
        'outbox.delivery_key AS delivery_key, outbox.lease_owner AS lease_owner, '
        'outbox.lease_until AS lease_until, outbox.attempt_count AS attempt_count, '
        'outbox.provider_message_id AS provider_message_id, outbox.last_error_code AS '
        'last_error_code, outbox.created_at AS created_at, outbox.updated_at AS updated_at, '
        'outbox.sent_at AS sent_at, outbox.lease_token AS lease_token, outbox.next_attempt_at AS '
        'next_attempt_at, outbox.reply_group_id AS reply_group_id, outbox.segment_index AS '
        'segment_index, outbox.segment_count AS segment_count, outbox.reply_group_sha256 AS '
        'reply_group_sha256, outbox.segment_interval_ms AS segment_interval_ms, '
        'outbox.delivery_lane AS delivery_lane, outbox.native_ack_json AS native_ack_json, '
        'outbox.authority_json AS authority_json FROM outbox AS outbox WHERE outbox.message_id = '
        '%s', (message_id,))
    if (
        row["state"] not in {"LEASED", "SENDING"}
        or not row["lease_until"]
        or row["lease_until"] <= clock()
        or body.lease_token != row["lease_token"]
        or body.authority != json.loads(row["authority_json"])
        or body.event_id != row["origin_event_id"]
        or body.content_sha256 != row["content_sha256"]
    ):
        raise CoreError("OWNER_DELIVERY_LEASE_LOST", 409)
    reply_revision.require_reply_source(store, body.event_id, body.reply_revision)
    return row, {
        "message_id": message_id,
        "delivery_key": row["delivery_key"],
        "event_id": body.event_id,
        "reply_revision": body.reply_revision,
        "authority": body.authority,
        "content_sha256": body.content_sha256,
        "lease_until": permission["lease_until"],
    }


def send_check(store, settings, clock, body):
    from companion.voice_jobs import REPLY_RECEIPT

    with store.transaction(delivery_preflight=True):
        result = check_delivery(store, settings, clock, body.message_id, check=body)
        content = json.loads(store.one('SELECT content_json FROM outbox WHERE message_id = %s',
                                        (body.message_id,))["content_json"])
        if content.get("image") is not None:
            owner_authority.current(store, settings, clock,
                                     purpose="meme", authority=body.authority)
        if content.get("generated_image") is not None:
            from companion import image_generation

            image_generation.check_send(store, settings, clock, body.authority)
        if body.voice is not None or store.one(
            (
                'SELECT 1 FROM runtime_meta WHERE key=%s AND (CASE WHEN '
                "value IS JSON THEN (value::json#>>'{state}') "
                "END)='AUDIO'"
            ),
            (REPLY_RECEIPT + body.message_id,),
        ):
            from companion.voice_flow import VoiceFlow

            owner_authority.current(store, settings, clock,
                                    purpose="voice", authority=body.authority)
            VoiceFlow(store, settings, clock).validate_send(
                body.message_id, body.lease_token, body.voice)
        cumulative.admit_operation(store, settings, clock(), "send", body.message_id,
            paid_budget.sha(body.model_dump(mode="json")), logical=body.event_id)
        return result
