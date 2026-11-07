import hashlib
import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from companion import budget
from companion.api import create_app
from companion.backup import backup, restore
from companion.contracts import InboundEvent
from companion.core import Core
from companion.observation import ObservationEvent, ObservationPayload
from companion.privacy import epoch
from companion.transport import digest
from tests.conftest import event, ingest, run
from tests.support.content import POST, enable, png
from tests.support.content_integration import finish_read, share


def test_content_default_off_uses_canonical_payload_fingerprint(core):
    canonical = event(core).model_dump(mode="json", exclude={"event_id", "trace_id"})
    ingest(core)
    assert core.store.one("SELECT fingerprint FROM events")["fingerprint"] == digest(canonical)
    assert ingest(core)["duplicate"]
    omitted = event(core).model_dump(mode="json")
    omitted["payload"].pop("shared_content")
    assert core.ingest(InboundEvent.model_validate(omitted), "key-1")["duplicate"]
    share(core, 2)
    assert not core.store.rows("SELECT * FROM content_items")
    assert not core.store.rows("SELECT * FROM exploration_requests")


def test_shared_manifest_matches_published_json_contract(core):
    from pathlib import Path

    schema = json.loads((Path(__file__).parents[1] / "contracts/event.v1.schema.json").read_text())
    value = event(core).model_dump(mode="json")
    value["payload"]["shared_content"] = [
        {"kind": "link", "url": POST},
        {"kind": "image", "slot": 0},
    ]
    Draft202012Validator(schema).validate(value)
    assert len(InboundEvent.model_validate(value).payload.shared_content) == 2


def test_chat_waits_without_attempts_then_discusses_read_evidence(core):
    enable(core)
    share(core)
    for _ in range(5):
        assert not run(core.run_once())
    assert core.store.one("SELECT attempts FROM jobs")["attempts"] == 0
    result = finish_read(core)
    assert run(core.run_once())
    messages = core.model.calls[-1][0]
    assert result["event_ids"][0] in json.dumps(messages, ensure_ascii=False)
    assert "作者讨论游戏关卡设计" in json.dumps(messages, ensure_ascii=False)
    assert core.store.one("SELECT state FROM jobs")["state"] == "SUCCEEDED"


def test_wait_timeout_yields_honest_unknown_with_single_attempt(core):
    content, _ = enable(core)
    share(core)
    core.clock.advance(content.options.wait_seconds + 1)
    assert run(core.run_once())
    messages = core.model.calls[-1][0]
    assert '"status":"UNKNOWN"' in json.dumps(messages, ensure_ascii=False).replace('\\"', '"')
    assert core.store.one("SELECT attempts FROM jobs")["attempts"] == 1


def test_autonomy_pause_does_not_block_owner_requested_content(core):
    enable(core)
    core.store.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('life_paused','1')")
    share(core)
    assert finish_read(core)["state"] == "DONE"


def test_link_images_do_not_extend_original_reply_wait(core):
    content, _ = enable(core)
    share(core)
    core.clock.advance(content.options.wait_seconds - 1)
    result = finish_read(core)
    content.register_link_images(
        "evt_test_1", result["event_ids"][0], ["https://imgheybox.max-c.com/test.png"]
    )
    assert content.status("evt_test_1")["pending"]
    core.clock.advance(2)
    assert not content.status("evt_test_1")["pending"]


def test_current_content_has_priority_over_chat_history_within_soft_budget(core):
    enable(core)
    for number in range(1, 6):
        ingest(core, number, "很长的旧对话" * 300)
        core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    share(core, 6, "你怎么看这个观点？")
    result = finish_read(core, "当前文章的可靠内容" * 600)
    job = core.claim_job("test")
    messages = core.context(job)
    assert budget.token_upper_bound(messages) <= core.settings.context_soft_tokens
    assert result["event_ids"][0] in messages[-2]["content"]
    assert "当前文章的可靠内容" in messages[-2]["content"]
    assert messages[-1]["content"] == "你怎么看这个观点？"


