from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import companion_delivery_protocol as wire
from companion import budget, cumulative, live_text, owner_authority, paid_budget
from companion import delivery_reconciliation as reconciliation
from companion.contracts import DeliveryResult
from companion.model import ModelResponse
from companion.store import after, encode, uid
from companion.transport import delivery_result
from tests.conftest import ingest
from tests.support.cumulative_window import bind_policy
from tests.support.live_text_window import live_fixture
from tests.support.private_companion import companion_options


def put(store, key, value):
    store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)", (key, encode(value)))


def signed(env, **changes):
    proof = {**env.proof, **changes}
    proof["signature"] = env.key.sign(wire.signing_payload(proof)).hex()
    return proof


def native(core, tmp_path, *, quota=True, uncertain=True, expired=False,
           action="qq.private.poke", stopped=True, model_unknown=False, legacy=False, voice=False,
           voice_v1=False):
    accepted = ingest(core, 1, "synthetic private source")
    event = core.store.one('SELECT * FROM events WHERE event_id=%s', (accepted["event_id"],))
    key = Ed25519PrivateKey.generate()
    options = {} if legacy else companion_options(core)
    account = action in wire.ACCOUNT_ACTIONS
    if account:
        options.update(account_actions_scope="qq_social_v1",
                       account_actions_authorization_sha256="a" * 64)
    if voice:
        options.update(voice_scope="owner_voice_v1", voice_policy_sha256="b" * 64)
    cfg, plan = live_fixture(core, tmp_path, bridge_options={
        "capture_signing_key": key.private_bytes_raw().hex(),
    }, **options)
    if quota:
        plan = bind_policy(core, plan)
    approval = core.settings.live_text_window
    binding = {"approval_sha256": paid_budget.sha(approval.model_dump(mode="json")),
        "privacy_epoch": 0, "plan_sha256": plan.sha256, "window_id": approval.window_id,
        "budget_journal_id": approval.budget_journal_id,
        "budget_journal_path": str(Path(core.settings.paid_model.journal_path).resolve())}
    with core.store.transaction():
        if quota:
            cumulative.activate(core.store, core.settings, core.clock())
        paid_budget.journal(core.store, core.settings.paid_model, {
            "kind": "window_open", "window_id": approval.window_id,
            "plan_sha256": plan.sha256, "handoff_sha256": None,
        })
        put(core.store, live_text.BINDING, binding)
        put(core.store, live_text.HISTORY + plan.sha256, {
            "state": "OPEN", "binding": binding, "approval": approval.model_dump(mode="json"),
        })
    # This fixture supplies earlier window authority, while retaining the actual
    # admission, amount, model journal, settlement and cumulative implementation.
    with pytest.MonkeyPatch.context() as authority:
        authority.setattr(live_text, "reserve_model", lambda *args, **kwargs: None)
        # Historical v1 fixture only: create its already-admitted liability without
        # opening a new native grant or making any provider request.
        authority.setattr(owner_authority, "admit_model", lambda *args, **kwargs: None)
        request_id = budget.reserve(core.store, core.settings, None, "chat",
            [{"role": "user", "content": "synthetic model input"}], core.clock())
    message_id, token = uid("msg"), uid("lease")
    content = ({"kind": "poke", "target_user_id": core.settings.owner_qq_id}
               if action == "qq.private.poke" else {"text": "synthetic private reply"})
    if account:
        content = account_content(core, action, message_id, event["event_id"])
    kind = "content_sha256" if action == "qq.private.poke" else "reply_sha256"
    kind = "account_payload_sha256" if account else kind
    payload = content["payload_sha256"] if account else wire.digest(
        content if kind == "content_sha256" else content["text"])
    send_check = {"window_sha256": plan.sha256, "event_id": event["event_id"],
                  "message_id": message_id, kind: payload, "lease_token": token}
    if account:
        send_check = {"window_sha256": plan.sha256, "message_id": message_id,
                      "content_sha256": wire.digest(content), "payload_sha256": payload,
                      "lease_token": token}
    with core.store.transaction():
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s",
                           (accepted["job_id"],))
        core.store.insert("outbox", message_id=message_id,
            origin_event_id=None if account else event["event_id"],
            conversation_id=event["conversation_id"], action_type=action,
            delivery_lane="qq_account" if account else "owner_private",
            content_json=encode(content), content_sha256=wire.digest(content),
            delivery_key="synthetic-original:" + message_id, state="SENDING",
            lease_token=token, lease_until=after(core.clock(), 30), attempt_count=1,
            created_at=core.clock(), updated_at=core.clock())
        if account:
            account_authority(core, content, plan.sha256)
        else:
            put(core.store, "private_life_delivery:" + message_id, {
            "plan_sha256": plan.sha256, "privacy_epoch": 0, "message_id": message_id,
            "event_id": event["event_id"], "event_sha256": wire.digest(event),
            "content_sha256": wire.digest(content),
            "delivery_key": "synthetic-original:" + message_id,
            "action_type": action, "request_ids": [request_id],
            })
        if voice:
            send_check["voice"] = voice_authority(
                core, event, content, message_id, plan.sha256, legacy=voice_v1
            )
            send_check["reply_revision"] = event["reply_revision"]
        if legacy:
            core.store.execute('DELETE FROM runtime_meta WHERE key=%s',
                               ("private_life_delivery:" + message_id,))
            put(core.store, "live_text_reply:" + message_id, {
                "message_id": message_id, "event_id": event["event_id"],
                "job_id": accepted["job_id"], "request_id": request_id,
                "reply_sha256": payload, "plan_sha256": plan.sha256, "privacy_epoch": 0,
            })
        input_receipt = {"window_sha256": plan.sha256, "event_id": event["event_id"],
            "input_sha256": wire.digest("synthetic private source"), "evidence_sha256": "e" * 64,
            "capture": None, "privacy_epoch": 0}
        put(core.store, live_text.INPUT + event["event_id"], input_receipt)
        cumulative.admit_operation(core.store, core.settings, core.clock(), "input",
            event["event_id"], wire.digest(input_receipt))
        cumulative.admit_operation(core.store, core.settings, core.clock(), "send",
            message_id, wire.digest(send_check),
            logical=content["plan_id"] if account else event["event_id"])
    budget.settle(core.store, core.settings, request_id,
                  None if model_unknown else ModelResponse("synthetic", 100, 20))
    if uncertain:
        account_receipt = ({"message_id": message_id, "action_type": action,
            "lease_token": token, "payload_sha256": payload, "result": "UNKNOWN"}
            if account else None)
        delivery_result(core.store, core.settings, message_id, DeliveryResult(
            lease_token=token, result="DELIVERY_UNCERTAIN",
            account_action_receipt=account_receipt), core.clock())
    if expired:
        core.clock.advance(31)
    if stopped:
        live_text.stop_window(core.store, live_text.WindowReference(window_sha256=plan.sha256),
                              core.clock)
    env = SimpleNamespace(core=core, key=key, plan=plan, cfg=cfg, message_id=message_id,
                          token=token, request_id=request_id, proof=None)
    env.proof = {"kind": wire.QUIESCENCE_KIND, "version": 1, "window_id": approval.window_id,
        "plan_sha256": plan.sha256, "budget_journal_id": approval.budget_journal_id,
        "message_id": message_id, "event_id": event["event_id"], "action_type": action,
        "fence": 1, "item_sha256": wire.digest([message_id, content, token]),
        "content_sha256": wire.digest(content) if kind == "content_sha256" else None,
        "payload_sha256": payload, "payload_kind": kind, "lease_sha256": wire.digest(token),
        "send_check_sha256": wire.digest(send_check), "reply_revision": None,
        "attempt_state": "UNKNOWN", "attempt_record_sha256": "1" * 64,
        "reserved_record_sha256": "2" * 64, "stop_record_sha256": "3" * 64,
        "core_stop_record_sha256": "4" * 64, "bridge_journal_head_sha256": "5" * 64,
        "original_result_sha256": wire.digest({"lease_token": token,
                                                "result": "DELIVERY_UNCERTAIN"}),
        "stopped_at": core.clock(), "quiesced_at": core.clock(), "signature": "0" * 128}
    if account:
        env.proof.pop("event_id")
        env.proof.pop("reply_revision")
        env.proof.update(account_plan_id=content["plan_id"], authorization_sha256="a" * 64,
                         content_sha256=wire.digest(content))
    if voice:
        env.proof.update(voice_check=send_check["voice"], reply_revision=event["reply_revision"],
                         content_sha256=wire.digest(content))
    env.proof = signed(env)
    return env


