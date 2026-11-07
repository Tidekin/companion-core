import hashlib
import json
import re
from contextlib import nullcontext
from datetime import datetime

from companion import reply_revision
from companion.contracts import CoreError
from companion.store import after, encode, uid
from companion_delivery_protocol import preflight
from companion_delivery_protocol.reply_groups import manifest, mixed_part, part_hash


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def enqueue(store, event_id, conversation_id, text, now, action="qq.private.reply", key=None, *,
            reply_to=None):
    if action not in {"qq.private.reply", "qq.private.proactive"}:
        raise CoreError("CAPABILITY_DISABLED", 403)
    with nullcontext() if store.db.in_transaction else store.transaction():
        if action == "qq.private.reply":
            reply_revision.require_reply_source(store, event_id)
        content = {"text": text}
        if reply_to is not None:
            # Only a Core-resolved owner input id; the Bridge adds the native quote.
            if action != "qq.private.reply" or not valid_reply_to({"message_id": reply_to}):
                raise CoreError("INVALID_REPLY_QUOTE", 400)
            content["reply_to"] = {"message_id": reply_to}
        key = key or f"reply:{event_id}:v1"
        previous = store.one("SELECT message_id FROM outbox WHERE delivery_key=?", (key,))
        if previous:
            return previous["message_id"]
        message_id = uid("msg")
        store.insert(
            "outbox",
            message_id=message_id,
            origin_event_id=event_id,
            conversation_id=conversation_id,
            action_type=action,
            content_json=encode(content),
            content_sha256=digest(content),
            state="PENDING",
            delivery_key=key,
            created_at=now,
            updated_at=now,
        )
        return message_id


def _group_manifest(job_id, parts, interval_ms, *, contents=None, version=None):
    version = version or (2 if contents is not None else 1)
    hashes = ([part_hash(content, version) for content in contents] if contents is not None
              else [digest(text) for text in parts])
    return manifest(job_id, hashes, sum(map(len, parts)), interval_ms, version)


def _mixed_content(content, event_id):
    choice = mixed_part(content)
    if choice["kind"] == "meme":
        from companion.expression import ImageIntent

        image = ImageIntent.model_validate(content["image"])
        if (image.source != "companion.meme_catalog" or image.offer_key != "meme:" + event_id
            or image.model_dump(exclude_none=True) != content["image"]):
            raise ValueError("invalid mixed meme binding")
    return choice


def _meme_tail(content, event_id):
    from companion.expression import ImageIntent

    if set(content) != {"text", "image"} or content["text"] != "":
        raise ValueError("invalid meme tail")
    image = ImageIntent.model_validate(content["image"])
    if image.model_dump(exclude_none=True) != content["image"] or (
        image.offer_key is not None and image.offer_key != "meme:" + event_id
    ):
        raise ValueError("invalid meme binding")


def valid_reply_to(value):
    return (isinstance(value, dict) and set(value) == {"message_id"}
            and isinstance(value["message_id"], str)
            and re.fullmatch(r"-?[1-9][0-9]{0,19}", value["message_id"]) is not None)


def enqueue_reply_parts(store, job, parts, interval_ms, now, *, action="qq.private.reply",
                        reply_to=None, image=None):
    """Freeze all parts atomically; a retried job cannot recut or duplicate its reply."""
    if (
        not isinstance(parts, list) or not 1 <= len(parts) <= 2**31 - 1
        or any(not isinstance(text, str) or not text.strip() for text in parts)
        or sum(map(len, parts)) > 20000
        or type(interval_ms) is not int or not 0 <= interval_ms <= 3000
    ):
        raise CoreError("INVALID_REPLY_SEGMENTS", 400)
    contents = [{"text": text} for text in parts]
    if reply_to is not None:
        contents[0]["reply_to"] = {"message_id": reply_to}
    if image is not None:
        tail = {"text": "", "image": image}
        try:
            _meme_tail(tail, job["event_id"])
            if job["kind"] != "chat" or action != "qq.private.reply":
                raise ValueError("private chat reply required")
        except (ValueError, TypeError) as error:
            raise CoreError("INVALID_REPLY_SEGMENTS", 400) from error
        contents.append(tail)
    return _enqueue_contents(store, job, parts, contents, interval_ms, now, action=action,
                             reply_to=reply_to, version=2 if image is not None else 1)


