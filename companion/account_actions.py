"""Finite account delivery fences on the existing Outbox and cumulative journal."""

import json

from pydantic import Field, field_validator

from companion import cumulative, owner_authority, paid_budget, transport
from companion.contracts import CoreError, StrictModel
from companion.social_autonomy import SocialAutonomy
from companion.social_autonomy_protocol import ProfileBaseline
from companion.store import encode
from companion_delivery_protocol.authority import validate_authority_ref

DELIVERY = "account_action_delivery:"
CANCELLED = "account_action_not_sent:"
ACTIONS = {"qzone.publish", "qq.profile.nickname", "qq.profile.signature", "qq.profile.avatar"}


class AuthorityBody(StrictModel):
    authority: dict

    @field_validator("authority")
    @classmethod
    def exact_authority(cls, value):
        return validate_authority_ref(value)


class SendCheck(AuthorityBody):
    message_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=1, max_length=128)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class CancelBeforeSend(SendCheck):
    reason: str = Field(pattern=r"^[A-Z0-9_]{1,64}$")


class ProfileProbe(AuthorityBody):
    authorization_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    profiles: list[ProfileBaseline] = Field(min_length=1, max_length=3)

    @field_validator("profiles")
    @classmethod
    def unique_fields(cls, profiles):
        if len({item.field for item in profiles}) != len(profiles):
            raise ValueError("Each profile field has one probe receipt")
        return profiles


def record_profiles(store, settings, clock, body):
    with store.transaction():
        scope(store, settings, clock, body.authority, body.authorization_sha256)
        module = SocialAutonomy(store, settings, clock=clock)
        for profile in body.profiles:
            if not getattr(settings.social_autonomy, profile.field + "_enabled"):
                raise CoreError("SOCIAL_PROFILE_SCOPE_REQUIRED", 403)
            module.record_profile(profile)
        return {"accepted": True, "profiles": [{"field": row.field,
                "observed_at": row.observed_at} for row in body.profiles]}


def profile_probe_state(store, settings, clock, authority, authorization_sha256):
    with store.transaction():
        view = scope(store, settings, clock, authority, authorization_sha256)
        policy = settings.social_autonomy
        return {
            "authority": view.authority.as_dict(),
            "authorization_sha256": authorization_sha256,
            "fields": [name for name in ("nickname", "signature", "avatar")
                       if getattr(policy, name + "_enabled")],
            "baseline_seconds": policy.profile_baseline_seconds,
        }


def scope(store, settings, clock, authority, authorization_sha256, *, message_id=None):
    view = owner_authority.current(store, settings, clock,
                                   purpose="account_actions", authority=authority)
    owner_authority.current(store, settings, clock, purpose="social", authority=view.authority)
    cfg = settings.social_autonomy
    if (not cfg.enabled or cfg.scope != "qq_social_v1"
        or view.policy.capability_policies.get("account_actions") != authorization_sha256
        or cfg.authorization_sha256 != authorization_sha256):
        raise CoreError("SOCIAL_LIVE_SCOPE_REQUIRED", 403)
    if message_id is not None:
        row = store.one("SELECT authority_json,delivery_lane FROM outbox WHERE message_id=?",
                        (message_id,))
        if (not row or row["delivery_lane"] != "qq_account"
            or json.loads(row["authority_json"]) != view.authority.as_dict()):
            raise CoreError("SOCIAL_DELIVERY_AUTHORITY_CHANGED", 409)
    return view


def _meta(store, key):
    row = store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))
    return json.loads(row["value"]) if row else None


