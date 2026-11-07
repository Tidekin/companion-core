import json

import pytest

from companion import console_media_views, paid_budget
from companion.social_autonomy_protocol import ProfileBaseline
from companion.store import after, encode
from companion.voice_protocol import VoiceLeaseRef
from tests.support.voice_jobs import TEXT, TOKEN, claim, create_asr, create_tts, prepared, result
from tests.test_owner_console import client as client  # noqa: F401 -- pytest fixture
from tests.test_owner_console import login
from tests.test_social_autonomy import (
    avatar,
    background,
    dispatch,
    outcome,
    planned,
    queue,
    service,
)


def view(client, route):
    response = client.get("/v1/console/" + route)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_media_views_require_owner_session_and_read_only_empty_state(client, core):
    for route in ("voice", "account-actions"):
        assert client.get("/v1/console/" + route).status_code == 401
        assert (
            client.get(
                "/v1/console/" + route,
                headers={
                    "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()
                },
            ).status_code
            == 401
        )
    login(client)
    before = client.app_core.store.db.total_changes
    assert view(client, "voice")["items"] == []
    accounts = view(client, "account-actions")
    assert accounts["meta"]["enabled"] is False
    assert len(accounts["items"]) == 3
    assert all(item["status"] == "UNOBSERVED" for item in accounts["items"])
    assert client.app_core.store.db.total_changes == before
    assert not core.model.calls


def test_voice_projection_separates_node_contact_task_and_original_delivery(
    client, core, monkeypatch
):
    jobs, message_id, event_id, key = prepared(core, monkeypatch)
    job = create_tts(core, jobs, message_id)
    _, lease = claim(jobs)
    ref = VoiceLeaseRef.from_lease(lease.lease)
    jobs.input("local", ref)
    value = result(ref)
    jobs.submit("local", value)
    create_asr(core, jobs, event_id, key, partial=True)
    core.store.execute("UPDATE outbox SET state='DELIVERY_UNCERTAIN',attempt_count=1")
    login(client)
    before = client.app_core.store.db.total_changes
    projected = view(client, "voice")
    assert client.app_core.store.db.total_changes == before
    nodes = [item for item in projected["items"] if item["kind"] == "voice_node"]
    assert nodes[0]["status"] == "BUSY"
    assert nodes[0]["detail"]["verified_manifest_sha256"] == "b" * 64
    assert nodes[0]["detail"]["expected_manifest_sha256"] == "b" * 64
    audio = next(item for item in projected["items"] if item["id"] == job["job_id"])
    assert audio["status"] == "DELIVERY_UNCERTAIN" and audio["detail"]["task_state"] == "READY"
    assert any(item["detail"].get("partial") is True for item in projected["items"])
    encoded = encode(projected)
    for private in (
        TEXT,
        TOKEN,
        "61" * 32,
        value.payload.audio_base64,
        "audio_base64",
        "input_blob",
    ):
        assert private not in encoded
    assert projected["meta"]["uncertain_deliveries"] == 1
    core.clock.advance(61)
    assert (
        next(item for item in view(client, "voice")["items"] if item["kind"] == "voice_node")[
            "status"
        ]
        == "OFFLINE"
    )


def test_voice_empty_poll_does_not_invent_manifest_declaration(client, core, monkeypatch):
    jobs, _, _, _ = prepared(core, monkeypatch)
    claim(jobs)
    login(client)
    node = view(client, "voice")["items"][0]
    assert node["status"] == "ONLINE"
    assert node["detail"]["manifest_state"] == "UNOBSERVED"
    assert node["detail"]["verified_manifest_sha256"] is None


@pytest.mark.parametrize(
    "native,expected",
    [
        ("SUCCEEDED", "SUCCEEDED"),
        ("UNKNOWN", "DELIVERY_UNCERTAIN"),
        ("REJECTED", "REJECTED"),
    ],
)
def test_account_view_uses_native_receipt_and_public_provenance(client, core, native, expected):
    module, source, conversation = background(core)
    plan = planned(module, source)
    frozen = queue(module, plan, conversation)
    token = dispatch(module, frozen)
    outcome(module, frozen, token, native)
    login(client)
    before = client.app_core.store.db.total_changes
    ledger = core.store.rows("SELECT * FROM model_usage")
    item = next(
        item for item in view(client, "account-actions")["items"] if item["id"] == plan["plan_id"]
    )
    assert item["status"] == expected
    assert item["detail"]["receipt_state"] == native
    assert item["detail"]["sources"][0]["status"] == "FETCHED"
    assert item["detail"]["sources"][0]["url"].startswith("https://")
    if native == "SUCCEEDED":
        assert item["detail"]["resource_id"] == "synthetic-real-tid"
    else:
        assert item["detail"]["resource_id"] is None
    assert client.app_core.store.db.total_changes == before
    assert core.store.rows("SELECT * FROM model_usage") == ledger


