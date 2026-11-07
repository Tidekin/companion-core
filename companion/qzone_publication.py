"""Owner-reviewed public QZone actions in native plans, audits and the original Outbox."""

import json
from datetime import UTC
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from companion import owner_authority
from companion.account_actions import scope
from companion.contracts import CoreError, StrictModel
from companion.paid_budget import sha
from companion.qzone import review_source
from companion.social_autonomy import Publish, _instant, _lineage, _tx, public_source
from companion.store import after, uid

PREFIX = "qzone-publication:"
SHA = r"^[a-f0-9]{64}$"


class PublicationCreate(StrictModel):
    job_id: str = Field(min_length=1, max_length=128)
    expected_content_sha256: str = Field(pattern=SHA)
    text: str | None = Field(default=None, min_length=1, max_length=5000)
    expires_at: AwareDatetime | None = None


class PublicationEdit(StrictModel):
    expected_payload_sha256: str = Field(pattern=SHA)
    text: str = Field(min_length=1, max_length=5000)
    expires_at: AwareDatetime | None = None


class PublicationDecision(StrictModel):
    expected_payload_sha256: str = Field(pattern=SHA)
    decision: Literal["APPROVE", "REJECT", "REVOKE"]
    source_sharing_confirmed: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def sharing(self):
        if self.decision == "APPROVE" and not self.source_sharing_confirmed:
            raise ValueError("Approve the exact draft's source sharing explicitly")
        return self


def policy(module, authority=None):
    cfg = module._policy()
    if not cfg.qzone_enabled or not cfg.qzone_public_enabled:
        raise CoreError("QZONE_PUBLICATION_DISABLED", 403)
    view = owner_authority.current(module.store, module.settings, module.clock,
        purpose="qzone_publication", authority=authority)
    if view.policy.capability_policies.get("qzone_publication") != sha(cfg.model_dump(mode="json")):
        raise CoreError("QZONE_PUBLICATION_POLICY_CHANGED", 403)
    scope(module.store, module.settings, module.clock, view.authority, cfg.authorization_sha256)
    return cfg, view


def context(module, job_id):
    """Sharing admission is local to an exact reviewed plan; public_source stays strict."""
    _, view = policy(module)
    data, proof = module._life_draft(job_id)
    sources = []
    for source in data["sources"]:
        event_id = source["event_id"]
        if source["source"] == "observation.qzone":
            review_source(module.store, module.settings, event_id, module.clock())
            row = module.store.one('SELECT o.event_id AS event_id, o.adapter_id AS adapter_id, '
                'o.source_kind AS source_kind, o.result_status AS result_status, o.source_sha256 '
                'AS source_sha256, o.canonical_url AS canonical_url, o.device_id AS device_id, '
                'o.session_id AS session_id, o.consent_id AS consent_id, o.evidence_json AS '
                'evidence_json, o.captured_privacy_epoch AS captured_privacy_epoch, o.redacted AS '
                'redacted, o.created_at AS created_at, e.body_json AS body_json FROM observations '
                'AS o JOIN events AS e ON o.event_id = e.event_id WHERE COALESCE(o.event_id, '
                'e.event_id) = %s', (event_id,))
            body, evidence = json.loads(row["body_json"]), json.loads(row["evidence_json"])
            sources.append({"ref": event_id, "title": body["title"], "text": body["text"],
                "url": row["canonical_url"], "coverage": evidence["read_observation"]["coverage"],
                "source_sha256": row["source_sha256"], "provenance_sha256": sha(evidence),
                "sharing": "owner_review_required", "dependencies": _lineage(module.store,
                    event_id, module.clock(), module.settings.raw_event_retention_days)})
        else:
            sources.append(public_source(module.store, module.settings, event_id, module.clock()))
    if not sources or len(sources) > module.config.max_source_refs:
        raise CoreError("QZONE_PUBLICATION_SOURCE_REQUIRED", 403)
    return {"sources": sorted(sources, key=lambda s: s["ref"]), "assets": [], "profiles": [],
            "authority": view.authority.as_dict(), "life_result": proof,
            "publication_review": {"job_id": job_id}}