def lease_receipt(store, settings, clock, message_id, lease_token, view):
    frozen = SocialAutonomy(store, settings, clock=clock).validate_delivery(message_id, lease_token)
    key = DELIVERY + message_id
    expected = {"authority": view.authority.as_dict(), "message_id": message_id,
                "lease_token": lease_token,
                "authorization_sha256": frozen.authorization_sha256,
                "payload_sha256": frozen.payload_sha256,
                "content_sha256": paid_budget.sha(frozen.model_dump(mode="json"))}
    previous = _meta(store, key)
    if previous is not None and any(previous.get(k) != v for k, v in expected.items()
                                    if k != "lease_token"):
        raise CoreError("SOCIAL_DELIVERY_AUTHORITY_CHANGED", 409)
    store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)", (key, encode(expected)))
    return frozen


def send_check(store, settings, clock, body):
    with store.transaction():
        view = scope(store, settings, clock, body.authority,
                         settings.social_autonomy.authorization_sha256,
                         message_id=body.message_id)
        module = SocialAutonomy(store, settings, clock=clock)
        frozen = lease_receipt(store, settings, clock, body.message_id, body.lease_token, view)
        row = store.one("SELECT * FROM outbox WHERE message_id=?", (body.message_id,))
        if (row["state"] != "SENDING" or row["content_sha256"] != body.content_sha256
            or frozen.payload_sha256 != body.payload_sha256):
            raise CoreError("SOCIAL_DELIVERY_CHANGED", 403)
        cumulative.admit_operation(store, settings, clock(), "send", body.message_id,
                                   paid_budget.sha(body.model_dump()), logical=frozen.plan_id)
        module.mark_dispatched(body.message_id, body.lease_token)
        return {"approved": True, "lease_until": row["lease_until"],
                "content_sha256": row["content_sha256"], "payload_sha256": frozen.payload_sha256}


def cancel_before_send(store, settings, clock, body):
    # Stopping and erasure cannot prevent a matching no-I/O receipt. Retained
    # frozen hashes suffice; this path does not re-authorize a plan or read text.
    material = body.model_dump()
    key = CANCELLED + body.message_id
    with store.transaction():
        previous = _meta(store, key)
        if previous is not None:
            if previous["request"] != material:
                raise CoreError("IDEMPOTENCY_CONFLICT", 409)
            return previous["response"]
        proof = _meta(store, DELIVERY + body.message_id)
        row = store.one("SELECT o.*,r.dispatched_at FROM outbox o JOIN "
                        "social_autonomy_reservations r USING(message_id) "
                        "WHERE o.message_id=?", (body.message_id,))
        if (not row or not proof or proof.get("lease_token") != body.lease_token
            or (row["lease_token"] != body.lease_token
                and not (row["state"] == "CANCELLED" and row["lease_token"] is None))
            or row["delivery_lane"] != "qq_account"
            or row["state"] not in {"LEASED", "SENDING", "CANCELLED", "DELIVERY_UNCERTAIN"}
            or row["attempt_count"] > 1 or row["dispatched_at"] is not None
            or any(proof[name] != material[name] for name in (
                "message_id", "content_sha256", "payload_sha256"))
            or proof.get("authority") != body.authority
            or json.loads(row["authority_json"]) != body.authority
            or store.one("SELECT 1 FROM audit_log WHERE entity_id=? AND action='delivery.result'",
                         (body.message_id,))):
            raise CoreError("SOCIAL_NOT_SENT_PROOF_REQUIRED", 409)
        cumulative.send_result(store, body.message_id, "FAILED", clock())
        transport.mark_not_dispatched(store, body.message_id, clock(),
                                      attempt_count=row["attempt_count"], reason=body.reason)
        store.execute("UPDATE social_autonomy_reservations SET released_at=? WHERE message_id=?",
                      (clock(), body.message_id))
        result = {"state": "NOT_SENT", "message_id": body.message_id,
                  "lease_token": body.lease_token, "content_sha256": body.content_sha256,
                  "payload_sha256": body.payload_sha256}
        store.insert("runtime_meta", key=key,
                     value=encode({"request": material, "response": result}))
        store.audit("delivery.account_not_sent", body.message_id, "delivery", clock(),
                    reason=body.reason)
        return result
