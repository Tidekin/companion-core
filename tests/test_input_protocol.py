"""Authenticated lifecycle controls use the real API and independently opened state."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import input_protocol, input_recall, live_text, owner_authority
from companion.config import LiveTextHandoff
from companion.store import encode, uid
from companion_delivery_protocol.authority import recall_signing_payload
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.rich_input import input_digest
from tests import native_input_protocol_fixture as native
from tests.conftest import run
from tests.support import onebot_window as window_fixtures
from tests.support.input_protocol import sign
from tests.support.onebot_capture import pinned_plan
from tests.support.onebot_events import raw_event
from tests.support.private_companion import companion_options


async def admit(client, config, plan, now, number=1):
    raw = raw_event(number, time=int(now.timestamp()), text="合成主人输入" + str(number))
    event = normalize(json.dumps(raw), config, now=now.timestamp())
    capture = sign(config, {
        "kind": "authenticated_onebot_ws", "window_sha256": plan.sha256,
        "event_id": event["event_id"], "source_event_id": event["source_event_id"],
        "input_sha256": input_digest(event["payload"]),
        "frame_sha256": hashlib.sha256(encode(raw).encode()).hexdigest(),
        "evidence_sha256": "e" * 64, "identity_sha256": plan.value["identity_sha256"],
        "config_sha256": plan.value["config_sha256"], "protocol_epoch": config.protocol_epoch,
        "received_at": now.isoformat(), "occurred_at": event["occurred_at"],
    }, live_text.CAPTURE_SIGNATURE_DOMAIN)
    result = await client.post("/internal/v1/live-text/inputs/approve", json={
        "window_sha256": plan.sha256, "event_id": event["event_id"],
        "input_sha256": capture["input_sha256"], "evidence_sha256": "f" * 64,
        "capture": capture,
    })
    assert result.status_code == 200, result.text
    result = await client.post("/internal/v1/inbox/events", json=event,
                               headers={"X-Idempotency-Key": event["event_id"]})
    return event, result


def test_recall_erases_queued_input_once_and_next_input_still_admits(core, tmp_path, monkeypatch):
    async def scenario():
        async with native.environment(core, tmp_path, monkeypatch) as (client, config, plan, now):
            event, accepted = await native.admit(client, config, plan, now)
            assert accepted.status_code == 202, accepted.text
            body = native.notice(config, plan, now)
            first = await client.post(native.BASE + "/inputs/recall", json=body)
            assert first.status_code == 200 and first.json()["state"] == "RECALLED", first.text
            assert first.json()["duplicate"] is False
            before = core.ledger.path.read_bytes()
            repeat = await client.post(native.BASE + "/inputs/recall", json=body)
            assert repeat.status_code == 200 and repeat.json()["duplicate"], repeat.text
            assert core.ledger.path.read_bytes() == before
            state = await native.state(client, plan, event)
            assert state.json()["state"] == "RECALLED"
            assert not state.json()["unsettled_model"] and not state.json()["unsettled_delivery"]
            assert core.store.one("SELECT body_json,redacted FROM events") == {
                "body_json": "{}", "redacted": 1,
            }
            _, accepted = await native.admit(client, config, plan, now, 2)
            assert accepted.status_code == 202, accepted.text
            active = owner_authority.current(core.store, core.settings, core.clock)
            assert active.authority.as_dict() == plan.authority
    run(scenario())


@pytest.mark.parametrize("mutation", ["signature", "epoch", "source"])
def test_recall_rejects_invalid_authentication_or_source(core, tmp_path, monkeypatch, mutation):
    async def scenario():
        async with native.environment(core, tmp_path, monkeypatch) as (client, config, plan, now):
            _, accepted = await native.admit(client, config, plan, now)
            assert accepted.status_code == 202, accepted.text
            body = native.notice(config, plan, now)
            if mutation == "signature":
                body["signature"] = "0" * 128
            elif mutation == "epoch":
                body = native.notice(replace(config, protocol_epoch="previous-epoch"), plan, now)
            else:
                unsigned = {key: value for key, value in body.items() if key != "signature"}
                unsigned["target_source_event_id"] = "99999:" + body[
                    "target_source_event_id"].split(":", 1)[1]
                body = unsigned | {
                    "signature": plan.key.sign(recall_signing_payload(unsigned)).hex()}
            result = await client.post(native.BASE + "/inputs/recall", json=body)
            assert result.status_code == 403, result.text
            assert core.store.one("SELECT redacted FROM events") == {"redacted": 0}
    run(scenario())


def test_notice_before_input_commits_tombstone_and_returns_authenticated_410(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with native.environment(core, tmp_path, monkeypatch) as (client, config, plan, now):
            result = await client.post(native.BASE + "/inputs/recall",
                                       json=native.notice(config, plan, now))
            assert result.status_code == 200 and result.json()["state"] == "PENDING_TARGET"
            event, result = await native.admit(client, config, plan, now)
            assert result.status_code == 410 and result.json() == {"error": "EVENT_ERASED"}
            assert core.store.rows("SELECT * FROM events") == []
            assert core.store.rows("SELECT * FROM jobs") == []
            state = await native.state(client, plan, event)
            assert state.json()["state"] == "RECALLED"
            _, accepted = await native.admit(client, config, plan, now, 2)
            assert accepted.status_code == 202, accepted.text
            active = owner_authority.current(core.store, core.settings, core.clock)
            assert active.authority.as_dict() == plan.authority
    run(scenario())


def test_prebegin_cancellation_requires_exact_lease_and_retains_attempt(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with native.environment(core, tmp_path, monkeypatch) as (client, config, plan, now):
            _, accepted = await native.admit(client, config, plan, now)
            assert accepted.status_code == 202, accepted.text
            item = await native.reply(core, client, plan)
            token = item["lease_token"]
            _, accepted = await native.admit(client, config, plan, now, 2)
            assert accepted.status_code == 202, accepted.text
            body = {"authority": plan.authority, "message_id": item["message_id"],
                    "content_sha256": item["content_sha256"],
                    "lease_token": uid("lease"), "reply_revision": item["reply_revision"]}
            rejected = await client.post(native.BASE + "/cancel-before-send", json=body)
            assert rejected.status_code == 409, rejected.text
            body["lease_token"] = token
            cancelled = await client.post(native.BASE + "/cancel-before-send", json=body)
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["send_attempt_retained"] is True
            repeated = await client.post(native.BASE + "/cancel-before-send", json=body)
            assert repeated.json() == cancelled.json()
            assert core.store.one("SELECT state,attempt_count FROM outbox") == {
                "state": "CANCELLED", "attempt_count": 1,
            }
    run(scenario())


def test_old_capture_uses_verified_same_epoch_handoff_chain_and_original_signing_key(
    core, tmp_path, monkeypatch,
):
    # A historical projection exercises the retained reader without reopening or
    # admitting any retired Window. Current native execution has a separate test.
    now = datetime.now(UTC)
    core.clock.now = now
    monkeypatch.setattr(window_fixtures, "raw_event", lambda: raw_event(time=int(now.timestamp())))
    config, plan = pinned_plan(core, tmp_path, 3001, live=True, approved=True,
                               scope_options=companion_options(core))
    event = normalize(encode(raw_event(1, time=int(now.timestamp()))), config, now=now.timestamp())
    capture = sign(config, {
        "kind": "authenticated_onebot_ws", "window_sha256": plan.sha256,
        "event_id": event["event_id"], "source_event_id": event["source_event_id"],
        "input_sha256": input_digest(event["payload"]), "protocol_epoch": config.protocol_epoch,
        "identity_sha256": plan.value["identity_sha256"],
        "received_at": now.isoformat(), "occurred_at": event["occurred_at"],
    }, live_text.CAPTURE_SIGNATURE_DOMAIN)
    live_text._put(core.store, live_text.INPUT + event["event_id"], {
        "window_sha256": plan.sha256, "event_id": event["event_id"],
        "input_sha256": capture["input_sha256"], "capture": capture})
    prior = core.settings.live_text_window
    successor_sha = "2" * 64
    original = {"approval": prior.model_dump(mode="json"), "state": "RETIRED",
                "successor_plan_sha256": successor_sha}
    live_text._put(core.store, live_text.HISTORY + plan.sha256, original)
    handoff = LiveTextHandoff(
        predecessor_plan_sha256=plan.sha256, predecessor_window_id=prior.window_id,
        budget_journal_head_sha256="a" * 64, bridge_journal_head_sha256="b" * 64,
        identity_sha256=plan.value["identity_sha256"], protocol_epoch=config.protocol_epoch,
        privacy_epoch=0, erasure_ledger_sha256="c" * 64, evidence_sha256="d" * 64)
    rotated = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    successor = prior.model_copy(update={"plan_sha256": successor_sha,
        "window_id": "win_" + "2" * 32, "handoff": handoff, "capture_public_key": rotated})
    source = input_recall.canonical_source(bot_qq_id=config.bot_qq_id,
        owner_qq_id=config.owner_qq_id, protocol_epoch=config.protocol_epoch,
        provider_message_id="1")
    before = list(core.store.db.iterdump())
    assert input_protocol._capture_matches(core.store, core.settings, successor,
                                            source, event["event_id"], now.isoformat())
    stale_epoch = successor.model_copy(update={"handoff": handoff.model_copy(
        update={"protocol_epoch": "previous-epoch"})})
    assert not input_protocol._capture_matches(core.store, core.settings, stale_epoch,
                                                source, event["event_id"], now.isoformat())
    assert list(core.store.db.iterdump()) == before
    original["successor_plan_sha256"] = "3" * 64
    live_text._put(core.store, live_text.HISTORY + plan.sha256, original)
    before = list(core.store.db.iterdump())
    assert not input_protocol._capture_matches(core.store, core.settings, successor,
                                                source, event["event_id"], now.isoformat())
    assert list(core.store.db.iterdump()) == before
    assert core.store.rows("SELECT * FROM events") == []
    assert core.store.rows("SELECT * FROM jobs") == []


def test_native_closed_input_retains_original_key_after_stopped_rotation(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with native.environment(core, tmp_path, monkeypatch) as (client, config, plan, now):
            event, accepted = await native.admit(client, config, plan, now)
            assert accepted.status_code == 202, accepted.text
            item = await native.reply(core, client, plan)
            started = await client.post(f"/internal/v1/deliveries/{item['message_id']}/begin-send",
                                         json={"lease_token": item["lease_token"]})
            assert started.status_code == 200, started.text
            checked = await client.post(native.BASE + "/send-check", json={
                "authority": plan.authority, "message_id": item["message_id"],
                "event_id": event["event_id"], "content_sha256": item["content_sha256"],
                "lease_token": item["lease_token"], "reply_revision": item["reply_revision"]})
            assert checked.status_code == 200, checked.text
            sent = await client.post(f"/internal/v1/deliveries/{item['message_id']}/result", json={
                "lease_token": item["lease_token"], "result": "SENT",
                "provider_message_id": "98765"})
            assert sent.status_code == 200, sent.text
            closed = await native.state(client, plan, event)
            assert closed.status_code == 200 and closed.json()["terminal"], closed.text
            original_usage = core.store.rows("SELECT * FROM model_usage")
            policy = owner_authority.current(core.store, core.settings, core.clock).policy
            headers = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
            stopped = await client.post(native.BASE + "/stop", json={
                "operation_id": "stop-protocol-rotation", "authority": plan.authority},
                headers=headers)
            assert stopped.status_code == 200, stopped.text
            key = Ed25519PrivateKey.generate()
            successor = policy.model_dump(mode="json")
            successor["capture"]["public_key"] = key.public_key().public_bytes_raw().hex()
            query = {"expected_revision": plan.authority["revision"] + 1, "policy": successor}
            preview = await client.post(native.BASE + "/preview", json=query, headers=headers)
            assert preview.status_code == 200, preview.text
            changed = await client.post(native.BASE + "/activate", json={**query,
                "operation_id": "rotate-protocol-key", "preview_sha256": preview.json()[
                    "preview_sha256"]}, headers=headers)
            assert changed.status_code == 200, changed.text
            assert changed.json()["authority"] != plan.authority
            original = plan.captures[event["event_id"]]
            reread = await native.state(client, plan, event)
            assert reread.json() == closed.json()
            replay = await client.post(native.BASE + "/inputs/approve", json=original)
            assert replay.status_code == 409, replay.text
            old_notice = native.notice(config, plan, now)
            unsigned = {k: v for k, v in old_notice.items() if k != "signature"}
            forged = unsigned | {"signature": key.sign(recall_signing_payload(unsigned)).hex()}
            denied = await client.post(native.BASE + "/inputs/recall", json=forged)
            assert denied.status_code == 403, denied.text
            recalled = await client.post(native.BASE + "/inputs/recall", json=old_notice)
            assert recalled.status_code == 200 and recalled.json()["state"] == "RECALLED"
            assert core.store.rows("SELECT * FROM model_usage") == original_usage
    run(scenario())