def enqueue_mixed_reply(store, job, contents, interval_ms, now):
    if (job["kind"] != "chat" or not isinstance(contents, list) or not 1 <= len(contents) <= 64
        or type(interval_ms) is not int or not 0 <= interval_ms <= 3000):
        raise CoreError("INVALID_REPLY_SEGMENTS", 400)
    try:
        for content in contents:
            _mixed_content(content, job["event_id"])
        if sum(len(content["text"]) for content in contents) > 20000:
            raise ValueError("mixed reply too long")
    except (ValueError, TypeError, KeyError) as error:
        raise CoreError("INVALID_REPLY_SEGMENTS", 400) from error
    return _enqueue_contents(store, job, [content["text"] for content in contents], contents,
                             interval_ms, now, action="qq.private.reply", version=3)


def _enqueue_contents(store, job, parts, contents, interval_ms, now, *, action, version,
                      reply_to=None):
    with nullcontext() if store.db.in_transaction else store.transaction():
        reply_revision.check_job(store, job)
        source = store.one(
            "SELECT j.job_id,j.kind,e.event_id,e.conversation_id,e.redacted "
            "FROM jobs j JOIN events e USING(event_id) WHERE j.job_id=?", (job["job_id"],),
        )
        if (not source or source["kind"] not in {"chat", "activity", "reply_recovery"}
            or action not in {"qq.private.reply", "qq.private.proactive"}
            or (source["kind"] in {"chat", "reply_recovery"} and action != "qq.private.reply")
            or source["redacted"] or any(
            source[key] != job[key] for key in ("event_id", "conversation_id")
        )):
            raise CoreError("REPLY_GROUP_SOURCE_CHANGED", 409)
        existing = store.rows(
            "SELECT * FROM outbox WHERE origin_event_id=? AND action_type=? "
            "ORDER BY rowid", (job["event_id"], action),
        )
        if existing:
            group = reply_group(store, existing[0]["message_id"])
            if (
                len(group["rows"]) != len(existing)
                or [json.loads(row["content_json"]) for row in group["rows"]] != contents
                or (group["segmented"] and any(
                    row["reply_group_id"] != job["job_id"]
                    or row["segment_interval_ms"] != interval_ms for row in group["rows"]
                ))
            ):
                raise CoreError("REPLY_GROUP_CHANGED", 409)
            return [row["message_id"] for row in group["rows"]]
        if len(contents) == 1 and version != 3:
            return [enqueue(store, job["event_id"], job["conversation_id"], parts[0], now,
                            action=action, reply_to=reply_to)]
        group_hash = digest(_group_manifest(job["job_id"], parts, interval_ms,
                            contents=contents if version in (2, 3) else None, version=version))
        ids = []
        for index, content in enumerate(contents):
            message_id = enqueue(store, job["event_id"], job["conversation_id"],
                                 content["text"], now,
                                 key=f"reply:{job['event_id']}:v2:{index}", action=action,
                                 reply_to=reply_to if index == 0 else None)
            if version in (2, 3):
                store.execute("UPDATE outbox SET content_json=?,content_sha256=? "
                              "WHERE message_id=?",
                              (encode(content), digest(content), message_id))
            store.execute(
                "UPDATE outbox SET reply_group_id=?,segment_index=?,segment_count=?,"
                "reply_group_sha256=?,segment_interval_ms=? WHERE message_id=?",
                (job["job_id"], index, len(contents), group_hash, interval_ms, message_id),
            )
            ids.append(message_id)
        return ids