def account_content(core, action, message_id, event_id):
    from companion.social_autonomy_protocol import FrozenPlan

    kind = action.rsplit(".", 1)[-1]
    proposal = ({"kind": "qzone_publish", "text": "synthetic public source",
                 "source_refs": [event_id]} if kind == "publish" else
                {"kind": "avatar", "asset_id": "synthetic_reviewed_asset"} if kind == "avatar"
                else {"kind": kind, "value": "synthetic profile"})
    material = {"schema_version": 1, "plan_id": uid("social"), "message_id": message_id,
        "delivery_key": "synthetic-original:" + message_id, "action_type": action,
        "policy_version": 1, "policy_sha256": "c" * 64, "authorization_sha256": "a" * 64,
        "bot_qq_id": core.settings.bot_qq_id, "owner_qq_id": core.settings.owner_qq_id,
        "proposal": proposal, "source_snapshot_sha256": "d" * 64,
        "profile_baseline_sha256": None if kind == "publish" else "e" * 64,
        "asset_sha256": "f" * 64 if kind == "avatar" else None,
        "audience": {"ugc_right": 16, "target_uins": [core.settings.owner_qq_id]}
            if kind == "publish" else None, "expires_at": after(core.clock(), 60)}
    return FrozenPlan(**material, payload_sha256=wire.digest(material)).model_dump(mode="json")


