"""Private, bounded voice custody. Native source and Outbox authority stay in Core."""

import hashlib
import hmac
import json
import secrets
import uuid
from contextlib import nullcontext
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, field_validator

from companion import paid_budget, private_life, reply_revision, voice_authority
from companion.contracts import CoreError
from companion.privacy import epoch
from companion.qq_rich import input_digest
from companion.store import after, encode, uid, utcnow
from companion.voice_protocol import (
    Digest,
    SingingAudio,
    SingingResult,
    VoiceAudioResult,
    VoiceClaimResponse,
    VoiceControlResponse,
    VoiceDTO,
    VoiceLease,
    VoiceResultReceipt,
    VoiceSession,
    VoiceStopResponse,
    VoiceTranscriptResult,
    canonical_bytes,
    digest,
    sign_voice_lease,
)
from companion_delivery_protocol.authority import validate_authority_ref
from companion_delivery_protocol.singing import MAX_RESULT_BODY_BYTES as SONG_RESERVATION
from companion_voice_node.providers import LocalVoiceError, WAVLimits, validate_wav

UPLOAD_DOMAIN = b"companion-core/onebot-owner-voice/v2\x00"
ACTIVE = {"QUEUED", "LEASED", "RUNNING"}
COMPLETED = {"READY", "TRANSCRIBED"}
REPLY_RECEIPT = "voice_reply:"
INPUT_RECEIPT = "voice_input:"


class VoiceASRProof(VoiceDTO):
    """Capture-key attestation binding one native record segment to uploaded WAV bytes."""

    authority: dict

    @field_validator("authority")
    @classmethod
    def authority_ref(cls, value):
        return validate_authority_ref(value)

    event_id: str = Field(min_length=1, max_length=128)
    source_event_id: str = Field(min_length=1, max_length=256)
    source_input_sha256: Digest
    source_segment_index: int = Field(ge=0, le=63, strict=True)
    audio_sha256: Digest
    audio_bytes: int = Field(ge=44, le=8_000_000, strict=True)
    source_partial: bool = Field(strict=True)
    signature: str = Field(pattern=r"^[a-f0-9]{128}$", repr=False)


def sign_asr_proof(private_key, **material):
    """Capture adapter signature over the existing canonical JSON and v2 owner reference."""
    unsigned = VoiceASRProof(**material, signature="0" * 128).model_dump(
        mode="json", exclude={"signature"}
    )
    return VoiceASRProof(
        **unsigned, signature=private_key.sign(UPLOAD_DOMAIN + canonical_bytes(unsigned)).hex()
    )


def _transaction_required(store):
    if not store.db.in_transaction:
        raise RuntimeError("VOICE_TRANSACTION_REQUIRED")


def incarnation(store):
    row = store.one("SELECT value FROM runtime_meta WHERE key='voice_incarnation'")
    if not row:
        raise CoreError("VOICE_UPGRADE_REQUIRED", 503)
    return row["value"]


def _retire_content(store, row):
    """Keep only random-capability stop custody; never grant input/result authority."""
    store.execute(
        "UPDATE voice_jobs SET input_blob=NULL,result_blob=NULL,input_sha256=NULL,"
        "result_sha256=NULL,source_sha256=NULL,source_input_sha256=NULL,source_proof_sha256=NULL,"
        "outbox_content_sha256=NULL,identity_sha256=NULL,window_sha256=NULL,manifest_sha256=NULL,"
        "config_sha256=NULL,config_json='{}',authority_json='{}',input_bytes=NULL,input_duration_seconds=NULL,"
        "result_bytes=NULL,lease_json=NULL,lease_token_sha256=NULL,content_retired=1,"
        "singing_context_json='{}',reserved_output_bytes=0,"
        "cleanup_epoch=?,cleanup_state=CASE WHEN stop_token_sha256 IS NULL "
        "OR cleanup_state='CONFIRMED' THEN 'CONFIRMED' ELSE 'PENDING' END WHERE job_id=?",
        (epoch(store), row["job_id"]),
    )
    store.execute("UPDATE voice_node_state SET claim_request_sha256=NULL,"
                  "claim_response_json=NULL,claim_expires_at=NULL,claim_job_id=NULL "
                  "WHERE claim_job_id=?", (row["job_id"],))
    store.execute("UPDATE audit_log SET safe_detail_json=json_remove(safe_detail_json,"
                  "'$.input_sha256','$.result_sha256') WHERE entity_id=? "
                  "AND action IN ('voice.created','voice.committed')", (row["job_id"],))


def _fence(store, row, reason, now, *, expired=False):
    # Input release is the dispatch boundary. An absent result cannot prove no GPU work.
    state = row["state"]
    if state in ACTIVE | COMPLETED:
        state = (
            "UNKNOWN"
            if row["started_at"] and state in ACTIVE
            else "EXPIRED"
            if expired
            else "CANCELLED"
        )
    store.execute(
        "UPDATE voice_jobs SET state=?,input_blob=NULL,result_blob=NULL,"
        "updated_at=?,last_error_code=? WHERE job_id=?",
        (state, now, reason, row["job_id"]),
    )
    if state != row["state"] or row["input_blob"] is not None or row["result_blob"] is not None:
        store.audit(
            "voice.fenced",
            row["job_id"],
            row["event_id"],
            now,
            state=state,
            reason=reason,
            node_id=row["node_id"],
        )
    _retire_content(store, row)
    if row["operation"] == "sing" and not row["started_at"]:
        store.execute("UPDATE voice_jobs SET source_reserved_requests=0,source_reserved_bytes=0 "
                      "WHERE job_id=?", (row["job_id"],))
    if row["operation"] == "sing":
        _invalidate_flow(store, row, reason)
    if not row["started_at"]:
        store.execute(
            "UPDATE voice_node_state SET active_job_id=NULL WHERE active_job_id=?",
            (row["job_id"],),
        )
    # Preserve content-free WAITING/TEXT choice state so an unattempted reply
    # can still take its existing text fallback. Frozen audio loses its content.
    reply = store.one("SELECT value FROM runtime_meta WHERE key=?",
                      (REPLY_RECEIPT + str(row["message_id"]),))
    try:
        value = json.loads(reply["value"]) if reply else {}
        has_binding = isinstance(value, dict) and (value.get("binding") or value.get("retired"))
    except (ValueError, TypeError):
        has_binding = True
    if has_binding:
        _invalidate_receipts(store, "key=?", (REPLY_RECEIPT + str(row["message_id"]),), reason)
    _invalidate_receipts(store, "key=?", (INPUT_RECEIPT + row["event_id"],), reason)