def reply_group(store, message_id, *, require_sent=False):
    """Read one immutable reply; missing/tampered parts are never repaired from a prefix."""
    row = store.one("SELECT * FROM outbox WHERE message_id=?", (message_id,))
    if not row:
        raise CoreError("REPLY_GROUP_MISSING", 409)
    try:
        if not row.get("reply_group_id"):
            if ":v2:" in row["delivery_key"]:
                raise ValueError("missing group")
            return None if require_sent and row["state"] != "SENT" else {
                "rows": [row], "text": json.loads(row["content_json"])["text"],
                "segmented": False,
            }
        rows = store.rows("SELECT * FROM outbox WHERE reply_group_id=? ORDER BY segment_index",
                          (row["reply_group_id"],))
        source = store.one(
            "SELECT j.event_id,e.conversation_id,j.kind,e.redacted FROM jobs j "
            "JOIN events e USING(event_id) WHERE j.job_id=?", (row["reply_group_id"],),
        )
        if (
            not source or source["redacted"]
            or source["kind"] not in {"chat", "activity", "reply_recovery"}
            or not 1 <= len(rows) <= 2**31 - 1 or row["segment_count"] != len(rows)
            or row["action_type"] not in {"qq.private.reply", "qq.private.proactive"}
            or (source["kind"] in {"chat", "reply_recovery"}
                and row["action_type"] != "qq.private.reply")
            or (source["event_id"], source["conversation_id"]) != (
                row["origin_event_id"], row["conversation_id"])
            or type(row["segment_interval_ms"]) is not int
            or not 0 <= row["segment_interval_ms"] <= 3000
        ):
            raise ValueError("invalid group")
        contents = [json.loads(part["content_json"]) for part in rows]
        mixed = any("reply_part" in content for content in contents)
        media = any("image" in content for content in contents)
        if not mixed and len(rows) < 2:
            raise ValueError("invalid group")
        parts = []
        for index, part in enumerate(rows):
            content = contents[index]
            tail = not mixed and media and index == len(rows) - 1
            if mixed:
                _mixed_content(content, row["origin_event_id"])
            if tail:
                _meme_tail(content, row["origin_event_id"])
            if (
                part["segment_index"] != index or part["segment_count"] != len(rows)
                or any(part[key] != row[key] for key in (
                    "origin_event_id", "conversation_id", "action_type", "reply_group_sha256",
                    "segment_interval_ms"))
                or part["delivery_key"] != f"reply:{row['origin_event_id']}:v2:{index}"
                or not mixed and not tail and set(content) - (
                    {"reply_to"} if index == 0 else set()) != {"text"}
                or ("reply_to" in content and not valid_reply_to(content["reply_to"]))
                or mixed and "reply_to" in content and index != 0
                or not isinstance(content["text"], str)
                or not mixed and not tail and not content["text"].strip()
                or mixed and (source["kind"] != "chat" or row["action_type"] != "qq.private.reply")
                or tail and (source["kind"] != "chat" or row["action_type"] != "qq.private.reply")
                or part["content_sha256"] != digest(content)
            ):
                raise ValueError("invalid part")
            parts.append(content["text"])
        if sum(map(len, parts)) > 20000 or row["reply_group_sha256"] != digest(
            _group_manifest(row["reply_group_id"], parts, row["segment_interval_ms"],
                            contents=contents if media or mixed else None,
                            version=3 if mixed else None)
        ):
            raise ValueError("invalid manifest")
        if require_sent and any(part["state"] != "SENT" for part in rows):
            return None
        return {"rows": rows, "text": "".join(parts), "segmented": True,
                **({"version": 3} if mixed else {"version": 2} if media else {})}
    except (ValueError, TypeError, KeyError) as error:
        raise CoreError("REPLY_GROUP_CHANGED", 409) from error


def segment_envelope(store, message_id):
    return segment_envelopes(reply_group(store, message_id))[message_id]


def segment_envelopes(group):
    """Project envelopes from one freshly validated reply_group; performs no reads."""
    if not group["segmented"]:
        return {group["rows"][0]["message_id"]: None}
    version = group.get("version", 1)
    hashes = [part_hash(json.loads(part["content_json"]), version) for part in group["rows"]]
    return {
        row["message_id"]: {
            "version": version, "group_id": row["reply_group_id"], "index": row["segment_index"],
            "count": len(group["rows"]), "group_sha256": row["reply_group_sha256"],
            "total_chars": len(group["text"]), "interval_ms": row["segment_interval_ms"],
            "part_sha256s": hashes,
        }
        for row in group["rows"]
    }


def check_segment_ready(store, message_id, now, *, group=None):
    group = group if group is not None else reply_group(store, message_id)
    if not group["segmented"]:
        return
    row = next(part for part in group["rows"] if part["message_id"] == message_id)
    previous = group["rows"][:row["segment_index"]]
    if any(part["state"] != "SENT" or not part["sent_at"] for part in previous):
        raise CoreError("REPLY_SEGMENT_NOT_READY", 409)
    if previous and datetime.fromisoformat(now) < datetime.fromisoformat(after(
        previous[-1]["sent_at"], row["segment_interval_ms"] / 1000,
    )):
        raise CoreError("REPLY_SEGMENT_NOT_READY", 409)