def account_authority(core, content, window_sha256):
    core.store.insert("social_autonomy_plans", plan_id=content["plan_id"],
        idempotency_key=content["plan_id"], request_sha256="1" * 64, candidate_sha256="2" * 64,
        state="READY", policy_version=1, policy_sha256=content["policy_sha256"],
        authorization_sha256=content["authorization_sha256"], binding_sha256="3" * 64,
        timezone="UTC", policy_json="{}", input_snapshot_json="{}",
        source_snapshot_sha256=content["source_snapshot_sha256"], plan_json=encode(content),
        payload_sha256=content["payload_sha256"], created_at=core.clock(),
        expires_at=content["expires_at"], updated_at=core.clock())
    core.store.insert("social_autonomy_reservations", plan_id=content["plan_id"],
        message_id=content["message_id"], action_type=content["action_type"],
        semantic_sha256="4" * 64, reserved_day=core.clock()[:10], reserved_at=core.clock(),
        dispatched_at=core.clock())
    put(core.store, "account_action_delivery:" + content["message_id"], {
        "plan_sha256": window_sha256, "message_id": content["message_id"],
        "authorization_sha256": content["authorization_sha256"],
        "payload_sha256": content["payload_sha256"], "content_sha256": wire.digest(content),
    })


def voice_authority(core, event, content, message_id, window_sha256, *, legacy=False):
    from companion.voice_protocol import LegacyVoiceDeliveryBinding

    job_id = uid("voice")
    delivery_key = "synthetic-original:" + message_id
    binding = LegacyVoiceDeliveryBinding(
        retirement_token="random-proof-" + "x" * 32,
        job_id=job_id,
        generation=1,
        message_id=message_id,
        delivery_key=delivery_key,
        event_id=event["event_id"],
        window_sha256=window_sha256,
        source_sha256="1" * 64,
        identity_sha256="2" * 64,
        privacy_epoch=0,
        reply_revision=event["reply_revision"],
        node_id="local",
        manifest_sha256="3" * 64,
        node_config_sha256="4" * 64,
        voice_policy_sha256="b" * 64,
        content_sha256=wire.digest(content),
        reply_sha256=wire.digest(content["text"]),
        result_sha256="5" * 64,
        audio_sha256="6" * 64,
        audio_bytes=100,
        expires_at=after(core.clock(), 60),
    ).model_dump(mode="json")
    if legacy:
        binding.pop("retirement_token")
        binding["schema_version"] = 1
    core.store.insert("voice_jobs", job_id=job_id, operation="tts", event_id=event["event_id"],
        native_source_event_id=event["source_event_id"], conversation_id=event["conversation_id"],
        message_id=message_id, source_sha256=binding["source_sha256"],
        identity_sha256=binding["identity_sha256"], window_sha256=window_sha256,
        captured_privacy_epoch=0, captured_reply_revision=event["reply_revision"],
        outbox_content_sha256=wire.digest(content), outbox_delivery_key=delivery_key,
        node_id="local", manifest_sha256=binding["manifest_sha256"],
        config_sha256=binding["node_config_sha256"], config_json="{}", generation=1,
        state="READY", input_mime="text/plain", input_sha256="7" * 64, input_bytes=20,
        source_partial=0, result_sha256=binding["result_sha256"], result_bytes=100,
        created_at=core.clock(), updated_at=core.clock(), deadline_at=after(core.clock(), 60),
        result_expires_at=after(core.clock(), 60))
    put(core.store, "voice_reply:" + message_id, {"state": "AUDIO", "job_id": job_id,
        "binding": binding, "binding_sha256": wire.digest(binding)})
    return {"binding_sha256": wire.digest(binding),
            **{k: binding[k] for k in wire.VOICE_CHECK_FIELDS - {"binding_sha256"} if k in binding}}


def decide(env, outcome="CANCELLED", *, operation_id="owner-fixture-op", operation_sha256=None):
    return reconciliation.resolve(env.core.store, env.core.settings, env.message_id, outcome,
        env.core.clock(), operation_id=operation_id,
        operation_sha256=operation_sha256 or wire.digest([env.message_id, outcome]))


def prepare(env):
    return reconciliation.prepare(env.core.store, env.core.settings, env.proof, env.core.clock())
