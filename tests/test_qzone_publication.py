"""Exact public review, native approval receipts, source separation and delivery fencing."""

import json
from contextlib import closing

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from companion import qzone_publication as publication
from companion.api import create_app
from companion.backup import backup, restore
from companion.contracts import CoreError, LeaseRequest
from companion.social_autonomy import SocialAutonomy
from companion.store import Store, after
from companion.transport import begin_send, lease_deliveries
from tests.conftest import run
from tests.support.owner_console import ORIGIN, login
from tests.support.owner_console import client as client
from tests.support.qzone_publication import setup
from tests.support.social_owner_authority import close_provider_clients, stop  # noqa: F401


def ready(core, tmp_path):
    module, body, ref, source = setup(core, tmp_path)
    result = publication.prepare(module, body, "prepare-publication")
    return module, result, ref, source


def approval(value, decision="APPROVE"):
    return publication.PublicationDecision(expected_payload_sha256=value["payload_sha256"],
        decision=decision, source_sharing_confirmed=decision == "APPROVE")


def test_prepare_is_review_only_and_approve_reserves_once_without_another_model_call(
    core, tmp_path,
):
    module, body, ref, _ = setup(core, tmp_path)
    usage = core.store.rows("SELECT * FROM model_usage")
    result = publication.prepare(module, body, "prepare-publication")
    assert result["state"] == "WAITING_APPROVAL" and result["text"]
    assert result["audience"] == "public" and result["sources"]
    row = core.store.one("SELECT * FROM social_autonomy_plans")
    frozen = module._frozen(row)
    assert frozen.schema_version == 2 and frozen.audience.ugc_right == 1
    assert not core.store.rows("SELECT * FROM outbox")
    with core.store.transaction(), pytest.raises(CoreError, match="APPROVAL_REQUIRED"):
        module.reserve(result["plan_id"], body.job_id)
    again = publication.prepare(module, body, "prepare-publication")
    assert again == result
    sent = publication.decide(module, result["plan_id"], approval(result), "approve-publication")
    assert sent["state"] == "PENDING"
    repeated = publication.decide(module, result["plan_id"], approval(result),
                                   "approve-publication")
    assert repeated == sent
    assert len(core.store.rows("SELECT * FROM outbox")) == 1
    assert core.store.rows("SELECT * FROM model_usage") == usage
    assert json.loads(core.store.one("SELECT authority_json FROM outbox")["authority_json"]) == ref


def test_edit_invalidates_stale_confirmation_and_approved_content_is_immutable(core, tmp_path):
    module, result, _, _ = ready(core, tmp_path)
    edit = publication.PublicationEdit(expected_payload_sha256=result["payload_sha256"],
        text="主人审阅后保留的公开表达。", expires_at=after(core.clock(), 600))
    updated = publication.edit(module, result["plan_id"], edit, "edit-publication")
    assert updated["payload_sha256"] != result["payload_sha256"]
    assert updated["text"] == edit.text and updated["state"] == "WAITING_APPROVAL"
    with pytest.raises(CoreError, match="QZONE_PUBLICATION_CHANGED"):
        publication.decide(module, result["plan_id"], approval(result), "stale-publication")
    publication.decide(module, result["plan_id"], approval(updated), "approve-edited-publication")
    with pytest.raises(CoreError, match="ALREADY_RESERVED"):
        publication.edit(module, result["plan_id"], edit.model_copy(update={
            "expected_payload_sha256": updated["payload_sha256"]}), "edit-approved-publication")
    assert len(core.store.rows("SELECT * FROM outbox")) == 1


@pytest.mark.parametrize("action", ["REJECT", "REVOKE"])
def test_negative_decisions_never_publish_and_do_not_rearm_on_replay(core, tmp_path, action):
    module, result, _, _ = ready(core, tmp_path)
    if action == "REVOKE":
        publication.decide(module, result["plan_id"], approval(result), "approve-publication")
    changed = publication.decide(module, result["plan_id"], approval(result, action),
                                  "cancel-publication")
    assert changed["state"] == "CANCELLED"
    assert publication.decide(module, result["plan_id"], approval(result, action),
                              "cancel-publication")["state"] == "CANCELLED"
    assert not core.store.rows("SELECT * FROM outbox WHERE state<>'CANCELLED'")
    with core.store.transaction(), pytest.raises(CoreError):
        module.reserve(result["plan_id"], "not-an-owner-conversation")


@pytest.mark.parametrize("change", ["source", "stop", "expiry", "disabled"])
def test_final_approval_is_bound_to_current_source_authority_and_expiry(core, tmp_path, change):
    module, result, ref, source = ready(core, tmp_path)
    if change == "source":
        with core.store.transaction():
            core.ledger.expire_events(core.store, [source], core.clock())
    elif change == "stop":
        stop(core, ref)
    elif change == "expiry":
        core.clock.advance(3601)
    else:
        core.settings.social_autonomy = core.settings.social_autonomy.model_copy(
            update={"qzone_public_enabled": False})
    with pytest.raises(CoreError):
        publication.decide(module, result["plan_id"], approval(result), "invalid-publication")
    assert not core.store.rows("SELECT * FROM outbox")


def test_private_life_draft_cannot_receive_a_public_sharing_override(core, tmp_path):
    module, body, _, _ = setup(core, tmp_path, private=True)
    with pytest.raises(CoreError, match="SOCIAL_PUBLIC_SOURCE_REQUIRED"):
        publication.prepare(module, body, "private-publication")
    assert not core.store.rows("SELECT * FROM social_autonomy_plans")