@pytest.mark.parametrize("receipt", [{}, {"result": "SUCCEEDED"}])
def test_manual_sent_or_incomplete_receipt_is_not_platform_success(client, core, receipt):
    module, source, conversation = background(core)
    plan = planned(module, source)
    queue(module, plan, conversation)
    core.store.execute("UPDATE outbox SET state='SENT'")
    core.store.execute(
        "UPDATE social_autonomy_reservations SET receipt_json=?,receipt_sha256=?",
        (encode(receipt), paid_budget.sha(receipt)),
    )
    login(client)
    item = next(
        item for item in view(client, "account-actions")["items"] if item["kind"] == "account_plan"
    )
    assert item["status"] == "UNVERIFIED"
    assert item["detail"]["delivery_state"] == "SENT"
    assert item["detail"]["receipt_state"] in {"INVALID", "UNOBSERVED"}


def test_profile_freshness_asset_license_and_secret_redaction(client, core):
    module = service(core, nickname_enabled=True, avatar_enabled=True, asset_policy_sha256="e" * 64)
    module.record_profile(
        ProfileBaseline(
            bot_qq_id=core.settings.bot_qq_id,
            field="nickname",
            value="当前昵称 " + core.settings.control_token.get_secret_value(),
            observed_at=core.clock(),
            receipt_sha256="f" * 64,
        )
    )
    module.register_asset(avatar(core))
    login(client)
    value = view(client, "account-actions")
    profile = next(item for item in value["items"] if item["id"] == "profile:nickname")
    assert profile["status"] == "FRESH"
    assert core.settings.control_token.get_secret_value() not in encode(value)
    asset = next(item for item in value["items"] if item["kind"] == "account_asset")
    assert asset["detail"]["license_label"] == "CC0-1.0"
    assert asset["detail"]["usage_conditions"] == "允许作为账号头像使用"
    assert asset["status"] == "REVIEWED"
    core.clock.advance(301)
    value = view(client, "account-actions")
    assert (
        next(item for item in value["items"] if item["id"] == "profile:nickname")["status"]
        == "STALE"
    )
    core.store.execute("UPDATE social_autonomy_assets SET revoked_at=?", (core.clock(),))
    assert (
        next(
            item
            for item in view(client, "account-actions")["items"]
            if item["kind"] == "account_asset"
        )["status"]
        == "REVOKED"
    )


def test_cooldown_uses_native_capacity_without_creating_action(client, core):
    module, source, conversation = background(
        core, qzone_daily_limit=10, qzone_cooldown_seconds=3600
    )
    frozen = queue(module, planned(module, source), conversation)
    token = dispatch(module, frozen)
    outcome(module, frozen, token, "SUCCEEDED")
    login(client)
    projected = view(client, "account-actions")
    publish = next(
        item for item in projected["meta"]["limits"] if item["action_type"] == "qzone.publish"
    )
    assert publish["state"] == "BLOCKED" and publish["reason_code"] == "SOCIAL_ACTION_COOLDOWN"
    assert publish["next_after"] == after(core.clock(), 3600)
    assert core.store.one("SELECT COUNT(*) AS count FROM outbox")["count"] == 1


def test_projection_never_returns_private_plan_snapshot(client, core):
    module, source, _ = background(core)
    plan = planned(module, source)
    private = "private-current-dialogue-never-display"
    core.store.execute(
        "UPDATE social_autonomy_plans SET input_snapshot_json=? WHERE plan_id=?",
        (json.dumps({"private": private}), plan["plan_id"]),
    )
    login(client)
    assert private not in encode(view(client, "account-actions"))
    assert "input_snapshot_json" not in encode(console_media_views.account_actions(core))