def _cancel_unsent(store, now, reason, condition, params, *, retain_lease=False):
    """Caller owns the transaction. Scope predicates are internal SQL; values are bound."""
    # A superseded lease still identifies the Bridge's pre-dispatch cancellation proof.
    # Even when retiring its token, keep the lease deadline as evidence of a prior lease.
    retire = "" if retain_lease else "lease_token=NULL,"
    store.execute(
        f"UPDATE outbox SET state='CANCELLED',{retire}updated_at=?,last_error_code=? "
        "WHERE state IN ('PENDING','LEASED','RETRY_WAIT') AND " + condition,
        (now, reason, *params),
    )


def cancel_interrupted_replies(store, conversation_id, now):
    """An accepted legacy-mode input interrupts the conversation's unsent groups."""
    if store.one("SELECT 1 FROM pragma_table_info('outbox') WHERE name='reply_group_id'"):
        _cancel_unsent(store, now, "REPLY_INTERRUPTED_BY_OWNER",
                       "conversation_id=? AND reply_group_id IS NOT NULL",
                       (conversation_id,), retain_lease=True)


def cancel_superseded_replies(store, conversation_id, event_id, now):
    _cancel_unsent(store, now, reply_revision.SUPERSEDED,
                   "conversation_id=? AND action_type='qq.private.reply' AND origin_event_id!=? "
                   "AND origin_event_id IN (SELECT event_id FROM events WHERE "
                   + reply_revision.OWNER_INPUT + ")",
                   (conversation_id, event_id), retain_lease=True)


def cancel_origin_replies(store, event_id, now):
    _cancel_unsent(store, now, reply_revision.SUPERSEDED,
                   "origin_event_id=? AND action_type='qq.private.reply'", (event_id,),
                   retain_lease=True)


def mark_not_dispatched(store, message_id, now, *, attempt_count):
    """Apply a verified Bridge non-dispatch receipt inside its settlement transaction."""
    store.execute(
        "UPDATE outbox SET state='CANCELLED',updated_at=?,attempt_count=?,"
        "last_error_code='REPLY_NOT_DISPATCHED' WHERE message_id=?",
        (now, attempt_count, message_id),
    )


def cancel_segment_tail(store, row, now, *, reason="REPLY_SEGMENT_CHAIN_STOPPED"):
    if row["reply_group_id"] is not None:
        _cancel_unsent(store, now, reason, "reply_group_id=? AND segment_index>?",
                       (row["reply_group_id"], row["segment_index"]))


def _cancel_blocked_delivery(store, row, now, reason):
    _cancel_unsent(store, now, reason, "message_id=?", (row["message_id"],))
    cancel_segment_tail(store, row, now)


def recover_deliveries(store, now):
    store.execute(
        "UPDATE outbox SET state='PENDING',lease_token=NULL,lease_until=NULL "
        "WHERE state='LEASED' AND lease_until<=?",
        (now,),
    )
    store.execute(
        "UPDATE outbox SET state='DELIVERY_UNCERTAIN',updated_at=?,"
        "last_error_code='SEND_LEASE_EXPIRED' WHERE state='SENDING' AND lease_until<=?",
        (now, now),
    )
    # Recovery must also find interrupted groups left by an earlier process.
    _cancel_unsent(store, now, "REPLY_SEGMENT_CHAIN_STOPPED",
                   "reply_group_id IN (SELECT reply_group_id FROM outbox "
                   "WHERE reply_group_id IS NOT NULL AND state IN "
                   "('FAILED','CANCELLED','DELIVERY_UNCERTAIN'))", ())


def cancel_proactive_work(store, now):
    # Caller holds the write transaction. A dispatched model still owns settlement;
    # logical cancellation must not turn its eventual observed usage into UNKNOWN.
    store.execute(
        "UPDATE jobs SET state='CANCELLED',lease_token=NULL,updated_at=?,"
        "last_error_code='AUTONOMY_PAUSED' WHERE kind='activity' "
        "AND event_id NOT IN (SELECT event_id FROM events WHERE "
        "json_extract(body_json,'$.notice.kind') IN ('commitment','life')) "
        "AND state IN ('QUEUED','RUNNING','RETRY_WAIT')",
        (now,),
    )
    _cancel_unsent(store, now, "AUTONOMY_PAUSED", "action_type='qq.private.proactive'", ())