def expire_voice_jobs(store, now):
    """Existing Core recovery can retire expired content without authorizing execution."""
    _transaction_required(store)
    rows = store.rows("SELECT * FROM voice_jobs WHERE content_retired=0 AND "
        "(deadline_at<=? OR result_expires_at<=? OR "
        "(state IN ('LEASED','RUNNING') AND lease_until<=?)) ORDER BY deadline_at LIMIT 1000",
        (now, now, now))
    for row in rows:
        _fence(store, row, "VOICE_EXPIRED", now, expired=True)
    store.execute("UPDATE voice_node_state SET claim_response_json=NULL,claim_request_sha256=NULL,"
                  "claim_job_id=NULL,claim_expires_at=NULL WHERE claim_expires_at<=?", (now,))
    return len(rows)


def erase_voice_jobs(store, event_ids):
    _transaction_required(store)
    for event_id in set(event_ids):
        for row in store.rows(
            "SELECT DISTINCT v.* FROM voice_jobs v LEFT JOIN jobs j ON j.event_id=v.event_id "
            "LEFT JOIN job_sources s ON s.job_id=j.job_id WHERE v.event_id=? OR s.event_id=?",
            (event_id, event_id),
        ):
            _fence(store, row, "VOICE_SOURCE_ERASED", utcnow())
            _invalidate_flow(store, row, "VOICE_SOURCE_ERASED")
        store.execute(
            "UPDATE runtime_meta SET value=? WHERE key=?",
            (
                encode({"state": "FENCED", "reason": "VOICE_SOURCE_ERASED"}),
                INPUT_RECEIPT + event_id,
            ),
        )
        _invalidate_receipts(store,
            "key IN "
            "(SELECT ? || message_id FROM outbox WHERE origin_event_id=?)",
            (REPLY_RECEIPT, event_id), "VOICE_SOURCE_ERASED",
        )


def _invalidate_flow(store, row, reason):
    _invalidate_receipts(store, "key IN (?,?)",
                         (REPLY_RECEIPT + str(row["message_id"]), INPUT_RECEIPT + row["event_id"]),
                         reason)
    if row["operation"] == "sing":
        _invalidate_receipts(store, "key LIKE 'voice_reply:%' AND "
            "json_extract(value,'$.job_id')=?", (row["job_id"],), reason)


def retired_delivery_binding(binding, *, legacy_signature=None):
    """A prior-message verifier only; this can never construct a send envelope."""
    token = binding.get("retirement_token")
    if legacy_signature is None and not token:
        raise CoreError("VOICE_LEGACY_PROOF_REQUIRED", 409)
    verifier = hashlib.sha256(bytes.fromhex(legacy_signature) if legacy_signature
                              else token.encode()).hexdigest()
    scope = {"authority": binding["authority"]} if "authority" in binding else {
        "window_sha256": binding["window_sha256"]}
    return {"kind": "legacy_signature" if legacy_signature else "capability", **scope,
            "verifier": verifier, **{key: binding[key] for key in (
                "job_id", "generation", "message_id", "event_id", "delivery_key",
                "voice_policy_sha256", "reply_revision")}}


def _invalidate_receipts(store, where, parameters, reason):
    from companion.voice_protocol import (
        LegacyVoiceDeliveryBinding,
        VoiceDeliveryBinding,
        VoiceRetiredBinding,
    )

    for row in store.rows("SELECT key,value FROM runtime_meta WHERE " + where, parameters):
        value = {"state": "FENCED", "reason": reason}
        if row["key"].startswith(REPLY_RECEIPT):
            try:
                previous = json.loads(row["value"])
                if not isinstance(previous, dict):
                    previous = {}
            except (ValueError, TypeError):
                previous = {}
            if previous.get("retired"):
                try:
                    proof = VoiceRetiredBinding.model_validate(previous["retired"])
                    original = store.one(
                        "SELECT v.generation,v.event_id,o.message_id,o.delivery_key "
                        "FROM voice_jobs v JOIN outbox o ON o.message_id=? WHERE v.job_id=? "
                        "AND o.origin_event_id=v.event_id",
                        (proof.message_id, proof.job_id))
                    if (original and previous.get("job_id") == proof.job_id
                        and row["key"] == REPLY_RECEIPT + proof.message_id
                        and all(original[k] == getattr(proof, k) for k in (
                            "generation", "event_id", "message_id", "delivery_key"))):
                        value.update(job_id=proof.job_id, retired=proof.model_dump(mode="json"))
                except (ValueError, TypeError):
                    pass
            else:
                try:
                    raw = previous.get("binding")
                    codec = (LegacyVoiceDeliveryBinding if isinstance(raw, dict)
                             and raw.get("schema_version") == 2 else VoiceDeliveryBinding)
                    binding = codec.model_validate(raw)
                    if (previous.get("state") in {"AUDIO", "FENCED"}
                        and previous.get("binding_sha256") == digest(binding)
                        and previous.get("job_id") == binding.job_id
                        and row["key"] == REPLY_RECEIPT + binding.message_id):
                        value.update(job_id=binding.job_id,
                                     retired=retired_delivery_binding(binding.model_dump(mode="json")))
                except (ValueError, TypeError, AttributeError):
                    pass
        store.execute("UPDATE runtime_meta SET value=? WHERE key=?", (encode(value), row["key"]))


def restore_voice_jobs(store):
    _transaction_required(store)
    store.execute(
        "UPDATE runtime_meta SET value=? WHERE key='voice_incarnation'", (uuid.uuid4().hex,)
    )
    store.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('voice_restore_required','1')")
    store.execute("UPDATE voice_node_state SET recovery_required=1,resource_state='QUARANTINED',"
                  "last_claim_seq=0,claim_request_sha256=NULL,claim_response_json=NULL,"
                  "claim_expires_at=NULL,claim_job_id=NULL,recovered_incarnation=NULL")
    for row in store.rows("SELECT * FROM voice_jobs"):
        _fence(store, row, "VOICE_RESTORE_FENCED", utcnow())
    _invalidate_receipts(store,
        "key LIKE 'voice_reply:%' "
        "OR key LIKE 'voice_input:%'",
        (), "VOICE_RESTORE_FENCED",
    )


def fence_conversation(store, conversation_id, reason="REPLY_SUPERSEDED"):
    _transaction_required(store)
    for row in store.rows("SELECT * FROM voice_jobs WHERE conversation_id=?", (conversation_id,)):
        _fence(store, row, reason, utcnow())
        _invalidate_flow(store, row, reason)
    _invalidate_receipts(store,
        "key IN "
        "(SELECT ? || event_id FROM events WHERE conversation_id=? UNION "
        "SELECT ? || message_id FROM outbox WHERE conversation_id=?)",
        (
            INPUT_RECEIPT,
            conversation_id,
            REPLY_RECEIPT,
            conversation_id,
        ), reason,
    )


