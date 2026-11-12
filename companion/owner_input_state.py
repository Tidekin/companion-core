"""Read-only original input completion receipts for stopped Bridge custody."""

import json
from contextlib import nullcontext
from datetime import datetime

import companion_delivery_protocol as wire
from companion import owner_authority, owner_input, poke
from companion.contracts import CoreError
from companion.poke_protocol import OwnerPokeCapture
from companion.qq_rich import input_digest
from companion_delivery_protocol.authority import AuthorityRef, validate_authority_ref
from companion_delivery_protocol.native_chat import KIND as NATIVE_KIND
from companion_delivery_protocol.native_chat import SESSION as NATIVE_SESSION
from companion_delivery_protocol.native_chat import SOURCE as NATIVE_SOURCE


def _source(store, original, saved, owner_id):
    """Reuse signed input identity checks without requiring current execution authority.

    QQ and native device inputs share the capture contract; only the admission source and
    session they were signed for differ.
    """
    row = store.one('SELECT e.event_id AS event_id, e.schema_version AS schema_version, e.source '
        'AS source, e.source_event_id AS source_event_id, e.conversation_id AS conversation_id, '
        'e.actor_id AS actor_id, e.event_type AS event_type, e.occurred_at AS occurred_at, '
        'e.ingested_at AS ingested_at, e.privacy_scope AS privacy_scope, e.body_json AS body_json, '
        'e.trace_id AS trace_id, e.idempotency_key AS idempotency_key, e.fingerprint AS '
        'fingerprint, e.redacted AS redacted, e.memory_processed AS memory_processed, '
        'e.reply_revision AS reply_revision, c.scope AS scope, c.owner_actor_id AS owner_actor_id, '
        'c.platform_session_key AS platform_session_key FROM events AS e JOIN conversations AS c '
        'ON e.conversation_id = c.conversation_id WHERE e.event_id = %s',
                    (original.event_id,))
    # The signed capture kind decides which original admission path it must match:
    # a native device input never borrows a QQ identity and the reverse.
    native = original.kind == NATIVE_KIND
    source, session = ((NATIVE_SOURCE, NATIVE_SESSION) if native
                       else ("qq.onebot", "onebot-direct:FriendMessage:" + owner_id))
    try:
        body = json.loads(row["body_json"])
        if (not isinstance(body, dict) or row["redacted"]
            or (row["source"], row["actor_id"], row["event_type"], row["privacy_scope"],
                row["scope"], row["owner_actor_id"]) != (
                source, "owner", "message.received", "private", "private", "owner")
            or row["platform_session_key"] != session
            or row["source_event_id"] != original.source_event_id
            or datetime.fromisoformat(row["occurred_at"]) != original.occurred_at
            or input_digest(body) != original.input_sha256
            or saved.get("input_sha256") != original.input_sha256
            or saved.get("event_id") != original.event_id
            or saved.get("authority") != original.authority):
            raise ValueError("original input changed")
    except (ValueError, KeyError, TypeError):
        raise CoreError("OWNER_APPROVED_INPUT_REQUIRED", 409) from None


def _cleared_after(store, created_at):
    return bool(store.one("SELECT 1 FROM privacy_erasure_ledger WHERE state = 'COMPLETE' AND "
        'erased_at >= %s LIMIT 1', (created_at,)))


def _deliveries(store, rows, jobs, *, erased):
    """Terminal states retain the original native delivery identity and frozen bytes."""
    for row in rows:
        saved = store.one('SELECT value FROM runtime_meta WHERE key = %s',
                          ("owner_delivery:" + row["message_id"],))
        original = store.one('SELECT outbox.message_id AS message_id, outbox.origin_event_id AS '
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
            '= %s', (row["message_id"],))
        try:
            material = json.loads(saved["value"]) if saved else {}
            job = jobs.get(material.get("job_id"))
            authority = validate_authority_ref(json.loads(row["authority_json"]))
            content = json.loads(original["content_json"])
            intact = (material.get("content_sha256") == original["content_sha256"]
                      and wire.digest(content) == original["content_sha256"])
            # An erasure of this input, or a later raw-fact erasure (which clears every
            # earlier reply), retires the text but keeps the delivery identity.
            retired = original["content_sha256"] == "" and content == {} and (
                erased or _cleared_after(store, original["created_at"]))
            if (not job or not authority or material.get("authority") != authority
                or authority != json.loads(job["authority_json"])
                or any(material.get(key) != original[key] for key in (
                    "message_id", "action_type", "delivery_key"))
                or material.get("event_id") != original["origin_event_id"]
                or not (intact or retired)):
                raise ValueError("original delivery changed")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise CoreError("OWNER_DELIVERY_BINDING_REQUIRED", 409) from None


def read(store, settings, capture, *, model_requests=None):
    return store.read_or_write(lambda: _read(store, settings, capture,
                                           model_requests=model_requests))