def _delivery_scope(store, settings, request, now):
    from companion import account_actions, owner_authority

    owner = owner_authority.required(store, settings)
    if owner:
        if request.authority is None:
            raise CoreError("OWNER_AUTHORITY_REFERENCE_REQUIRED", 403)
        owner_authority.current(store, settings, lambda: now, authority=request.authority)
    account = request.delivery_lane == "qq_account"
    approval = (account_actions.scope(store, settings, lambda: now,
        request.authority, request.account_actions_authorization_sha256) if account else None)
    return owner, account, approval


def _empty_delivery_poll(store, settings, request, now):
    from companion.social import proactive_allowed

    # No recovery, tail cancellation or proactive cancellation can write in this
    # state. A committed WAL snapshot is sufficient to return an empty batch.
    if store.one("SELECT 1 FROM outbox WHERE state IN "
                 "('PENDING','RETRY_WAIT','LEASED','SENDING') LIMIT 1") or store.one(
        "SELECT 1 FROM jobs WHERE kind='activity' "
        "AND state IN ('QUEUED','RUNNING','RETRY_WAIT') LIMIT 1"
    ):
        return False
    _delivery_scope(store, settings, request, now)
    proactive_allowed(store, settings, now)
    return True


def lease_deliveries(store, settings, request, now):
    from companion import (
        account_actions,
        owner_authority,
        owner_delivery,
        poke,
        private_life,
        reply_recovery,
    )
    from companion.social import proactive_allowed, share_delivery_problem

    if store.read_or_write(lambda: _empty_delivery_poll(store, settings, request, now)):
        return {"items": []}
    with store.transaction():
        recover_deliveries(store, now)
        owner, account, approval = _delivery_scope(store, settings, request, now)
        if not proactive_allowed(store, settings, now):
            cancel_proactive_work(store, now)
        # One delivery at a time per conversation. Uncertain items are never replayed;
        # later ordinary replies can continue so the owner can reconcile over QQ.
        rows = store.rows(
            "SELECT o.*,c.platform_session_key FROM outbox o JOIN conversations c "
            "ON c.conversation_id=o.conversation_id WHERE o.state IN ('PENDING','RETRY_WAIT') "
            "AND (o.next_attempt_at IS NULL OR o.next_attempt_at<=?) AND c.scope='private' "
            "AND c.owner_actor_id='owner' AND o.delivery_lane=? AND ((? AND o.action_type IN "
            "('qzone.publish','qq.profile.nickname','qq.profile.signature','qq.profile.avatar')) "
            "OR (NOT ? AND o.action_type IN "
            "('qq.private.reply','qq.private.proactive','qq.private.poke'))) "
            "AND (o.action_type!='qq.private.poke' OR ?) "
            "AND NOT EXISTS (SELECT 1 FROM outbox b "
            "WHERE b.conversation_id=o.conversation_id AND b.delivery_lane=o.delivery_lane "
            "AND b.rowid<o.rowid AND b.state IN "
            "('PENDING','RETRY_WAIT','LEASED','SENDING')) "
            "ORDER BY o.rowid LIMIT ?",
            (now, request.delivery_lane, int(account), int(account),
             int((owner or private_life.enabled(settings)) and poke.enabled(settings)),
             request.limit),
        )
        items = []
        for row in rows:
            if account:
                account_actions.scope(store, settings, lambda: now, request.authority,
                    request.account_actions_authorization_sha256, message_id=row["message_id"])
            try:
                if owner and not account:
                    owner_delivery.check_delivery(store, settings, lambda: now, row["message_id"])
                reply_revision.check_delivery(store, row["message_id"])
            except CoreError as error:
                if error.code not in {reply_revision.SUPERSEDED, reply_revision.SOURCE_RECALLED,
                                      reply_revision.CANCELLED, "OWNER_SOURCE_CHANGED",
                                      "OWNER_CAPTURE_INVALID", "OWNER_SOURCE_JOB_BINDING_REQUIRED",
                                      "OWNER_DELIVERY_SOURCE_CHANGED", "SINGING_RESULT_CHANGED",
                                      "SINGING_ORIGINAL_SOURCE_CHANGED",
                                      "SINGING_ORIGINAL_TOOL_REQUIRED",
                                      "SINGING_DISABLED", "SINGING_SOURCE_UNAVAILABLE",
                                      "LIFE_DRAFT_EXPIRED", "LIFE_DRAFT_CHANGED",
                                      "LIFE_PRIVATE_DELIVERY_PAUSED"}:
                    raise
                _cancel_blocked_delivery(store, row, now, error.code)
                continue
            problem = share_delivery_problem(store, settings, row["message_id"], now)
            if problem:
                _cancel_blocked_delivery(store, row, now, problem)
                continue
            if row.get("reply_group_id"):
                try:
                    check_segment_ready(store, row["message_id"], now)
                except CoreError as error:
                    if error.code == "REPLY_SEGMENT_NOT_READY":
                        continue
                    raise
            voice_flow = None
            if (not account and settings.voice.enabled
                and not reply_recovery.is_delivery(store, row["message_id"])
                and (not owner or "voice" in owner_authority.current(
                    store, settings, lambda: now).policy.purposes)
                and row["action_type"] in {"qq.private.reply", "qq.private.proactive"}
                and "generated_image" not in json.loads(row["content_json"])
                and not store.one("SELECT 1 FROM poke_batches WHERE response_message_id=?",
                                  (row["message_id"],))):
                from companion.voice_flow import VoiceFlow

                voice_flow = VoiceFlow(store, settings, lambda: now)
                try:
                    decision = voice_flow.prepare_reply(row["message_id"])
                except CoreError as error:
                    from companion.agent_output import reply_format

                    if (reply_format(store, row["origin_event_id"], row) != "singing"
                        or error.code not in {"SINGING_DELIVERY_FENCED",
                                              "VOICE_FLOW_RECEIPT_INVALID"}):
                        raise
                    _cancel_blocked_delivery(store, row, now, error.code)
                    continue
                if decision.state == "WAITING":
                    continue
            token = uid("lease")
            store.execute(
                "UPDATE outbox SET state='LEASED',lease_owner=?,lease_token=?,"
                "lease_until=?,updated_at=? WHERE message_id=?",
                (
                    request.worker_id,
                    token,
                    after(now, settings.lease_seconds),
                    now,
                    row["message_id"],
                ),
            )
            if account:
                try:
                    account_actions.lease_receipt(store, settings, lambda: now,
                        row["message_id"], token, approval)
                except CoreError as error:
                    store.execute("UPDATE outbox SET state='CANCELLED',lease_token=NULL,"
                                  "updated_at=?,last_error_code=? WHERE message_id=?",
                                  (now, error.code, row["message_id"]))
                    continue
            items.append(
                {
                    "message_id": row["message_id"],
                    "conversation_platform_session_key": row["platform_session_key"],
                    "content": json.loads(row["content_json"]),
                    "lease_token": token,
                    "lease_until": after(now, settings.lease_seconds),
                    "delivery_key": row["delivery_key"],
                    "action_type": row["action_type"],
                }
            )
            if row.get("reply_group_id"):
                items[-1]["reply_segment"] = segment_envelope(store, row["message_id"])
            if voice_flow is not None:
                voice = voice_flow.delivery_envelope(row["message_id"], token)
                if voice is not None:
                    items[-1]["voice"] = voice.model_dump(mode="json")
            if account:
                items[-1].update(delivery_lane="qq_account",
                                 authority=approval.authority.as_dict(),
                                 content_sha256=row["content_sha256"])
                continue
            if owner:
                items[-1].update(authority=json.loads(row["authority_json"]),
                                 origin_event_id=row["origin_event_id"],
                                 content_sha256=row["content_sha256"])
                if (reply_revision.owner_input(store, row["origin_event_id"])
                    or reply_recovery.is_delivery(store, row["message_id"])):
                    items[-1]["reply_revision"] = store.one(
                        "SELECT reply_revision FROM events WHERE event_id=?",
                        (row["origin_event_id"],),
                    )["reply_revision"]
                if preflight.eligible(items[-1]):
                    # Recovery clears expired LEASED tokens. Only the most recently
                    # issued envelope can settle a retired, unstarted lease.
                    store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)",
                        ("owner_preflight_lease:" + row["message_id"], encode({
                            "item_sha256": digest(items[-1]), "lease_sha256": digest(token),
                            "lease_until": items[-1]["lease_until"]})))
                continue
            from companion.private_life import enabled

            if enabled(settings):
                items[-1]["window_origin_event_id"] = row["origin_event_id"]
                from companion import notices

                notice = notices.delivery_job(store, row["message_id"])
                if notice and notices.required(store, notice["job_id"]):
                    items[-1]["window_notice"] = "commitment"
                if (row["action_type"] == "qq.private.reply"
                    and reply_revision.owner_input(store, row["origin_event_id"])):
                    items[-1]["reply_revision"] = store.one(
                        "SELECT reply_revision FROM events WHERE event_id=?",
                        (row["origin_event_id"],),
                    )["reply_revision"]
        return {"items": items}