def _row(module, plan_id):
    row = module.store.one('SELECT social_autonomy_plans.plan_id AS plan_id, '
        'social_autonomy_plans.idempotency_key AS idempotency_key, '
        'social_autonomy_plans.request_sha256 AS request_sha256, '
        'social_autonomy_plans.candidate_sha256 AS candidate_sha256, social_autonomy_plans.state '
        'AS state, social_autonomy_plans.policy_version AS policy_version, '
        'social_autonomy_plans.policy_sha256 AS policy_sha256, '
        'social_autonomy_plans.authorization_sha256 AS authorization_sha256, '
        'social_autonomy_plans.binding_sha256 AS binding_sha256, social_autonomy_plans.timezone AS '
        'timezone, social_autonomy_plans.policy_json AS policy_json, '
        'social_autonomy_plans.input_snapshot_json AS input_snapshot_json, '
        'social_autonomy_plans.source_snapshot_sha256 AS source_snapshot_sha256, '
        'social_autonomy_plans.plan_json AS plan_json, social_autonomy_plans.payload_sha256 AS '
        'payload_sha256, social_autonomy_plans.model_request_id AS model_request_id, '
        'social_autonomy_plans.last_error_code AS last_error_code, social_autonomy_plans.redacted '
        'AS redacted, social_autonomy_plans.created_at AS created_at, '
        'social_autonomy_plans.expires_at AS expires_at, social_autonomy_plans.updated_at AS '
        'updated_at FROM social_autonomy_plans AS social_autonomy_plans WHERE '
        'social_autonomy_plans.plan_id = %s', (plan_id,))
    if not row or not row["idempotency_key"].startswith(PREFIX):
        raise CoreError("QZONE_PUBLICATION_NOT_FOUND", 404)
    return row


def _decision(module, plan_id):
    row = module.store.one('SELECT safe_detail_json FROM audit_log WHERE action = '
        "'qzone.publication_decision' AND entity_id = %s ORDER BY audit_id DESC NULLS LAST LIMIT 1",
        (plan_id,))
    return json.loads(row["safe_detail_json"]) if row else {}


def require_approval(module, row, frozen):
    if (not row["idempotency_key"].startswith(PREFIX)
        or frozen.schema_version != 2 or row["state"] != "READY" or row["redacted"]
        or _instant(row["expires_at"]) <= _instant(module.clock())):
        raise CoreError("QZONE_PUBLICATION_APPROVAL_REQUIRED", 403)
    original = json.loads(row["input_snapshot_json"])
    policy(module, original.get("authority", {}))
    expected = {"decision": "APPROVE", "payload_sha256": frozen.payload_sha256,
                "source_sharing_confirmed": True}
    if _decision(module, row["plan_id"]) != expected:
        raise CoreError("QZONE_PUBLICATION_APPROVAL_REQUIRED", 403)