def test_content_http_image_intake_auth_budget_and_generic_observation_denial(core):
    content, vision = enable(core)
    core.settings.content = content.options
    auth = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
    value = event(core, text="").model_dump(mode="json")
    value["payload"]["shared_content"] = [{"kind": "image", "slot": 0}]
    with TestClient(create_app(core.settings)) as client:
        active = client.app.state.core
        active.content.provider = vision
        accepted = client.post(
            "/internal/v1/inbox/events",
            json=value,
            headers={**auth, "X-Idempotency-Key": "key-1"},
        )
        assert accepted.status_code == 202
        prefix = "/internal/v2/content/evt_test_1"
        assert client.get(prefix).status_code == 401
        assert client.get(prefix, headers=auth).json()["pending"]
        headers = {**auth, "Content-Type": "image/png", "X-Content-Key": "image:0"}
        assert (
            client.post(prefix + "/image", content=b"x" * 1_000_001, headers=headers).status_code
            == 413
        )
        large = client.post(
            prefix + "/image",
            content=b"x" * 200_000,
            headers={**headers, "Content-Type": "application/octet-stream"},
        )
        assert large.status_code == 415
        assert (
            client.post(
                "/internal/v1/inbox/events", content=b"x" * 200_000, headers=auth
            ).status_code
            == 413
        )
        assert (
            client.post(
                prefix + "/image/claim", json={"request_key": "image:0"}, headers=auth
            ).status_code
            == 200
        )
        answer = client.post(prefix + "/image", content=png(), headers=headers)
        assert answer.status_code == 200 and answer.json()["state"] == "SUCCEEDED"
        assert client.post(prefix + "/image", content=png(), headers=headers).json()["duplicate"]
        assert len(vision.calls) == 1
        image = ObservationEvent(
            schema_version=2,
            event_id="obs_bypass_001",
            event_type="content.observed",
            source="qq_image",
            source_kind="image_analysis",
            provider_event_id="bypass",
            occurred_at=active.clock(),
            trace_id="trace_bypass",
            result_status="FETCHED",
            source_event_ids=["evt_test_1"],
            source_sha256=hashlib.sha256(b"text").hexdigest(),
            privacy_epoch=epoch(active.store),
            payload=ObservationPayload(
                text="text",
                media_sha256="a" * 64,
                processor_id="fake",
            ),
        )
        bypass = client.post(
            "/internal/v2/observations/content_image",
            json=image.model_dump(mode="json"),
            headers={
                "Authorization": "Bearer "
                + core.settings.observation_adapters["content_image"].token.get_secret_value()
            },
        )
        assert (
            bypass.status_code == 403 and bypass.json()["error"] == "CONTENT_IMAGE_INTAKE_REQUIRED"
        )


def test_image_only_chat_uses_image_evidence(core):
    content, vision = enable(core)
    share(core, manifest=[{"kind": "image", "slot": 0}])
    result = run(content.analyze_image("evt_test_1", "image:0", png(), "image/png"))
    assert run(core.run_once())
    messages = core.model.calls[-1][0]
    assert result["event_id"] in messages[-2]["content"]
    assert "图片中有一只猫" in messages[-2]["content"]
    assert len(vision.calls) == 1


def test_restore_cannot_restart_pending_content(core, tmp_path):
    content, _ = enable(core)
    core.settings.content = content.options
    share(core, manifest=[{"kind": "image", "slot": 0}])
    key, archive, destination = (
        Fernet.generate_key(),
        tmp_path / "content.enc",
        tmp_path / "restored.db",
    )
    backup(core.settings.database, archive, key)
    restore(archive, destination, key, core.settings.erasure_ledger)
    recovered = Core(
        core.settings.model_copy(update={"database": str(destination)}), clock=core.clock
    )
    try:
        assert not recovered.content.status("evt_test_1")["pending"]
        with pytest.raises(Exception, match="CONTENT_IMAGE_ALREADY_UNKNOWN"):
            recovered.content.claim_upload("evt_test_1", "image:0")
    finally:
        recovered.store.close()
