import json

from companion.model import ModelResponse
from companion.reading_protocol import ReadObservation
from companion.social_autonomy import SocialAutonomy
from companion.social_autonomy_protocol import ActionReceipt, ReviewedAvatar, SocialAutonomyConfig
from companion.store import after, encode, uid
from tests.conftest import ingest, run
from tests.support.reading_protocol import envelope
from tests.support.reading_service import queued


def service(core, **changes):
    config = SocialAutonomyConfig(**{
        "enabled": True, "scope": "qq_social_v1", "authorization_sha256": "a" * 64,
        "qzone_enabled": True, **changes,
    })
    object.__setattr__(core.settings, "social_autonomy", config)
    return SocialAutonomy(core.store, core.settings, core.model, core.clock)


def read_public(core, source="web"):
    adapter, item, body = queued(core, source)
    result = core.reading.complete(adapter, item["request_id"],
                                  ReadObservation.model_validate(body))
    return result["event_ids"][0]


def avatar(core, **changes):
    return ReviewedAvatar(**{
        "asset_id": "avatar_reviewed_one", "asset_sha256": "b" * 64,
        "description": "公开授权的星空插画", "source_page": "https://art.example.test/license",
        "source_url": "https://art.example.test/star.png", "license_label": "CC0-1.0",
        "usage_conditions": "允许作为账号头像使用", "license_sha256": "c" * 64,
        "review_receipt_sha256": "d" * 64, "review_policy_sha256": "e" * 64,
        "purpose": "profile_avatar", "origin": "public_download", "mime_type": "image/png",
        "size_bytes": 1024, "width": 128, "height": 128,
        "reviewed_at": core.clock(), "expires_at": after(core.clock(), 3600), **changes,
    })


class MockPlanner:
    """External model substitute; native budget admission and settlement are unchanged."""

    def __init__(self):
        self.calls = []
        self.proposal = None
        self.text = "公开资料让我对恒星的颜色更好奇了。"
        self.callback = None
        self.error = None

    async def complete(self, messages, scenario):
        assert scenario == "social_autonomy"
        data = json.loads(messages[-1]["content"])
        self.calls.append(data)
        if self.callback:
            self.callback()
        if self.error:
            raise self.error
        proposal = self.proposal or {"kind": "qzone_publish", "text": self.text,
                                     "source_refs": [data["sources"][0]["ref"]]}
        return ModelResponse(encode(proposal), 100, 20)


def background(core, **changes):
    ref = read_public(core, "bilibili")
    accepted = ingest(core, 99, "合成账号绑定", origin="onebot-direct:FriendMessage:10001")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    module = service(core, **{"decision_interval_seconds": 0, "qzone_cooldown_seconds": 0,
                              "profile_cooldown_seconds": 0, **changes})
    module.model = MockPlanner()
    conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                  (accepted["event_id"],))["conversation_id"]
    return module, ref, conversation


def extra_public(core, number=2):
    body = envelope("bilibili")
    with core.store.transaction():
        request = core.exploration.create("bili", "read", f"social-extra-{number}",
                                          body["observations"][0]["url"])
    item = core.reading.lease("bili")
    assert item["request_id"] == request
    body["lease_token"] = item["lease_token"]
    body["observations"][0]["provenance"] = item["context"]
    body["observations"][0]["fetched_at"] = core.clock()
    body["observations"][0]["evidence"]["fetched_at"] = core.clock()
    result = core.reading.complete("bili", request, ReadObservation.model_validate(body))
    return result["event_ids"][0]


def planned(module, ref, key="social-plan-one", **kwargs):
    return run(module.plan(key, source_ids=[ref], **kwargs))


def queue(module, result, conversation):
    with module.store.transaction():
        return module.reserve(result["plan_id"], conversation)


def dispatch(module, frozen):
    """Exercise module transaction hooks against native Outbox states, with no protocol I/O."""
    token = uid("lease")
    with module.store.transaction():
        module.store.execute("UPDATE outbox SET state='LEASED',lease_token=?,lease_until=? "
                             "WHERE message_id=?",
                             (token, after(module.clock(), 60), frozen.message_id))
        module.validate_delivery(frozen.message_id, token)
        module.store.execute("UPDATE outbox SET state='SENDING',attempt_count=attempt_count+1 "
                             "WHERE message_id=?", (frozen.message_id,))
        module.mark_dispatched(frozen.message_id, token)
    return token


def outcome(module, frozen, token, result):
    native = {"SUCCEEDED": "SENT", "REJECTED": "FAILED", "UNKNOWN": "DELIVERY_UNCERTAIN"}[result]
    resource = "synthetic-real-tid" if result == "SUCCEEDED" and frozen.action_type == (
        "qzone.publish"
    ) else None
    native_code = {"SUCCEEDED": 0, "REJECTED": 1400, "UNKNOWN": None}[result]
    response_sha = "9" * 64 if result != "UNKNOWN" else None
    receipt = ActionReceipt(message_id=frozen.message_id, lease_token=token,
                            payload_sha256=frozen.payload_sha256, action_type=frozen.action_type,
                            result=result, native_receipt_sha256=response_sha,
                            native_retcode=native_code,
                            resource_id=resource)
    with module.store.transaction():
        module.store.execute("UPDATE outbox SET state=?,provider_message_id=NULL "
                             "WHERE message_id=?", (native, frozen.message_id))
        return module.record_result(receipt)