def view(module, plan_id):
    row = _row(module, plan_id)
    delivery = module.get(plan_id)["delivery"]
    result = {"plan_id": plan_id, "state": "UNAVAILABLE", "text": "", "sources": [],
              "payload_sha256": row["payload_sha256"], "expires_at": row["expires_at"],
              "audience": "public", "delivery": delivery, "source_sharing_required": True}
    if not row["redacted"]:
        result["source_job_id"] = json.loads(row["input_snapshot_json"]).get(
            "publication_review", {}).get("job_id")
    try:
        module._validate_plan(plan_id)
        frozen = module._frozen(row)
    except CoreError as error:
        result["error_code"] = error.code
    else:
        result.update(text=frozen.proposal.text, bot_qq_id=frozen.bot_qq_id,
            source_snapshot_sha256=frozen.source_snapshot_sha256,
            sources=[{k: s[k] for k in ("ref", "title", "text", "url", "coverage", "source_sha256")}
                     for s in json.loads(row["input_snapshot_json"])["sources"]],
            state="WAITING_APPROVAL")
    decision = _decision(module, plan_id)
    if row["state"] == "CANCELLED":
        result["state"] = "CANCELLED"
    if delivery:
        result["state"] = delivery["state"]
        receipt = module.store.one('SELECT r.receipt_json, r.receipt_sha256, o.lease_token FROM '
            'social_autonomy_reservations AS r JOIN outbox AS o USING (message_id) WHERE '
            'message_id = %s', (delivery["message_id"],))
        from companion.social_autonomy_protocol import ActionReceipt

        try:
            native = ActionReceipt.model_validate_json(receipt["receipt_json"] if receipt else "{}")
            if native.message_id != delivery["message_id"] or (
                native.payload_sha256 != row["payload_sha256"]
                or native.action_type != "qzone.publish"
                or native.lease_token != receipt["lease_token"]
                or sha(native.model_dump(mode="json")) != receipt["receipt_sha256"]
            ):
                raise ValueError("receipt binding differs")
            result["receipt"] = {"result": native.result, "resource_id": native.resource_id}
            if delivery["state"] == "SENT" and native.result != "SUCCEEDED":
                result["state"] = "UNVERIFIED"
        except (ValueError, TypeError):
            if delivery["state"] == "SENT":
                result["state"] = "UNVERIFIED"
    result["decision"] = decision.get("decision")
    return result


def _receipt(module, key, kind, body, plan_id=None):
    if (not isinstance(key, str) or not 8 <= len(key) <= 128 or not key.isascii()
        or any(ord(c) < 33 or ord(c) > 126 for c in key)):
        raise CoreError("IDEMPOTENCY_KEY_REQUIRED", 400)
    fingerprint = sha([kind, plan_id, body.model_dump(mode="json")])
    row = module.store.one('SELECT entity_id, safe_detail_json FROM audit_log WHERE action = '
        "'qzone.publication_operation' AND trace_id = %s", (key,))
    if row and json.loads(row["safe_detail_json"]) != {"request_sha256": fingerprint}:
        raise CoreError("IDEMPOTENCY_CONFLICT", 409)
    return fingerprint, view(module, row["entity_id"]) if row else None


def _save_receipt(module, key, fingerprint, plan_id):
    module.store.audit("qzone.publication_operation", plan_id, key, module.clock(),
                       request_sha256=fingerprint)
    return view(module, plan_id)


def prepare(module, body, key, *, owner_check=None):
    with _tx(module.store):
        if owner_check:
            owner_check()
        cfg, _ = policy(module)
        fingerprint, previous = _receipt(module, key, "create", body)
        if previous:
            return previous
        data, proof = module._life_draft(body.job_id)
        if proof["content_sha256"] != body.expected_content_sha256:
            raise CoreError("LIFE_DRAFT_CHANGED", 409)
        material = context(module, body.job_id)
        now = module.clock()
        expires = min(_instant(data["qzone_draft"]["expires_at"]),
                      _instant(after(now, cfg.plan_ttl_seconds)))
        if body.expires_at is not None:
            if body.expires_at > expires:
                raise CoreError("QZONE_PUBLICATION_EXPIRY_INVALID", 400)
            expires = body.expires_at
        if expires <= _instant(now):
            raise CoreError("QZONE_PUBLICATION_EXPIRY_INVALID", 400)
        candidate = sha([PREFIX, body.job_id])
        if module.store.one('SELECT 1 FROM social_autonomy_plans WHERE candidate_sha256 = %s',
                            (candidate,)):
            raise CoreError("SOCIAL_CANDIDATE_ALREADY_CONSIDERED", 409)
        timezone = module._bind()
        module._advance_clock(now)
        module._pending_room(cfg, now)
        plan_id = uid("social")
        module._store_plan(plan_id, PREFIX + key, fingerprint, candidate, material, cfg, timezone,
            now, None, expires_at=expires.astimezone(UTC).isoformat(timespec="microseconds"))
        row = _row(module, plan_id)
        frozen = module._freeze(row, Publish(kind="qzone_publish",
            text=body.text if body.text is not None else data["result"]["text"],
            source_refs=data["decision"].source_event_ids))
        module.store.execute("UPDATE social_autonomy_plans SET state = 'READY', plan_json = %s, "
            'payload_sha256 = %s WHERE plan_id = '
            '%s', (frozen.model_dump_json(), frozen.payload_sha256,
                                                plan_id))
        return _save_receipt(module, key, fingerprint, plan_id)