def begin_send(store, message_id, token, now, settings=None, *, clock=None):
    problem = None
    with store.transaction(delivery_preflight=True):
        from companion import live_text, owner_authority, owner_delivery

        if not store.one("SELECT 1 FROM outbox WHERE message_id=? AND state='LEASED' "
                         "AND lease_token=? AND lease_until>?", (message_id, token, now)):
            raise CoreError("STALE_DELIVERY_LEASE")
        reply_revision.check_delivery(store, message_id)
        account = store.one("SELECT 1 FROM outbox WHERE message_id=? "
                            "AND delivery_lane='qq_account'", (message_id,))
        if account:
            from companion import account_actions

            if settings is None or clock is None:
                raise CoreError("SOCIAL_LIVE_SCOPE_REQUIRED", 403)
            row = store.one("SELECT authority_json FROM outbox WHERE message_id=?", (message_id,))
            approval = account_actions.scope(store, settings, clock,
                json.loads(row["authority_json"]), settings.social_autonomy.authorization_sha256,
                message_id=message_id)
            account_actions.lease_receipt(store, settings, clock, message_id, token, approval)
            now = clock()
        elif settings is not None and owner_authority.required(store, settings):
            if clock is None:
                raise CoreError("OWNER_DELIVERY_CLOCK_REQUIRED", 503)
            owner_delivery.check_delivery(store, settings, clock, message_id)
            now = clock()
        elif settings is not None and live_text.active(store, settings):
            live_text.check_delivery(store, settings, clock, message_id)
            now = clock()
        pending = store.one(
            "SELECT message_id,action_type,reply_group_id,segment_index FROM outbox "
            "WHERE message_id=? AND state='LEASED' "
            "AND lease_token=? AND lease_until>?",
            (message_id, token, now),
        )
        if pending:
            from companion.social import proactive_allowed, share_delivery_problem

            problem = share_delivery_problem(store, settings, message_id, now)
            if not problem and pending["reply_group_id"]:
                check_segment_ready(store, message_id, now)
            if pending["action_type"] == "qq.private.proactive" and not proactive_allowed(
                store, settings, now
            ):
                raise CoreError("PROACTIVE_SEND_PAUSED", 409)
        if problem:
            _cancel_blocked_delivery(store, pending, now, problem)
        else:
            changed = store.execute(
                "UPDATE outbox SET state='SENDING',attempt_count=attempt_count+1,updated_at=? "
                "WHERE message_id=? AND state='LEASED' AND lease_token=? AND lease_until>?",
                (now, message_id, token, now),
            ).rowcount
            if changed != 1:
                raise CoreError("STALE_DELIVERY_LEASE")
    if problem:
        raise CoreError(problem, 409)
    return {"state": "SENDING"}


