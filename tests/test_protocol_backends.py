import json
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from astrbot_plugin_companion_bridge.bridge import Spool, normalize
from companion.contracts import CoreError, InboundEvent
from companion.model import ModelResponse
from scripts.probe_onebot import probe
from tests.conftest import run
from tests.support.protocol_backends import astrbot_event


@pytest.mark.parametrize("backend,epoch", [("napcat", ""), ("snowluma", "snow-20261019-01")])
def test_standard_onebot_inbox_path_is_backend_independent(core, tmp_path, backend, epoch):
    spool = Spool(str(tmp_path / "bridge.db"))
    try:
        spool.bind_backend(backend, epoch)
        key, payload = normalize(astrbot_event(), "10001", "10002", protocol_epoch=epoch)
        spool.put(key, payload)
        spool.put(key, payload)
        assert spool.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1
        result = core.ingest(InboundEvent.model_validate(payload), key)
        assert not result["duplicate"]
        assert core.ingest(InboundEvent.model_validate(payload), key)["duplicate"]
        assert run(core.run_once("chat"))
        assert core.store.one("SELECT state FROM jobs")["state"] == "SUCCEEDED"
    finally:
        spool.close()


def test_switch_and_rollback_keep_conversation_and_isolate_message_ids(core, tmp_path):
    spool = Spool(str(tmp_path / "bridge.db"))
    keys = []
    try:
        for backend, generation in (
            ("napcat", ""),
            ("snowluma", "snow-new-0001"),
            ("napcat", "rollback-0002"),
        ):
            spool.bind_backend(backend, generation)
            key, payload = normalize(
                astrbot_event(backend + "消息"), "10001", "10002", protocol_epoch=generation
            )
            keys.append(key)
            core.ingest(InboundEvent.model_validate(payload), key)
        assert len(set(keys)) == 3
        assert len(core.store.rows("SELECT * FROM events")) == 3
        assert len(core.store.rows("SELECT * FROM conversations")) == 1
        with pytest.raises(ValueError, match="cannot be reused"):
            spool.bind_backend("snowluma", "snow-new-0001")
        with pytest.raises(ValueError, match="fresh protocol_epoch"):
            spool.bind_backend("napcat", "")
    finally:
        spool.close()


def test_switch_requires_drained_inbox_and_generation_survives_restart(tmp_path):
    path = str(tmp_path / "bridge.db")
    spool = Spool(path)
    spool.bind_backend("napcat", "")
    spool.put(*normalize(astrbot_event(), "10001", "10002"))
    with pytest.raises(ValueError, match="drain"):
        spool.bind_backend("snowluma", "fresh-0001")
    spool.close()
    reopened = Spool(path)
    try:
        reopened.bind_backend("napcat", "")
        with pytest.raises(ValueError, match="drain"):
            reopened.bind_backend("snowluma", "fresh-0001")
        assert reopened.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("segment", ["image", "record", "reply", "json"])
def test_media_and_extension_segments_delegate_wording_without_untrusted_refs(
    core, monkeypatch, segment,
):
    calls = []
    wording = "这是一条由测试模型生成的回复：" + segment

    async def complete(messages, scenario):
        calls.append((messages, scenario))
        return ModelResponse(wording, 100, 20)

    monkeypatch.setattr(core.model, "complete", complete)
    raw = astrbot_event("", segment=segment)
    raw.message_obj.raw_message["message"][0]["data"] = {
        "file": "https://untrusted.example/private-record?token=not-to-fetch"
    }
    key, payload = normalize(raw, "10001", "10002", protocol_epoch="snow-test-01")
    assert payload["payload"] == {"text": "", "media": []}
    assert "untrusted.example" not in json.dumps(payload)
    core.ingest(InboundEvent.model_validate(payload), key)
    assert run(core.run_once("chat"))
    text = json.loads(core.store.one("SELECT content_json FROM outbox")["content_json"])["text"]
    assert text == wording
    assert len(calls) == 1 and calls[0][1] == "chat"
    assert "untrusted.example" not in json.dumps(calls[0][0])
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"


def test_protocol_change_does_not_bypass_owner_or_bot_identity(core):
    event = astrbot_event()
    event.message_obj.self_id = "99999"
    assert normalize(event, "10001", "10002", protocol_epoch="snow-test-01") is None
    event.message_obj.self_id = "10002"
    event.get_sender_id = lambda: "99999"
    assert normalize(event, "10001", "10002") is None
    event.get_sender_id = lambda: "10001"
    key, payload = normalize(event, "10001", "10002", protocol_epoch="snow-test-01")
    payload["actor"]["platform_user_id"] = "99999"
    with pytest.raises(CoreError, match="OWNER_REQUIRED"):
        core.ingest(InboundEvent.model_validate(payload), key)


@pytest.mark.parametrize("app_name", ["NapCat.Onebot", "SnowLuma"])
def test_protocol_probe_only_uses_read_only_actions(app_name):
    calls = []
    data = {
        "get_login_info": {"user_id": 10002, "nickname": "never-export-this"},
        "get_status": {"online": True, "good": True},
        "get_version_info": {"app_name": app_name, "app_version": "test-fixture"},
        "can_send_image": {"yes": True},
        "can_send_record": {"yes": True},
    }

    def respond(request):
        action = request.url.path.lstrip("/")
        calls.append(action)
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data[action]})

    async def exercise():
        async with httpx.AsyncClient(
            base_url="http://onebot.test", transport=httpx.MockTransport(respond)
        ) as client:
            return await probe(client, "10002")

    report = run(exercise())
    assert report["connection_ready"]
    assert report["private_message_delivery_verified"] is False
    assert set(calls) == set(data)
    assert "never-export-this" not in json.dumps(report)


def test_legacy_default_keeps_existing_event_keys():
    key, payload = normalize(astrbot_event(), "10001", "10002")
    assert payload["source_event_id"] == "10002:qq:FriendMessage:10001:42"
    assert normalize(astrbot_event(), "10001", "10002", protocol_epoch="")[0] == key


def test_observation_draft_reserves_consent_and_cannot_enable_camera_input():
    schema = json.loads(Path("contracts/observation.draft.schema.json").read_text("utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    payload = {
        "schema_version": 1,
        "event_id": "observation_example",
        "event_type": "vision.observed",
        "occurred_at": "2026-10-19T00:00:00+00:00",
        "source_event_ids": ["evt_example"],
        "session_id": "session_example",
        "device_id": "owner-camera",
        "consent_id": "consent_example",
        "privacy_scope": "private",
        "raw_media_retention": "discard",
        "observation": "桌上有一本书",
        "confidence": 0.8,
        "source_sha256": "a" * 64,
    }
    validator.validate(payload)
    for altered in (
        {**payload, "raw_media_retention": "keep"},
        {**payload, "consent_id": ""},
        {**payload, "privacy_scope": "public"},
    ):
        assert list(validator.iter_errors(altered))
    with pytest.raises(ValidationError):
        InboundEvent.model_validate(payload)
