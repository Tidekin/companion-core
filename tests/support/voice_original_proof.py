import hashlib
import json

import companion_delivery_protocol as wire
from companion import paid_budget
from companion.voice_protocol import LegacyVoiceDeliveryBinding, VoiceDeliveryBinding


def original(*, legacy=False, retired=False):
    scope = {"window_sha256": "a" * 64} if legacy else {"authority": {
        "version": 2, "grant_id": "grant_" + "a" * 32, "revision": 1,
        "policy_sha256": "b" * 64, "restore_generation": "original",
    }}
    content = {"text": "original reply"}
    binding = dict(
        retirement_token="r" * 32, job_id="voice_" + "a" * 32, generation=1,
        message_id="msg_" + "a" * 32, delivery_key="original-delivery",
        event_id="original-event", source_sha256="a" * 64, identity_sha256="b" * 64,
        privacy_epoch=0, reply_revision=1, node_id="original-node",
        manifest_sha256="c" * 64, node_config_sha256="d" * 64,
        voice_policy_sha256="e" * 64, content_sha256=wire.digest(content),
        reply_sha256=paid_budget.sha(content["text"]),
        result_sha256="f" * 64, audio_sha256="a" * 64, audio_bytes=44,
        expires_at="2000-01-01T00:00:00Z", **scope,
    )
    codec = LegacyVoiceDeliveryBinding if legacy else VoiceDeliveryBinding
    binding = codec.model_validate(binding).model_dump(mode="json")
    given = {k: binding[k] for k in wire.VOICE_CHECK_FIELDS - {"binding_sha256"}}
    given["binding_sha256"] = wire.digest(binding)
    receipt = dict(state="AUDIO", binding=binding, job_id=binding["job_id"],
                   binding_sha256=wire.digest(binding))
    job = {k: binding[k] for k in (
        "job_id", "generation", "message_id", "event_id", "result_sha256",
        "source_sha256", "identity_sha256", "node_id", "manifest_sha256",
    )}
    job.update(operation="tts", state="EXPIRED", outbox_content_sha256=binding["content_sha256"],
               outbox_delivery_key=binding["delivery_key"], captured_privacy_epoch=0,
               captured_reply_revision=1, config_sha256=binding["node_config_sha256"],
               result_expires_at=binding["expires_at"],
               authority_json=json.dumps(scope.get("authority", {})))
    if legacy:
        job.update(scope)
    if retired:
        receipt = dict(state="FENCED", job_id=binding["job_id"], retired={
            **{k: binding[k] for k in ("job_id", "generation", "message_id", "event_id",
                "delivery_key", "voice_policy_sha256", "reply_revision")}, **scope,
            "kind": "capability", "verifier": hashlib.sha256(b"r" * 32).hexdigest(),
        })
        job["authority_json"] = "{}"  # retirement erases execution authority
    return dict(receipt=receipt, job=job,
                proof=dict(voice_check=given, event_id=binding["event_id"], reply_revision=1),
                outbox=dict(message_id=binding["message_id"], delivery_key=binding["delivery_key"],
                            content_json=json.dumps(content) if not retired else "{}"),
                content_sha256=binding["content_sha256"], original_scope=scope,
                voice_policy_sha256=binding["voice_policy_sha256"])