def _read(store, settings, capture, *, model_requests=None):
    with nullcontext() if store.in_transaction else store.transaction():
        native_poke = isinstance(capture, OwnerPokeCapture)
        if native_poke:
            original, _, source = poke.verify_original(
                store, settings, capture.model_dump(mode="json"))
            input_hash = source["notice_sha256"]
        else:
            original, view = owner_input.verify(store, settings, capture.model_dump(mode="json"))
            input_hash = original.input_sha256
        event_id = original.event_id
        erased = bool(store.one('SELECT 1 FROM runtime_meta WHERE key = %s',
                                 ("erased_event:" + event_id,)))
        event = store.one('SELECT event_id, redacted, fingerprint, reply_revision FROM events '
            'WHERE event_id = %s',
                          (event_id,))
        revision = event.pop("reply_revision") if event else 0
        saved = store.one('SELECT value FROM runtime_meta WHERE key = %s',
                          ((poke.INPUT if native_poke else owner_input.INPUT) + event_id,))
        head = owner_authority._latest(store)
        ref = original.authority
        from companion import owner_stop_receipt

        stopped = bool(head and owner_stop_receipt.stopped_chain(
            store, AuthorityRef.from_dict(ref)))
        unadmitted = bool(stopped and event is None)
        if native_poke and event and not erased:
            poke.original_source(store, settings, event_id)
        saved_capture = json.loads(saved["value"]) if saved else {}
        original_value = original.model_dump(mode="json")
        matches = (saved_capture.get("capture_sha256") == wire.digest(original_value)
                   if native_poke else saved_capture.get("capture") == original_value)
        if not erased and (saved and not matches or not saved and not unadmitted):
            raise CoreError("OWNER_APPROVED_INPUT_REQUIRED", 404)
        if not native_poke and event and not erased:
            _source(store, original, saved_capture, view.policy.owner_qq_id)
        maximum = wire.MAX_INPUT_DELIVERIES
        jobs = store.rows('SELECT j.job_id, j.kind, j.state, j.authority_json FROM jobs AS j WHERE '
            'j.event_id = %s OR EXISTS(SELECT 1 FROM job_sources AS s WHERE s.job_id = j.job_id '
            'AND s.event_id = %s) ORDER BY j.job_id NULLS FIRST LIMIT %s',
            (event_id, event_id, maximum + 1))
        job_map = {job["job_id"]: job for job in jobs}
        ids = list(job_map)
        placeholders = ",".join('%s' for _ in ids)
        models = store.rows("SELECT m.request_id,m.state,m.job_id,a.request_id AS admission,"
            "a.authority_json,a.logical_request_id FROM model_usage m "
            "LEFT JOIN model_admissions a USING(request_id) "
            f"WHERE m.job_id IN ({placeholders}) OR a.logical_request_id IN ({placeholders}) "
            "ORDER BY m.request_id LIMIT %s", (*ids, *ids, maximum + 1)) if ids else []
        for model in models:
            job = job_map.get(model["job_id"])
            try:
                authority = validate_authority_ref(json.loads(model["authority_json"]))
                if (not job or not model["admission"]
                    or authority != json.loads(job["authority_json"])
                    or (job["kind"] in {"chat", "poke"}
                        or model["logical_request_id"].startswith("job_"))
                    and model["logical_request_id"] != model["job_id"]):
                    raise ValueError("original model admission changed")
            except (ValueError, TypeError, KeyError):
                raise CoreError("OWNER_AUTHORITY_MODEL_ADMISSION_REQUIRED", 409) from None
        models = [{key: model[key] for key in ("request_id", "state")} for model in models]
        if model_requests is not None:
            model_requests.extend(models)
        # Frozen responsibility survives a changed/null origin pointer. Inspect only
        # source-linked identity metadata; _deliveries checks the selected original records.
        deliveries = store.rows((
            'SELECT o.message_id,o.state,o.attempt_count,'
            'o.authority_json FROM outbox o LEFT JOIN runtime_meta '
            "m ON m.key='owner_delivery:' || o.message_id WHERE "
            'o.origin_event_id=%s OR CASE WHEN (m.value IS JSON) '
            'THEN (CASE WHEN m.value IS JSON THEN '
            "(m.value::json#>>'{event_id}') END)=%s OR (CASE WHEN "
            "m.value IS JSON THEN (m.value::json#>>'{job_id}') END) "
            'IN ('
            f"{placeholders or 'NULL'}"
            ') ELSE false END ORDER BY o.message_id LIMIT %s'
        ), (event_id, event_id, *ids, maximum + 1))
        _deliveries(store, deliveries, job_map, erased=erased)
        # Native link discovery does not dispatch a read. Retain the row and its
        # identity while distinguishing pristine declarations from admitted work.
        reads = store.rows("SELECT item_id AS id, CASE WHEN %s AND kind = 'link' AND state = "
            "'WAITING' AND redacted = 0 AND read_request_id IS NULL AND lease_token IS NULL AND "
            'lease_until IS NULL AND started_at IS NULL AND reserved_bytes = 0 AND '
            'usage_request_id IS NULL AND source_event_id IS NULL AND output_event_id IS NULL AND '
            "media_sha256 = '' AND fingerprint = '' AND error_code IS NULL THEN 'DISCOVERED' ELSE "
            'state END AS state FROM content_items WHERE owner_event_id = %s ORDER BY item_id '
            'NULLS FIRST LIMIT %s',
            (settings.owner_runtime.mode == "ENABLED", event_id, maximum + 1))
        reads.extend(store.rows('SELECT request_id AS id, state FROM exploration_requests WHERE '
            'request_id IN (SELECT read_request_id FROM content_items WHERE owner_event_id = %s) '
            'ORDER BY request_id NULLS FIRST LIMIT %s', (event_id, maximum + 1)))
        tools = store.rows('SELECT t.task_id, t.state FROM tool_tasks AS t JOIN tool_evidence AS e '
            'USING (task_id) WHERE e.event_id = %s ORDER BY t.task_id NULLS FIRST LIMIT %s',
            (event_id, maximum + 1))
        # UNKNOWN retains the outcome and liability. Only a token-bound cleanup
        # receipt, retired lease and idle node prove that its execution has ended.
        voices = store.rows('SELECT v.job_id AS id, v.state, v.cleanup_state, v.state IN '
            "('QUEUED', 'LEASED', 'RUNNING', 'UNKNOWN') AND NOT (v.state = 'UNKNOWN' AND "
            "v.content_retired = 1 AND v.cleanup_state = 'CONFIRMED' AND NOT "
            'v.cleanup_confirmed_at IS NULL AND NOT v.stop_token_sha256 IS NULL AND v.lease_json '
            'IS NULL AND v.lease_token_sha256 IS NULL AND EXISTS(SELECT 1 FROM voice_node_state AS '
            "n WHERE n.node_id = v.node_id AND n.resource_state = 'READY' AND n.active_job_id IS "
            'NULL AND n.claim_job_id IS NULL AND n.recovery_required = 0 AND n.last_error_code IS '
            "NULL AND (NOT EXISTS(SELECT 1 FROM runtime_meta WHERE key = 'voice_restore_required') "
            'OR n.recovered_incarnation = (SELECT value FROM runtime_meta WHERE key = '
            "'voice_incarnation')))) OR v.cleanup_state = 'PENDING' AS active FROM voice_jobs AS v "
            'WHERE v.event_id = %s ORDER BY v.job_id NULLS FIRST LIMIT %s', (event_id, maximum + 1))
        reads.extend({**item, "active": bool(item["active"])} for item in voices)
        videos = store.rows('SELECT item_id AS id, state FROM bili_video_items WHERE '
            'owner_event_id = %s ORDER BY item_id NULLS FIRST LIMIT %s', (event_id, maximum + 1))
        reads.extend({**item, "active": item["state"] in {
            "DECLARED", "PREPARED", "ANALYZED", "UNKNOWN"}} for item in videos)
        complete = all(len(items) <= maximum for items in (jobs, models, deliveries, reads, tools))
        # A native agent read keeps its link row WAITING and records progress only on the
        # exploration request, which `reads` already includes. Judge it there; the digest
        # keeps the stored rows unchanged.
        delegated = {row["id"] for row in store.rows(
            "SELECT item_id AS id FROM content_items WHERE owner_event_id = %s AND kind = 'link' "
            "AND state = 'WAITING' AND read_request_id IN (SELECT request_id FROM "
            'exploration_requests) LIMIT %s', (event_id, maximum + 1))}
        unsettled_work = any(item["state"] not in wire.JOB_TERMINAL
                             for item in jobs + tools) or any(
            item["id"] not in delegated and item.get("active", item["state"] in {
                "WAITING", "QUEUED", "FETCHING", "RUNNING", "LEASED", "UNKNOWN"})
                for item in reads)
        unsettled_model = any(item["state"] != "SETTLED" for item in models)
        unsettled_delivery = any(item["state"] not in wire.DELIVERY_TERMINAL for item in deliveries)
        primary = store.rows('SELECT job_id, state, authority_json FROM jobs WHERE event_id = %s '
            'AND kind = %s LIMIT 2', (event_id, "poke" if native_poke else "chat"))
        bound = bool(len(primary) == 1
                     and json.loads(primary[0]["authority_json"]) == original.authority)
        suppressed = bool(native_poke and event and saved and not jobs and not deliveries)
        terminal = bool(complete and (bound or erased or suppressed
            or unadmitted and not jobs and not deliveries)
            and not (
            unsettled_work or unsettled_model or unsettled_delivery))
        cancelled = bool(not native_poke and bound and primary[0]["state"] == "CANCELLED")
        # Source cancellation stops further I/O; only terminal proves all liability is settled.
        return {"authority": original.authority, "event_id": event_id,
            "reply_revision": revision,
            "input_sha256": input_hash, "terminal": terminal,
            "state": "RECALLED" if erased or event and event["redacted"] else (
                "CANCELLED" if cancelled or unadmitted and terminal
                else "COMPLETE" if terminal else "PENDING"),
            "unsettled_work": unsettled_work, "unsettled_model": unsettled_model,
            "unsettled_delivery": unsettled_delivery,
            "work_sha256": wire.digest({"event": event, "jobs": jobs, "models": models,
                                        "deliveries": deliveries, "reads": reads, "tools": tools})}