class VoiceJobs:
    def __init__(self, store, settings, clock=utcnow):
        self.store, self.settings, self.clock = store, settings, clock

    def _transaction(self):
        return nullcontext() if self.store.db.in_transaction else self.store.transaction()

    def _node(self, node_id=None, operation=None):
        config = self.settings.voice
        voice_authority.current(self.store, self.settings, self.clock)
        if node_id is None:
            node_id = next(
                (
                    key
                    for key, node in sorted(config.nodes.items())
                    if node.enabled and (operation is None or operation in node.capabilities)
                ),
                None,
            )
        node = config.nodes.get(node_id)
        if not node or not node.enabled or (operation and operation not in node.capabilities):
            raise CoreError("VOICE_NODE_SCOPE_REQUIRED", 403)
        return node_id, node

    def _tts_source(self, message_id, text=None, *, delivery_token=None, retained_ready=False):
        row = voice_authority.delivery(self.store, self.settings, self.clock, message_id)
        reply_revision.check_delivery(self.store, message_id)
        content = json.loads(row["content_json"])
        valid_stage = row["state"] == "PENDING" and row["attempt_count"] == 0
        if retained_ready and row["state"] in {"PENDING", "RETRY_WAIT", "LEASED"}:
            # This preserves an already selected result, never authorizes new TTS or a send.
            valid_stage = True
        if delivery_token is not None:
            valid_stage = (
                row["state"] in {"LEASED", "SENDING"}
                and row["lease_token"] == delivery_token
                and row["lease_until"] is not None
                and datetime.fromisoformat(row["lease_until"])
                > datetime.fromisoformat(self.clock())
            )
        if (
            not valid_stage
            or row["action_type"] not in {"qq.private.reply", "qq.private.proactive"}
            or not isinstance(content.get("text"), str)
            or (text is not None and content["text"] != text)
        ):
            raise CoreError("VOICE_OUTBOX_SOURCE_REQUIRED", 403)
        event = self.store.one("SELECT * FROM events WHERE event_id=?", (row["origin_event_id"],))
        if not event or not reply_revision.source_active(self.store, event["event_id"]):
            raise CoreError("VOICE_SOURCE_CHANGED", 403)
        if event["source"] == "qq.onebot":
            voice_authority.source(self.store, self.settings, self.clock, event["event_id"],
                                   allow_commands=True)
        return event, row

    def _asr_source(self, event_id, segment_index):
        _, event, body, job = voice_authority.source(
            self.store, self.settings, self.clock, event_id)
        reply_revision.require_current(self.store, event_id)
        reply_revision.check_job(self.store, job)
        segment = next(
            (
                item
                for item in (body.get("rich") or {}).get("segments", [])
                if item.get("index") == segment_index
            ),
            None,
        )
        if segment != {
            "index": segment_index,
            "type": "record",
            "actor": "UNVERIFIED",
            "coverage": "UNSUPPORTED",
        }:
            raise CoreError("VOICE_RECORD_SOURCE_REQUIRED", 403)
        return event, body

    def _base(self, event, node_id, operation, data, mime, **extra):
        now, config = self.clock(), self.settings.voice
        revision = self.store.one(
            "SELECT reply_revision,scope,owner_actor_id FROM conversations WHERE conversation_id=?",
            (event["conversation_id"],),
        )
        if (
            not revision
            or revision["scope"] != "private"
            or revision["owner_actor_id"] != "owner"
            or event["privacy_scope"] != "private"
        ):
            raise CoreError("VOICE_PRIVATE_SOURCE_REQUIRED", 403)
        view = voice_authority.current(self.store, self.settings, self.clock)
        deadline = voice_authority.deadline(view, after(now, config.job_ttl_seconds))
        return dict(
            job_id=uid("voice"),
            operation=operation,
            event_id=event["event_id"],
            native_source_event_id=event["source_event_id"],
            conversation_id=event["conversation_id"],
            source_sha256=private_life.event_digest(event),
            identity_sha256=paid_budget.sha([self.settings.owner_qq_id, self.settings.bot_qq_id]),
            window_sha256=None,
            authority_json=encode(view.authority.as_dict()),
            captured_privacy_epoch=epoch(self.store),
            captured_reply_revision=revision["reply_revision"],
            node_id=node_id,
            manifest_sha256=config.nodes[node_id].manifest_sha256,
            config_sha256=config.node_fingerprint(node_id),
            config_json=encode(config.node_snapshot(node_id)),
            state="QUEUED",
            incarnation=incarnation(self.store),
            cleanup_epoch=epoch(self.store),
            generation=1,
            input_mime=mime,
            input_sha256=hashlib.sha256(data).hexdigest(),
            input_bytes=len(data),
            input_blob=data,
            source_partial=0,
            created_at=now,
            updated_at=now,
            deadline_at=deadline,
            **extra,
        )

    def _capacity(self, additional_bytes, *, new_job=False, replace_job_id=None):
        used = self.store.one(
            "SELECT COALESCE(SUM(COALESCE(length(input_blob),0)+"
            "MAX(COALESCE(length(result_blob),0),reserved_output_bytes)),0) AS bytes,"
            "SUM(state IN ('QUEUED','LEASED','RUNNING')) AS pending FROM voice_jobs "
            "WHERE job_id IS NOT ?", (replace_job_id,)
        )
        if used["bytes"] + additional_bytes > self.settings.voice.max_private_blob_bytes or (
            new_job and (used["pending"] or 0) >= self.settings.voice.max_pending_jobs
        ):
            raise CoreError("VOICE_CAPACITY_EXCEEDED", 429)

    def create_sing(self, job, source_event_id, part, tool_call_id):
        from companion.singing import prepare_context, reserve_read

        _transaction_required(self.store)
        existing = self.store.one("SELECT * FROM voice_jobs WHERE event_id=? AND operation='sing'",
                                  (job["event_id"],))
        if existing:
            # A retired/unknown original cannot be used as a new singing attempt.
            raise CoreError("SINGING_ORIGINAL_ATTEMPT_EXISTS", 409)
        context = prepare_context(self.store, self.settings, self.clock, job,
                                   source_event_id, part, tool_call_id)
        data = canonical_bytes(context["recipe"])
        node_id, _ = self._node(operation="sing")
        if self.store.one("SELECT 1 FROM voice_jobs WHERE operation='sing' "
                          "AND state IN ('QUEUED','LEASED','RUNNING')"):
            raise CoreError("VOICE_CAPACITY_EXCEEDED", 429)
        self._capacity(len(data) + SONG_RESERVATION, new_job=True)
        read_budget = reserve_read(self.store, self.settings, self.clock(),
                                   context["recipe"]["max_input_bytes"])
        event = self.store.one("SELECT * FROM events WHERE event_id=?", (job["event_id"],))
        row = self._base(event, node_id, "sing", data, "application/json",
            singing_context_json=encode(context), source_proof_sha256=digest(context),
            reserved_output_bytes=SONG_RESERVATION, **read_budget)
        self.store.insert("voice_jobs", **row)
        self.store.audit("voice.created", row["job_id"], event["event_id"], row["created_at"],
                         operation="sing", node_id=node_id, input_sha256=row["input_sha256"])
        return self._public(row)

    def create_tts(self, message_id, text, *, node_id=None):
        _transaction_required(self.store)
        existing = self.store.one("SELECT * FROM voice_jobs WHERE message_id=?", (message_id,))
        if existing:
            if existing["content_retired"]:
                return self.status(existing["job_id"])
            if (
                not isinstance(text, str)
                or hashlib.sha256(text.encode()).hexdigest() != (existing["input_sha256"])
                or (node_id is not None and node_id != existing["node_id"])
            ):
                raise CoreError("VOICE_IDEMPOTENCY_CONFLICT", 409)
            return self.status(existing["job_id"])
        node_id, node = self._node(node_id, "tts")
        if not isinstance(text, str) or not text.strip() or len(text) > node.max_tts_chars:
            raise CoreError("VOICE_TTS_TEXT_INVALID", 400)
        event, outbox = self._tts_source(message_id, text)
        data = text.encode("utf-8")
        self._capacity(len(data), new_job=True)
        row = self._base(
            event,
            node_id,
            "tts",
            data,
            "text/plain",
            message_id=message_id,
            outbox_content_sha256=outbox["content_sha256"],
            outbox_delivery_key=outbox["delivery_key"],
        )
        self.store.insert("voice_jobs", **row)
        self.store.audit(
            "voice.created",
            row["job_id"],
            event["event_id"],
            row["created_at"],
            operation="tts",
            node_id=node_id,
            input_sha256=row["input_sha256"],
        )
        return self._public(row)

    def create_asr(
        self,
        owner_event_id,
        wavbytes,
        *,
        source_segment_index,
        upload_proof,
        source_partial=False,
        node_id=None,
    ):
        _transaction_required(self.store)
        if (
            type(source_segment_index) is not int
            or not 0 <= source_segment_index <= 63
            or (type(source_partial) is not bool)
        ):
            raise CoreError("VOICE_ASR_SOURCE_INVALID", 400)
        node_id, node = self._node(node_id, "asr")
        event, body = self._asr_source(owner_event_id, source_segment_index)
        try:
            audio = validate_wav(
                wavbytes,
                WAVLimits(
                    max_bytes=node.max_asr_input_bytes,
                    max_duration_seconds=node.max_asr_seconds,
                ),
            )
            proof = VoiceASRProof.model_validate(upload_proof)
            expected = dict(
                authority=voice_authority.current(
                    self.store, self.settings, self.clock).authority.as_dict(),
                event_id=owner_event_id,
                source_event_id=event["source_event_id"],
                source_input_sha256=input_digest(body),
                source_segment_index=source_segment_index,
                audio_sha256=audio.sha256,
                audio_bytes=len(wavbytes),
                source_partial=source_partial,
            )
            if proof.model_dump(mode="json", exclude={"signature"}) != expected:
                raise ValueError()
            Ed25519PublicKey.from_public_bytes(
                bytes.fromhex(voice_authority.current(
                    self.store, self.settings, self.clock).policy.capture.public_key)
            ).verify(bytes.fromhex(proof.signature), UPLOAD_DOMAIN + canonical_bytes(expected))
        except (ValueError, TypeError, InvalidSignature, LocalVoiceError):
            raise CoreError("VOICE_ASR_UPLOAD_PROOF_REQUIRED", 403) from None
        existing = self.store.one(
            "SELECT * FROM voice_jobs WHERE event_id=? AND source_segment_index=?",
            (owner_event_id, source_segment_index),
        )
        if existing:
            if existing["content_retired"]:
                raise CoreError("VOICE_JOB_RETIRED", 410)
            if (existing["input_sha256"], existing["source_proof_sha256"], existing["node_id"]) != (
                audio.sha256,
                digest(proof),
                node_id,
            ):
                raise CoreError("VOICE_IDEMPOTENCY_CONFLICT", 409)
            return self.status(existing["job_id"])
        self._capacity(len(wavbytes), new_job=True)
        row = self._base(
            event,
            node_id,
            "asr",
            wavbytes,
            "audio/wav",
            source_segment_index=source_segment_index,
            source_input_sha256=input_digest(body),
            source_proof_sha256=digest(proof),
            input_duration_seconds=audio.duration_seconds,
        )
        row["source_partial"] = int(source_partial)
        self.store.insert("voice_jobs", **row)
        self.store.audit(
            "voice.created",
            row["job_id"],
            event["event_id"],
            row["created_at"],
            operation="asr",
            node_id=node_id,
            input_sha256=row["input_sha256"],
        )
        return self._public(row)

    def _delivery_binding(self, row, message_id=None):
        """A selected result may survive Outbox leasing; this never grants execution."""
        from companion.voice_protocol import VoiceDeliveryBinding

        message_id = message_id or row["message_id"]
        outbox = self.store.one("SELECT * FROM outbox WHERE message_id=?", (message_id,))
        receipt = self.store.one("SELECT value FROM runtime_meta WHERE key=?",
                                  (REPLY_RECEIPT + str(message_id),))
        try:
            value = json.loads(receipt["value"]) if receipt else {}
            binding = VoiceDeliveryBinding.model_validate(value["binding"]).model_dump(mode="json")
            expected = {
                "job_id": row["job_id"],
                "generation": row["generation"],
                "message_id": message_id,
                "result_sha256": row["result_sha256"],
                "content_sha256": (outbox["content_sha256"] if row["operation"] == "sing"
                                   else row["outbox_content_sha256"]),
                "delivery_key": (outbox["delivery_key"] if row["operation"] == "sing"
                                 else row["outbox_delivery_key"]),
                "source_sha256": row["source_sha256"],
                "authority": voice_authority.saved(row),
                "identity_sha256": row["identity_sha256"],
                "privacy_epoch": row["captured_privacy_epoch"],
                "reply_revision": row["captured_reply_revision"],
                "node_config_sha256": row["config_sha256"],
                "voice_policy_sha256": self.settings.voice.authority_sha256(),
                "event_id": row["event_id"],
                "node_id": row["node_id"],
                "manifest_sha256": row["manifest_sha256"],
            }
            if row["operation"] == "sing":
                if not outbox or outbox["origin_event_id"] != row["event_id"]:
                    return None
                content = json.loads(outbox["content_json"])
                choice = content.get("reply_part")
                if choice and (choice.get("kind") != "singing"
                               or choice.get("request_id") != row["job_id"]):
                    return None
                expected["singing_segment_index"] = (choice["singing_segment_index"]
                    if choice and choice["kind"] == "singing" else outbox["segment_index"] or 0)
            if (
                value["state"] != "AUDIO"
                or value["job_id"] != row["job_id"]
                or value["binding_sha256"] != digest(binding)
                or any(binding.get(key) != item for key, item in expected.items())
                or datetime.fromisoformat(binding["expires_at"])
                != datetime.fromisoformat(row["result_expires_at"])
            ):
                return None
            return binding
        except (ValueError, KeyError, TypeError):
            return None

    def _authority(self, row, *, delivery_token=None):
        node_id, _ = self._node(row["node_id"], row["operation"])
        voice_authority.current(self.store, self.settings, self.clock,
                                authority=voice_authority.saved(row))
        if (
            row["config_sha256"] != self.settings.voice.node_fingerprint(node_id)
            or row["captured_privacy_epoch"] != epoch(self.store)
            or row["identity_sha256"]
            != paid_budget.sha([self.settings.owner_qq_id, self.settings.bot_qq_id])
        ):
            raise CoreError("VOICE_AUTHORITY_CHANGED", 403)
        if row["operation"] == "tts":
            binding = self._delivery_binding(row) if row["state"] == "READY" else None
            if delivery_token is None and binding:
                sending = self.store.one(
                    "SELECT lease_token FROM outbox WHERE message_id=? "
                    "AND state='SENDING'",
                    (row["message_id"],),
                )
                if sending:
                    delivery_token = sending["lease_token"]
            event, outbox = self._tts_source(row["message_id"], delivery_token=delivery_token,
                retained_ready=bool(binding and delivery_token is None))
            if (outbox["content_sha256"], outbox["delivery_key"]) != (
                row["outbox_content_sha256"],
                row["outbox_delivery_key"],
            ) or binding and binding["reply_sha256"] != paid_budget.sha(
                json.loads(outbox["content_json"])["text"]
            ):
                raise CoreError("VOICE_OUTBOX_SOURCE_CHANGED", 403)
        elif row["operation"] == "sing":
            from companion.singing import check_source

            event, _ = check_source(self.store, self.settings, self.clock, row)
        else:
            event, body = self._asr_source(row["event_id"], row["source_segment_index"])
            if input_digest(body) != row["source_input_sha256"]:
                raise CoreError("VOICE_SOURCE_CHANGED", 403)
        current = self.store.one(
            "SELECT reply_revision FROM conversations WHERE conversation_id=?",
            (row["conversation_id"],),
        )
        if not current or current["reply_revision"] != row["captured_reply_revision"]:
            raise CoreError("REPLY_SUPERSEDED", 409)
        if private_life.event_digest(event) != row["source_sha256"]:
            raise CoreError("VOICE_SOURCE_CHANGED", 403)

    def _reconcile(self, row):
        if row["state"] not in ACTIVE | COMPLETED:
            return row
        now = self.clock()
        expired = any(
            value and datetime.fromisoformat(value) <= datetime.fromisoformat(now)
            for value in (
                row["deadline_at"],
                row["lease_until"] if row["state"] in ACTIVE else None,
                row["result_expires_at"],
            )
        )
        reason = "VOICE_EXPIRED" if expired else None
        if not reason:
            try:
                self._authority(row)
            except CoreError as error:
                reason = error.code
        if reason:
            _fence(self.store, row, reason, now, expired=expired)
            return self._get(row["job_id"])
        return row

    def _get(self, job_id):
        row = self.store.one("SELECT * FROM voice_jobs WHERE job_id=?", (job_id,))
        if row is None:
            raise CoreError("VOICE_JOB_NOT_FOUND", 404)
        return row

    @staticmethod
    def _public(row):
        return {
            key: row.get(key)
            for key in (
                "job_id",
                "operation",
                "state",
                "generation",
                "node_id",
                "event_id",
                "message_id",
                "source_segment_index",
                "deadline_at",
                "result_expires_at",
                "last_error_code",
                "content_retired", "cleanup_state", "cleanup_epoch",
            )
        } | {"execution": "local", "billed_micro_usd": 0}

    def status(self, job_id):
        with self._transaction():
            return self._public(self._reconcile(self._get(job_id)))

    def result(self, job_id):
        with self._transaction():
            row = self._reconcile(self._get(job_id))
            if row["state"] not in COMPLETED or row["result_blob"] is None:
                return None
            if row["operation"] == "tts" and not self.store.one(
                "SELECT 1 FROM outbox WHERE message_id=? AND state='PENDING' AND attempt_count=0",
                (row["message_id"],),
            ):
                return None
            model = {"tts": VoiceAudioResult, "asr": VoiceTranscriptResult,
                     "sing": SingingResult}[row["operation"]]
            return model.model_validate_json(row["result_blob"])

    def singing_manifest(self, job_id):
        from companion_delivery_protocol.singing import validate_manifest

        with self._transaction():
            row = self._reconcile(self._get(job_id))
            if row["operation"] != "sing" or row["state"] != "READY" or row["result_blob"] is None:
                return None
            compact = self.store.one("SELECT json_extract(result_blob,'$.manifest') AS manifest "
                                      "FROM voice_jobs WHERE job_id=?", (job_id,))
            value = validate_manifest(json.loads(compact["manifest"]))
            if digest(value) != row["result_sha256"]:
                raise CoreError("SINGING_RESULT_CHANGED", 409)
            return value

    def delivery_audio(self, job_id, message_id, lease_token):
        """Read only the selected, completed audio bound to this live Outbox lease."""
        _transaction_required(self.store)
        if not isinstance(lease_token, str) or not lease_token:
            raise CoreError("STALE_DELIVERY_LEASE", 409)
        row = self._reconcile(self._get(job_id))
        if row["operation"] == "sing":
            binding = self._delivery_binding(row, message_id)
            outbox = self.store.one("SELECT * FROM outbox WHERE message_id=? AND lease_token=? "
                "AND state IN ('LEASED','SENDING') AND lease_until>?",
                (message_id, lease_token, self.clock()))
            if not binding or not outbox or row["state"] != "READY" or row["result_blob"] is None:
                raise CoreError("SINGING_DELIVERY_FENCED", 409)
            manifest = self.singing_manifest(job_id)
            index = binding["singing_segment_index"]
            if (index >= len(manifest["segments"])
                or digest(manifest) != binding["result_sha256"]
                or binding["reply_sha256"] != paid_budget.sha(
                    json.loads(outbox["content_json"])["text"])):
                raise CoreError("SINGING_DELIVERY_FENCED", 409)
            from companion_delivery_protocol.singing import validate_part

            part = self.store.one("SELECT json_extract(result_blob,?) AS audio "
                "FROM voice_jobs WHERE job_id=?", (f"$.audio[{index}]", job_id))
            audio = SingingAudio.model_validate_json(part["audio"])
            validate_part(manifest, index, {"audio_bytes": len(audio.audio_bytes()),
                **audio.model_dump(exclude={"audio_base64"})})
            if audio.audio_sha256 != binding["audio_sha256"]:
                raise CoreError("SINGING_DELIVERY_FENCED", 409)
            return audio
        if (
            row["state"] != "READY"
            or row["message_id"] != message_id
            or row["result_blob"] is None
            or not self._delivery_binding(row)
        ):
            raise CoreError("VOICE_DELIVERY_FENCED", 409)
        self._authority(row, delivery_token=lease_token)
        return VoiceAudioResult.model_validate_json(row["result_blob"])

    def withdraw(self, job_id, reason="VOICE_FLOW_WITHDRAWN"):
        _transaction_required(self.store)
        row = self._get(job_id)
        _fence(self.store, row, reason, self.clock())
        return self._public(self._get(job_id))

    def untrusted_transcription(self, job_id):
        with self._transaction():
            row = self._reconcile(self._get(job_id))
            if row["state"] != "TRANSCRIBED" or row["result_blob"] is None:
                return None
            payload = VoiceTranscriptResult.model_validate_json(row["result_blob"])
            return {
                "kind": "untrusted_transcription",
                "text": payload.text,
                "source_event_id": row["event_id"],
                "segment_index": row["source_segment_index"],
                "audio_sha256": row["input_sha256"],
                "partial": payload.partial,
                "language": payload.language,
                "result_sha256": row["result_sha256"],
            }

    def _ref(self, node_id, ref):
        row = self._get(ref.job_id)
        if row["content_retired"] or row["incarnation"] != incarnation(self.store):
            raise CoreError("VOICE_LEASE_RETIRED", 410)
        if (
            row["node_id"] != node_id
            or row["generation"] != ref.generation
            or (row["input_sha256"], row["manifest_sha256"])
            != (ref.input_sha256, ref.manifest_sha256)
            or not hmac.compare_digest(
                row["lease_token_sha256"] or "",
                hashlib.sha256(ref.lease_token.encode()).hexdigest(),
            )
        ):
            raise CoreError("VOICE_LEASE_MISMATCH", 409)
        return row

    def session(self, node_id):
        if node_id not in self.settings.voice.nodes:
            raise CoreError("VOICE_NODE_SCOPE_REQUIRED", 403)
        row = self.store.one("SELECT * FROM voice_node_state WHERE node_id=?", (node_id,))
        current = incarnation(self.store)
        if row and row["claim_response_json"] and row["claim_expires_at"] <= self.clock():
            with self._transaction():
                self.store.execute(
                    "UPDATE voice_node_state SET claim_response_json=NULL,"
                    "claim_request_sha256=NULL,claim_expires_at=NULL,claim_job_id=NULL "
                    "WHERE node_id=? AND claim_expires_at<=?",
                    (node_id, self.clock()),
                )
            row = self.store.one("SELECT * FROM voice_node_state WHERE node_id=?", (node_id,))
        restored = self.store.one("SELECT 1 FROM runtime_meta WHERE key='voice_restore_required'")
        return VoiceSession(incarnation=current, last_sequence=row["last_claim_seq"] if row else 0,
            replay_available=bool(row and row["claim_response_json"]
                                  and row["claim_expires_at"] > self.clock()),
            recovery_required=bool(row and row["recovery_required"] or restored
                and (not row or row["recovered_incarnation"] != current)))

    def recover_node(self, node_id, request):
        """Owner-control-only confirmation of stopped execution, never a new dispatch."""
        with self._transaction():
            if request.incarnation != incarnation(self.store):
                raise CoreError("VOICE_CLAIM_EPOCH_RETIRED", 410)
            if not request.operator.strip() or not request.service_instance.strip():
                raise CoreError("VOICE_RECOVERY_CONFIRMATION_REQUIRED", 400)
            self.session(node_id)
            now = self.clock()
            for row in self.store.rows(
                "SELECT * FROM voice_jobs WHERE node_id=? "
                "AND state IN ('QUEUED','LEASED','RUNNING','UNKNOWN')",
                (node_id,),
            ):
                _fence(self.store, row, "VOICE_OWNER_CONFIRMED_STOPPED", now)
            self.store.execute("INSERT INTO voice_node_state(node_id,resource_state,last_seen_at,"
                "recovered_incarnation) VALUES (?,'READY',?,?) ON CONFLICT(node_id) DO UPDATE SET "
                "active_job_id=NULL,resource_state='READY',recovery_required=0,recovered_incarnation=?,"
                "claim_response_json=NULL,claim_request_sha256=NULL,claim_expires_at=NULL,claim_job_id=NULL",
                (node_id, now, request.incarnation, request.incarnation))
            self.store.audit("voice.owner_recovered", node_id, node_id, now,
                operator=request.operator, service_instance=request.service_instance,
                incarnation=request.incarnation, confirmation=request.confirmation)
            return self.session(node_id)

    def stop(self, node_id, request):
        with self._transaction():
            row = self._get(request.ref.job_id)
            if (
                row["node_id"] != node_id
                or row["generation"] != request.ref.generation
                or not row["stop_token_sha256"]
                or not hmac.compare_digest(
                    row["stop_token_sha256"],
                    hashlib.sha256(request.ref.stop_token.encode()).hexdigest(),
                )
            ):
                raise CoreError("VOICE_STOP_SCOPE_MISMATCH", 403)
            if row["state"] in ACTIVE:
                reason = ("VOICE_RESULT_UNCONFIRMED" if request.outcome == "COMMITTED"
                          else "VOICE_NODE_" + request.outcome)
                _fence(self.store, row, reason, self.clock())
                row = self._get(row["job_id"])
            if request.resource_state == "READY":
                self.store.execute(
                    "UPDATE voice_node_state SET active_job_id=NULL,resource_state='READY' "
                    "WHERE node_id=? AND active_job_id=?",
                    (node_id, row["job_id"]),
                )
            else:
                self.store.execute(
                    "UPDATE voice_node_state SET resource_state=? "
                    "WHERE node_id=? AND active_job_id=?",
                    (request.resource_state, node_id, row["job_id"]),
                )
            # Retire the replay payload even when a completed native result still awaits delivery.
            self.store.execute(
                "UPDATE voice_node_state SET claim_response_json=NULL,"
                "claim_request_sha256=NULL,claim_expires_at=NULL,claim_job_id=NULL "
                "WHERE claim_job_id=?",
                (row["job_id"],),
            )
            closed = not self.store.one(
                "SELECT 1 FROM voice_node_state WHERE node_id=? AND active_job_id=?",
                (node_id, row["job_id"]),
            )
            confirmed = request.cleanup.complete() and request.cleanup_epoch == row["cleanup_epoch"]
            if confirmed:
                self.store.execute(
                    "UPDATE voice_jobs SET cleanup_state='CONFIRMED',cleanup_confirmed_at=? "
                    "WHERE job_id=?",
                    (self.clock(), row["job_id"]),
                )
            elif row["cleanup_state"] != "CONFIRMED":
                self.store.execute(
                    "UPDATE voice_jobs SET cleanup_state='PENDING' WHERE job_id=?", (row["job_id"],)
                )
            return VoiceStopResponse(custody_closed=closed, cleanup_confirmed=confirmed,
                                     cleanup_epoch=row["cleanup_epoch"])

    def claim(self, node_id, request):
        error, response = None, None
        with self._transaction():
            try:
                response = self._claim(node_id, request)
            except CoreError as caught:
                # A revoked configuration must clear existing custody even if
                # this claim cannot receive a new lease under the current window.
                error = caught
        if error:
            raise error
        return response

    def _claim(self, node_id, request):
        with self._transaction():
            if node_id != request.node_id:
                raise CoreError("VOICE_NODE_SCOPE_REQUIRED", 403)
            states = sorted(ACTIVE | COMPLETED)
            for row in self.store.rows(
                "SELECT * FROM voice_jobs WHERE node_id=? AND state IN "
                f"({','.join('?' for _ in states)})", (node_id, *states),
            ):
                self._reconcile(row)
            _, node = self._node(node_id)
            now = self.clock()
            session = self.session(node_id)
            if request.incarnation != session.incarnation:
                raise CoreError("VOICE_CLAIM_EPOCH_RETIRED", 410)
            if session.recovery_required:
                raise CoreError("VOICE_NODE_RECOVERY_REQUIRED", 409)
            state = self.store.one("SELECT * FROM voice_node_state WHERE node_id=?", (node_id,))
            last = state["last_claim_seq"] if state else 0
            if request.sequence < last:
                raise CoreError("VOICE_CLAIM_RETIRED", 410)
            if request.sequence == last:
                if not state["claim_response_json"] or state["claim_expires_at"] <= now:
                    if state["claim_response_json"]:
                        self.store.execute("UPDATE voice_node_state SET claim_response_json=NULL,"
                            "claim_request_sha256=NULL,claim_expires_at=NULL,claim_job_id=NULL "
                            "WHERE node_id=?", (node_id,))
                    raise CoreError("VOICE_CLAIM_RETIRED", 410)
                if state["claim_request_sha256"] != digest(request):
                    raise CoreError("VOICE_IDEMPOTENCY_CONFLICT", 409)
                return VoiceClaimResponse.model_validate_json(state["claim_response_json"])
            if request.sequence != last + 1:
                raise CoreError("VOICE_CLAIM_SEQUENCE_GAP", 409)
            if request.manifest_sha256 != node.manifest_sha256:
                raise CoreError("VOICE_MANIFEST_CHANGED", 409)
            self.store.execute(
                "INSERT INTO voice_node_state(node_id,resource_state,last_seen_at) VALUES (?,?,?) "
                "ON CONFLICT(node_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (node_id, request.resource_state, now),
            )
            state = self.store.one("SELECT * FROM voice_node_state WHERE node_id=?", (node_id,))
            response = VoiceClaimResponse()
            if not state["active_job_id"]:
                self.store.execute(
                    "UPDATE voice_node_state SET resource_state=? WHERE node_id=?",
                    (request.resource_state, node_id),
                )
                if request.resource_state == "READY":
                    row = self.store.one(
                        "SELECT * FROM voice_jobs WHERE node_id=? AND state='QUEUED' "
                        "ORDER BY created_at,job_id LIMIT 1",
                        (node_id,),
                    )
                    if row:
                        lease = VoiceLease(
                            incarnation=session.incarnation,
                            claim_sequence=request.sequence,
                            stop_token=secrets.token_urlsafe(32),
                            privacy_epoch=row["cleanup_epoch"],
                            job_id=row["job_id"],
                            generation=row["generation"],
                            node_id=node_id,
                            operation=row["operation"],
                            lease_token=secrets.token_urlsafe(32),
                            input_sha256=row["input_sha256"],
                            input_bytes=row["input_bytes"],
                            input_mime=row["input_mime"],
                            source_partial=bool(row["source_partial"]),
                            manifest_sha256=row["manifest_sha256"],
                            issued_at=now,
                            server_time=now,
                            lease_expires_at=min(
                                after(now, self.settings.voice.lease_ttl_seconds),
                                row["deadline_at"],
                            ),
                            job_deadline_at=row["deadline_at"],
                        )
                        signed = sign_voice_lease(lease, self.settings.voice.private_key())
                        self.store.execute(
                            "UPDATE voice_jobs SET state='LEASED',lease_token_sha256=?,"
                            "stop_token_sha256=?,"
                            "lease_json=?,lease_issued_at=?,lease_until=?,updated_at=? "
                            "WHERE job_id=?",
                            (
                                hashlib.sha256(lease.lease_token.encode()).hexdigest(),
                                hashlib.sha256(lease.stop_token.encode()).hexdigest(),
                                canonical_bytes(signed).decode(),
                                now,
                                lease.lease_expires_at.isoformat(timespec="microseconds"),
                                now,
                                row["job_id"],
                            ),
                        )
                        self.store.execute(
                            "UPDATE voice_node_state SET active_job_id=? WHERE node_id=?",
                            (row["job_id"], node_id),
                        )
                        self.store.audit(
                            "voice.leased",
                            row["job_id"],
                            row["event_id"],
                            now,
                            generation=row["generation"],
                            node_id=node_id,
                        )
                        response = VoiceClaimResponse(lease=signed)
            self.store.execute(
                "UPDATE voice_node_state SET last_claim_seq=?,claim_request_sha256=?,"
                "claim_response_json=?,claim_job_id=?,claim_expires_at=? WHERE node_id=?",
                (request.sequence, digest(request), canonical_bytes(response).decode(),
                 response.lease.job_id if response.lease else None,
                 response.lease.job_deadline_at.isoformat(timespec="microseconds")
                 if response.lease else after(now, self.settings.voice.job_ttl_seconds), node_id),
            )
            return response

    def control(self, node_id, request):
        with self._transaction():
            row = self._reconcile(self._ref(node_id, request.ref))
            now = self.clock()
            self.store.execute(
                "UPDATE voice_node_state SET resource_state=?,last_seen_at=?,last_error_code=? "
                "WHERE node_id=? AND active_job_id=?",
                (request.resource_state, now, request.error_code, node_id, row["job_id"]),
            )
            if row["state"] in ACTIVE and request.phase in {
                "FAILED",
                "CANCELLED",
                "UNKNOWN",
                "RESULT_LOST",
            }:
                _fence(self.store, row, "VOICE_NODE_" + request.phase, now)
                row = self._get(row["job_id"])
            if row["state"] not in ACTIVE:
                if request.resource_state == "READY" and request.phase not in {
                    "CLAIMED",
                    "RUNNING",
                }:
                    self.store.execute(
                        "UPDATE voice_node_state SET active_job_id=NULL WHERE active_job_id=?",
                        (row["job_id"],),
                    )
                if row["state"] in COMPLETED:
                    if request.result_sha256 not in {None, row["result_sha256"]}:
                        raise CoreError("VOICE_RESULT_CONFLICT", 409)
                    return VoiceControlResponse(
                        action="COMMITTED", committed_result_sha256=row["result_sha256"]
                    )
                return VoiceControlResponse(
                    action=(
                        "EXPIRED"
                        if row["last_error_code"] == "VOICE_EXPIRED"
                        else "SUPERSEDED"
                        if row["last_error_code"] == "REPLY_SUPERSEDED"
                        else "CANCEL"
                    )
                )
            if request.resource_state == "QUARANTINED":
                _fence(self.store, row, "VOICE_GPU_QUARANTINED", now)
                return VoiceControlResponse(action="CANCEL")
            if request.phase in {"RUNNING", "RESULT_READY"} and not row["started_at"]:
                raise CoreError("VOICE_INPUT_NOT_RELEASED", 409)
            lease = VoiceLease.model_validate(
                {
                    key: value
                    for key, value in json.loads(row["lease_json"]).items()
                    if key != "signature"
                }
            )
            lease = lease.model_copy(
                update={
                    "server_time": datetime.fromisoformat(now),
                    "lease_expires_at": datetime.fromisoformat(
                        min(after(now, self.settings.voice.lease_ttl_seconds), row["deadline_at"])
                    ),
                }
            )
            signed = sign_voice_lease(lease, self.settings.voice.private_key())
            self.store.execute(
                "UPDATE voice_jobs SET lease_json=?,lease_until=?,updated_at=? WHERE job_id=?",
                (
                    canonical_bytes(signed).decode(),
                    lease.lease_expires_at.isoformat(timespec="microseconds"),
                    now,
                    row["job_id"],
                ),
            )
            return VoiceControlResponse(action="CONTINUE", renewed_lease=signed)

    def input(self, node_id, ref):
        error, result = None, None
        with self._transaction():
            row = self._reconcile(self._ref(node_id, ref))
            if row["state"] not in {"LEASED", "RUNNING"} or row["input_blob"] is None:
                error = CoreError("VOICE_INPUT_FENCED", 409)
            else:
                now = self.clock()
                self.store.execute(
                    "UPDATE voice_jobs SET state='RUNNING',started_at=COALESCE(started_at,?),"
                    "updated_at=? WHERE job_id=?",
                    (now, now, row["job_id"]),
                )
                self.store.execute(
                    "UPDATE voice_node_state SET resource_state='RUNNING' WHERE active_job_id=?",
                    (row["job_id"],),
                )
                result = row["input_blob"], row["input_mime"]
        if error:
            raise error
        return result

    def submit(self, node_id, result):
        error, receipt = None, None
        with self._transaction():
            row = self._reconcile(self._ref(node_id, result.ref))
            if row["state"] not in {"RUNNING", *COMPLETED}:
                error = CoreError("VOICE_RESULT_FENCED", 409)
            elif row["result_sha256"] and row["result_sha256"] != result.result_sha256:
                error = CoreError("VOICE_RESULT_CONFLICT", 409)
            elif result.operation != row["operation"]:
                error = CoreError("VOICE_RESULT_SCOPE_MISMATCH", 409)
            else:
                node = self.settings.voice.nodes[node_id]
                payload = result.payload
                if isinstance(payload, SingingResult):
                    from companion.singing import check_source

                    _, recipe = check_source(self.store, self.settings, self.clock, row)
                    source = payload.manifest["source"]
                    if (payload.manifest["node_manifest_sha256"] != row["manifest_sha256"]
                        or source["bvid"] != recipe.bvid or source["part"] != recipe.part
                        or source["audio_bytes"] > recipe.max_input_bytes
                        or source["duration_milliseconds"] > recipe.max_seconds * 1000
                        or source["used_bytes"] > row["source_reserved_bytes"]
                        or source["used_requests"] > row["source_reserved_requests"]):
                        raise CoreError("SINGING_RESULT_SOURCE_CHANGED", 400)
                invalid = (
                    isinstance(payload, VoiceAudioResult)
                    and (
                        len(payload.audio_bytes()) > node.max_tts_output_bytes
                        or payload.duration_seconds > node.max_tts_seconds
                    )
                ) or (
                    isinstance(payload, VoiceTranscriptResult)
                    and (
                        len(payload.text) > node.max_asr_chars
                        or len(payload.segments) > node.max_asr_segments
                        or abs(payload.duration_seconds - row["input_duration_seconds"]) > 1e-9
                        or payload.partial != bool(row["source_partial"])
                    )
                )
                if invalid:
                    error = CoreError("VOICE_RESULT_LIMIT_EXCEEDED", 400)
                elif row["state"] == "RUNNING":
                    data, now = canonical_bytes(payload), self.clock()
                    self._capacity(len(data), replace_job_id=row["job_id"])
                    self.store.execute(
                        "UPDATE voice_jobs SET state=?,input_blob=NULL,result_blob=?,"
                        "result_sha256=?,result_bytes=?,completed_at=?,result_expires_at=?,"
                        "updated_at=?,reserved_output_bytes=0 WHERE job_id=?",
                        (
                            "TRANSCRIBED" if row["operation"] == "asr" else "READY",
                            data,
                            result.result_sha256,
                            len(data),
                            now,
                            min(
                                row["deadline_at"],
                                after(now, self.settings.voice.result_ttl_seconds),
                            ),
                            now,
                            row["job_id"],
                        ),
                    )
                    self.store.execute(
                        "UPDATE voice_node_state SET active_job_id=NULL WHERE active_job_id=? "
                        "AND resource_state='READY'",
                        (row["job_id"],),
                    )
                    if row["operation"] == "sing":
                        self.store.execute("UPDATE voice_jobs SET source_used_requests=?,"
                            "source_used_bytes=? WHERE job_id=?",
                            (payload.manifest["source"]["used_requests"],
                             payload.manifest["source"]["used_bytes"], row["job_id"]))
                    self.store.audit(
                        "voice.committed",
                        row["job_id"],
                        row["event_id"],
                        now,
                        result_sha256=result.result_sha256,
                        result_bytes=len(data),
                    )
                if error is None:
                    receipt = VoiceResultReceipt(
                        job_id=row["job_id"],
                        generation=row["generation"],
                        result_sha256=result.result_sha256,
                    )
        if error:
            raise error
        return receipt