def test_decision_route_is_owner_only_and_checks_exact_body(core, tmp_path):
    module, result, _, _ = ready(core, tmp_path)
    core.settings.control_token = SecretStr("synthetic-publication-control-" * 2)
    app = create_app(core.settings)
    app.state.core = core

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://core.test") as client:
            route = "/internal/v2/qzone/publications/" + result["plan_id"] + "/decision"
            body = approval(result).model_dump(mode="json")
            assert (await client.post(route, json=body)).status_code == 401
            headers = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value(),
                       "Idempotency-Key": "http-approve-publication"}
            assert (await client.post(route, headers=headers,
                json={**body, "source_sharing_confirmed": False})).status_code == 400
            assert (await client.post(route, headers=headers,
                json={**body, "audience": "friends"})).status_code == 400
            accepted = await client.post(route, headers=headers, json=body)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["state"] == "PENDING"

    run(scenario())


def test_revocation_after_begin_send_preserves_uncertainty_and_final_fence(core, tmp_path):
    module, result, ref, _ = ready(core, tmp_path)
    publication.decide(module, result["plan_id"], approval(result), "approve-publication")
    request = LeaseRequest(worker_id="public-worker", authority=ref, delivery_lane="qq_account",
        account_actions_authorization_sha256=module.config.authorization_sha256)
    item = lease_deliveries(core.store, core.settings, request, core.clock())["items"][0]
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock(), core.settings,
               clock=core.clock)
    publication.decide(module, result["plan_id"], approval(result, "REVOKE"), "revoke-after-begin")
    row = core.store.one("SELECT state FROM outbox")
    assert row["state"] == "DELIVERY_UNCERTAIN"
    with core.store.transaction(), pytest.raises(CoreError):
        module.mark_dispatched(item["message_id"], item["lease_token"])
    assert core.store.one("SELECT released_at FROM social_autonomy_reservations")[
        "released_at"] is None


def test_console_approval_requires_session_origin_csrf_and_atomic_owner_check(
    client, core, tmp_path,
):
    # Prepare before login because the fixture advances its native clock for the Life worker.
    _, result, _, _ = ready(core, tmp_path)
    route = "/v1/console/qzone/publications/" + result["plan_id"] + "/decision"
    body = approval(result).model_dump(mode="json")
    assert client.post(route, json=body).status_code == 401
    headers = login(client)
    assert client.get("/v1/console/qzone/publications").headers["cache-control"] == "no-store"
    assert client.post(route, json=body, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post(route, json=body, headers={**headers,
        "Origin": "https://attacker.example"}).status_code == 403
    accepted = client.post(route, json=body, headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"] == "PENDING"
    assert client.post(route, json=body, headers=headers).json() == accepted.json()
    assert client.post(route, json={**body, "decision": "REVOKE"},
                       headers=headers).status_code == 409
    assert len(client.app_core.store.rows("SELECT * FROM outbox")) == 1


def test_public_review_is_never_auto_enqueued(core, tmp_path):
    from companion import social_autonomy_runtime

    ready(core, tmp_path)
    run(social_autonomy_runtime.run_once(core.store, core.settings, None, core.clock))
    assert not core.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("erase", [False, True])
def test_restore_and_erasure_cannot_rearm_a_public_approval(core, tmp_path, erase):
    module, result, _, source = ready(core, tmp_path)
    publication.decide(module, result["plan_id"], approval(result), "approve-publication")
    archive, target, key = tmp_path / "public.enc", tmp_path / "restored.db", Fernet.generate_key()
    backup(core.settings.database, archive, key)
    if erase:
        with core.store.transaction():
            core.ledger.expire_events(core.store, [source], core.clock())
    restore(archive, target, key, core.settings.erasure_ledger)
    with closing(Store(str(target))) as store:
        restored = SocialAutonomy(store, core.settings, clock=core.clock)
        assert store.one("SELECT state FROM outbox")["state"] == (
            "CANCELLED" if erase else "DELIVERY_UNCERTAIN")
        assert publication.view(restored, result["plan_id"])["text"] == ""
        with pytest.raises(CoreError):
            publication.decide(restored, result["plan_id"], approval(result),
                               "new-restored-approve")
        if erase:
            assert store.one("SELECT redacted,plan_json,input_snapshot_json "
                             "FROM social_autonomy_plans") == {
                "redacted": 1, "plan_json": "{}", "input_snapshot_json": "{}"}
        assert not store.rows("PRAGMA foreign_key_check")


def test_console_rechecks_rotated_credentials_inside_the_approval_transaction(
    client, core, tmp_path, monkeypatch,
):
    _, result, _, _ = ready(core, tmp_path)
    headers = login(client)
    original = client.app.state.api_store.run

    async def rotate_then_apply(operation):
        core.settings.control_token = SecretStr("rotated-before-public-approval-" * 2)
        return await original(operation)

    monkeypatch.setattr(client.app.state.api_store, "run", rotate_then_apply)
    route = "/v1/console/qzone/publications/" + result["plan_id"] + "/decision"
    assert client.post(route, json=approval(result).model_dump(mode="json"),
                       headers=headers).status_code == 401
    assert not core.store.rows("SELECT * FROM outbox")
    assert not core.store.rows("SELECT * FROM audit_log WHERE action='qzone.publication_decision'")