def delivery_result(store, settings, message_id, result, now):
    with store.transaction():
        row = store.one(
            "SELECT * FROM outbox WHERE message_id=? AND lease_token=?",
            (message_id, result.lease_token),
        )
        if not row:
            raise CoreError("STALE_DELIVERY_LEASE")
        native = row["action_type"] == "qq.private.poke"
        account = row["delivery_lane"] == "qq_account"
        receipt = result.account_action_receipt
        if (account and (receipt is None or receipt.message_id != message_id
                        or receipt.action_type != row["action_type"])) or (
            not account and receipt is not None
        ):
            raise CoreError("SOCIAL_RECEIPT_REQUIRED", 400)
        acknowledgement = encode(result.native_ack.model_dump()) if result.native_ack else None
        if (native and result.result == "SENT" and (
            not result.native_ack or result.native_ack.action != "friend_poke"
            or result.provider_message_id is not None
        )) or (not native and result.native_ack is not None):
            raise CoreError("NATIVE_ACTION_ACK_REQUIRED", 400)
        if row["state"] == "SENT" and result.result == "SENT":
            if native and row["native_ack_json"] != acknowledgement:
                raise CoreError("NATIVE_ACTION_ACK_CONFLICT", 409)
            if account:
                from companion.social_autonomy import SocialAutonomy

                SocialAutonomy(store, settings, clock=lambda: now).record_result(receipt)
            return {"state": "SENT"}
        if account and row["state"] == "FAILED" and result.result == "PERMANENT_FAILURE":
            from companion.social_autonomy import SocialAutonomy

            if row["last_error_code"] != result.error_code:
                raise CoreError("SOCIAL_RECEIPT_CONFLICT", 409)
            SocialAutonomy(store, settings, clock=lambda: now).record_result(receipt)
            return {"state": "FAILED"}
        if row["state"] not in {"LEASED", "SENDING", "DELIVERY_UNCERTAIN"}:
            raise CoreError("INVALID_DELIVERY_TRANSITION")
        if result.result == "SENT" and row["state"] == "LEASED":
            raise CoreError("SEND_NOT_STARTED")
        if (row.get("reply_group_id") and row["state"] == "SENDING"
            and datetime.fromisoformat(row["lease_until"]) <= datetime.fromisoformat(now)):
            # A late ACK may establish this part's delivery, but cannot resume an expired chain.
            cancel_segment_tail(store, row, now, reason="REPLY_SEGMENT_ACK_LATE")
        if result.result == "RETRYABLE_FAILURE":
            if row["state"] != "LEASED":
                # Once I/O starts, a generic failure cannot establish non-delivery.
                state = "DELIVERY_UNCERTAIN"
            else:
                state = "RETRY_WAIT" if row["attempt_count"] < settings.max_attempts else "FAILED"
        else:
            state = {
                "SENT": "SENT",
                "DELIVERY_UNCERTAIN": "DELIVERY_UNCERTAIN",
                "PERMANENT_FAILURE": "FAILED",
            }[result.result]
        if native and row["state"] != "LEASED" and state == "FAILED":
            state = "DELIVERY_UNCERTAIN"
        from companion import cumulative

        if state in {"SENT", "FAILED", "DELIVERY_UNCERTAIN"}:
            cumulative.send_result(store, message_id, state, now)
        store.execute(
            "UPDATE outbox SET state=?,provider_message_id=?,native_ack_json=?,last_error_code=?,"
            "sent_at=?,updated_at=?,next_attempt_at=?,attempt_count=attempt_count+? "
            "WHERE message_id=?",
            (
                state,
                result.provider_message_id,
                acknowledgement,
                result.error_code,
                now if state == "SENT" else None,
                now,
                after(now, 5),
                int(row["state"] == "LEASED"),
                message_id,
            ),
        )
        store.execute("DELETE FROM runtime_meta WHERE key=?",
                      ("owner_preflight_lease:" + message_id,))
        store.audit("delivery.result", message_id, "delivery", now, state=state)
        if account:
            from companion.social_autonomy import SocialAutonomy

            SocialAutonomy(store, settings, clock=lambda: now).record_result(receipt)
        if row.get("reply_group_id"):
            if state == "SENT":
                store.execute(
                    "UPDATE outbox SET next_attempt_at=? WHERE reply_group_id=? "
                    "AND segment_index=? AND state IN ('PENDING','RETRY_WAIT')",
                    (after(now, row["segment_interval_ms"] / 1000), row["reply_group_id"],
                     row["segment_index"] + 1),
                )
            elif state in {"FAILED", "DELIVERY_UNCERTAIN"}:
                cancel_segment_tail(store, row, now)
        from companion.goals import acknowledge_commitment_delivery

        acknowledge_commitment_delivery(store, message_id, state, now)
        return {"state": state}


def reconcile_delivery(store, message_id, outcome, now, settings=None, *,
                       operation_id=None, operation_sha256=None):
    if settings is not None:
        from companion import delivery_reconciliation

        return delivery_reconciliation.resolve(store, settings, message_id, outcome, now,
            operation_id=operation_id, operation_sha256=operation_sha256)
    if outcome not in {"SENT", "CANCELLED"}:
        raise CoreError("INVALID_RECONCILIATION", 400)
    with store.transaction():
        changed = store.execute(
            "UPDATE outbox SET state=?,updated_at=?,sent_at=?,lease_token=NULL "
            "WHERE message_id=? AND state='DELIVERY_UNCERTAIN' AND action_type IN "
            "('qq.private.reply','qq.private.proactive','qq.private.poke')",
            (outcome, now, now if outcome == "SENT" else None, message_id),
        ).rowcount
        if changed != 1:
            raise CoreError("NOT_UNCERTAIN")
        store.audit("delivery.owner_reconciled", message_id, "owner", now, outcome=outcome)
        from companion.goals import acknowledge_commitment_delivery

        acknowledge_commitment_delivery(store, message_id, outcome, now)
        return {"state": outcome}