def edit(module, plan_id, body, key, *, owner_check=None):
    with _tx(module.store):
        if owner_check:
            owner_check()
        policy(module)
        fingerprint, previous = _receipt(module, key, "edit", body, plan_id)
        if previous:
            return previous
        row = module._validate_plan(_row(module, plan_id)["plan_id"])
        if row["payload_sha256"] != body.expected_payload_sha256:
            raise CoreError("QZONE_PUBLICATION_CHANGED", 409)
        if module.store.one('SELECT 1 FROM social_autonomy_reservations WHERE plan_id = %s',
                            (plan_id,)):
            raise CoreError("QZONE_PUBLICATION_ALREADY_RESERVED", 409)
        if body.expires_at is not None:
            if not _instant(module.clock()) < body.expires_at <= _instant(row["expires_at"]):
                raise CoreError("QZONE_PUBLICATION_EXPIRY_INVALID", 400)
            row = {**row, "expires_at": body.expires_at.astimezone(UTC).isoformat(
                timespec="microseconds")}
        frozen = module._freeze(row, Publish(kind="qzone_publish", text=body.text,
            source_refs=module._frozen(row).proposal.source_refs))
        module.store.execute('UPDATE social_autonomy_plans SET plan_json = %s, payload_sha256 = '
            '%s, expires_at = %s, updated_at = %s WHERE plan_id = %s', (frozen.model_dump_json(),
                frozen.payload_sha256, row["expires_at"], module.clock(), plan_id))
        return _save_receipt(module, key, fingerprint, plan_id)


def decide(module, plan_id, body, key, *, owner_check=None):
    s, now = module.store, module.clock()
    with _tx(s):
        if owner_check:
            owner_check()
        fingerprint, previous = _receipt(module, key, "decision", body, plan_id)
        if previous:
            return previous
        row = _row(module, plan_id)
        if row["payload_sha256"] != body.expected_payload_sha256 or row["redacted"]:
            raise CoreError("QZONE_PUBLICATION_CHANGED", 409)
        if body.decision == "APPROVE":
            policy(module)
            module._validate_plan(plan_id)
        decision = {"decision": body.decision, "payload_sha256": row["payload_sha256"],
                    "source_sharing_confirmed": body.source_sharing_confirmed}
        s.audit("qzone.publication_decision", plan_id, key, now, **decision)
        if body.decision == "APPROVE":
            data, _ = module._life_draft(json.loads(row["input_snapshot_json"])[
                "publication_review"]["job_id"])
            module.reserve(plan_id, data["job"]["conversation_id"])
        else:
            # Reuse native cancellation semantics: no dispatched reservation is released.
            s.execute("UPDATE social_autonomy_plans SET state = 'CANCELLED', updated_at = %s, "
                "last_error_code = 'QZONE_PUBLICATION_REVOKED' WHERE plan_id = %s", (now, plan_id))
            s.execute('UPDATE social_autonomy_reservations SET released_at = %s WHERE plan_id = %s '
                'AND dispatched_at IS NULL AND message_id IN (SELECT message_id FROM outbox WHERE '
                "attempt_count = 0 AND state IN ('PENDING', 'LEASED', 'RETRY_WAIT'))",
                (now, plan_id))
            s.execute("UPDATE outbox SET state = CASE WHEN state = 'SENDING' THEN "
                "'DELIVERY_UNCERTAIN' WHEN state IN ('PENDING', 'LEASED', 'RETRY_WAIT') THEN "
                "'CANCELLED' ELSE state END, last_error_code = 'QZONE_PUBLICATION_REVOKED' WHERE "
                'message_id IN (SELECT message_id FROM social_autonomy_reservations WHERE plan_id '
                '= %s)', (plan_id,))
        return _save_receipt(module, key, fingerprint, plan_id)
