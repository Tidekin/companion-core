"""Authenticated Console owner decisions supply native profile-purpose evidence."""

import json

import pytest
from pydantic import SecretStr

from companion import social_profile_sources
from companion.contracts import CoreError
from companion.memory import Memory
from companion.social_autonomy_protocol import ProfileBaseline
from companion.store import Store, after, encode
from tests.conftest import run
from tests.social_profile_support import confirmed_profile_material
from tests.support.owner_console import ORIGIN, PASSWORD, login
from tests.support.owner_console import client as client  # noqa: F401 -- pytest fixture
from tests.support.social_autonomy import background, dispatch, queue


@pytest.mark.parametrize("decision", ["confirm", "correct"])
def test_authenticated_console_confirmation_is_usable_profile_material(client, core, decision):
    background(core, nickname_enabled=True)
    memory_id = confirmed_profile_material(core, confirm=False)
    body = {"decision": decision}
    if decision == "correct":
        body["corrected_content"] = "我偏爱海洋与简短的表达。"
    headers = login(client)
    response = client.post("/v1/console/memories/" + memory_id + "/decision",
                           json=body, headers=headers)
    assert response.status_code == 200, response.text
    target = response.json()["memory_id"]
    repeat = client.post("/v1/console/memories/" + memory_id + "/decision",
                         json=body, headers=headers)
    assert repeat.json() == response.json()
    receipts = core.store.rows("SELECT * FROM events WHERE event_type='memory.console_decided'")
    assert len(receipts) == 1
    receipt = json.loads(receipts[0]["body_json"])
    assert receipt["origin"] == ORIGIN and receipt["result_memory_id"] == target
    assert receipt["memory_id"] == memory_id and receipt["decision"] == decision
    assert receipt["text"] != "/确认这一条 " + target
    assert PASSWORD not in encode(receipts)
    assert headers["X-CSRF-Token"] not in encode(receipts)
    material = social_profile_sources.material(core.store, core.settings, core.clock(), [target])
    assert material[0]["content"] == body.get("corrected_content", "我偏爱星空与简洁的表达。")


@pytest.mark.parametrize("case", ["bridge", "origin", "csrf", "expired", "credential"])
def test_console_receipt_requires_real_owner_authentication(client, core, case):
    background(core, nickname_enabled=True)
    memory_id = confirmed_profile_material(core, confirm=False)
    if case == "bridge":
        headers = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
    else:
        headers = login(client)
        if case == "origin":
            headers["Origin"] = "https://attacker.invalid"
        elif case == "csrf":
            headers["X-CSRF-Token"] = "wrong"
        elif case == "expired":
            core.clock.advance(core.settings.console.session_seconds + 1)
        else:
            core.settings.control_token = SecretStr("rotated-owner-credential")
    response = client.post("/v1/console/memories/" + memory_id + "/decision",
                           json={"decision": "confirm"}, headers=headers)
    assert response.status_code in {401, 403}
    assert core.store.one('SELECT status FROM memories WHERE memory_id=%s', (memory_id,)) == {
        "status": "HYPOTHESIS"}
    assert not core.store.rows("SELECT * FROM events WHERE event_type='memory.console_decided'")


def test_bridge_memory_api_and_generic_console_audit_do_not_mint_owner_receipt(client, core):
    background(core, nickname_enabled=True)
    memory_id = confirmed_profile_material(core, confirm=False)
    bridge = core.settings.bridge_token.get_secret_value()
    response = client.post("/internal/v1/memories/" + memory_id + "/decisions",
                           json={"actor_id": "owner", "decision": "confirm"}, headers={
                               "Authorization": "Bearer " + bridge,
                           })
    assert response.status_code == 200, response.text
    core.store.audit("console.operation", memory_id, "console", core.clock(),
                     route="/v1/console/memories/" + memory_id + "/decision")
    with pytest.raises(CoreError, match="SOCIAL_PROFILE_SOURCE_REQUIRED"):
        social_profile_sources.material(core.store, core.settings, core.clock(), [memory_id])
    assert not core.store.rows("SELECT * FROM events WHERE event_type='memory.console_decided'")


@pytest.mark.parametrize("change", [
    "memory_version", "origin", "decision", "missing_audit", "roots",
])
def test_console_decision_receipt_binds_exact_memory_and_native_provenance(client, core, change):
    background(core, nickname_enabled=True)
    memory_id = confirmed_profile_material(core, confirm=False)
    response = client.post("/v1/console/memories/" + memory_id + "/decision",
                           json={"decision": "confirm"}, headers=login(client))
    assert response.status_code == 200, response.text
    receipt = core.store.one("SELECT * FROM events WHERE event_type='memory.console_decided'")
    if change == "memory_version":
        core.store.execute('UPDATE memories SET updated_at=%s WHERE memory_id=%s',
                           (after(core.clock(), 1), memory_id))
    elif change == "missing_audit":
        core.store.execute("DELETE FROM audit_log WHERE action='memory.owner_decision'")
    elif change == "roots":
        core.store.execute('DELETE FROM event_dependencies WHERE event_id=%s',
                           (receipt["event_id"],))
    else:
        body = json.loads(receipt["body_json"])
        body[change] = "https://attacker.invalid" if change == "origin" else "forget"
        core.store.execute('UPDATE events SET body_json=%s WHERE event_id=%s',
                           (encode(body), receipt["event_id"]))
    with pytest.raises(CoreError, match="SOCIAL_PROFILE_SOURCE_REQUIRED"):
        social_profile_sources.material(core.store, core.settings, core.clock(), [memory_id])


@pytest.mark.parametrize("decision", ["confirm", "correct"])
def test_console_decision_does_not_launder_reader_ancestry(client, core, decision):
    _, public, _ = background(core, nickname_enabled=True)
    memory_id = confirmed_profile_material(core, confirm=False)
    core.store.insert("event_dependencies", event_id="evt_test_800", source_event_id=public)
    response = client.post("/v1/console/memories/" + memory_id + "/decision", json={
        "decision": decision,
        "corrected_content": "我偏爱海洋。" if decision == "correct" else None,
    }, headers=login(client))
    assert response.status_code == 200, response.text
    with pytest.raises(CoreError, match="SOCIAL_PROFILE_SOURCE_REQUIRED"):
        social_profile_sources.material(core.store, core.settings, core.clock(),
                                        [response.json()["memory_id"]])


@pytest.mark.parametrize("erase", ["forget", "recall_original", "recall_receipt"])
def test_console_confirmation_erasure_cancels_profile_plan_and_preserves_fee(client, core, erase):
    module, _, conversation = background(core, nickname_enabled=True)
    memory_id = confirmed_profile_material(core, confirm=False)
    headers = login(client)
    path = "/v1/console/memories/" + memory_id + "/decision"
    assert client.post(path, json={"decision": "confirm"}, headers=headers).status_code == 200
    # A correction after confirmation keeps the original QQ closure through both receipts.
    corrected = client.post(path, json={"decision": "correct", "corrected_content": "我偏爱海洋。"},
                            headers={**headers, "Idempotency-Key": "console-correction"})
    assert corrected.status_code == 200, corrected.text
    target = corrected.json()["memory_id"]
    module.record_profile(ProfileBaseline(bot_qq_id="10002", field="nickname", value="旧昵称",
                                          observed_at=core.clock(), receipt_sha256="c" * 64))
    module.model.proposal = {"kind": "nickname", "value": "听海"}
    result = run(module.plan("console-profile-plan", profile_fields=["nickname"],
                             profile_memory_ids=[target]))
    frozen = queue(module, result, conversation)
    if erase == "forget":
        response = client.post("/v1/console/memories/" + target + "/decision",
                               json={"decision": "forget"},
                               headers={**headers, "Idempotency-Key": "console-forget"})
        assert response.status_code == 200, response.text
    else:
        event_id = "evt_test_800" if erase == "recall_original" else core.store.one(

                'SELECT event_id FROM events WHERE '
                "event_type='memory.console_decided' ORDER BY record_sequence"
                ' DESC LIMIT 1'
            )["event_id"]
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, [event_id], core.clock())
    with pytest.raises(CoreError):
        dispatch(module, frozen)
    assert core.store.one("SELECT state,attempt_count FROM outbox") == {
        "state": "CANCELLED", "attempt_count": 0}
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"


def test_console_receipt_and_native_confirmation_share_one_transaction(client, core, monkeypatch):
    background(core, nickname_enabled=True)
    memory_id = confirmed_profile_material(core, confirm=False)
    headers = login(client)
    original = Store.audit

    def fail(store, action, *args, **kwargs):
        if action == "memory.owner_decision":
            assert store is not client.app_core.store and store.in_transaction
            raise RuntimeError("synthetic receipt failure")
        return original(store, action, *args, **kwargs)

    monkeypatch.setattr(Store, "audit", fail)
    with pytest.raises(RuntimeError, match="synthetic receipt failure"):
        client.post("/v1/console/memories/" + memory_id + "/decision",
                    json={"decision": "confirm"}, headers=headers)
    assert core.store.one('SELECT status FROM memories WHERE memory_id=%s', (memory_id,)) == {
        "status": "HYPOTHESIS"}
    assert not core.store.rows("SELECT * FROM events WHERE event_type='memory.console_decided'")


def test_console_owner_is_rechecked_at_native_commit(client, core, monkeypatch):
    background(core, nickname_enabled=True)
    memory_id = confirmed_profile_material(core, confirm=False)
    headers = login(client)
    original = Memory.decide_from_console

    def rotate(memory, *args, **kwargs):
        assert memory.store is not client.app_core.store and memory.store.in_transaction
        core.settings.control_token = SecretStr("rotated-between-auth-and-memory")
        return original(memory, *args, **kwargs)

    monkeypatch.setattr(Memory, "decide_from_console", rotate)
    response = client.post("/v1/console/memories/" + memory_id + "/decision",
                           json={"decision": "confirm"}, headers=headers)
    assert response.status_code == 401, response.text
    assert core.store.one('SELECT status FROM memories WHERE memory_id=%s', (memory_id,)) == {
        "status": "HYPOTHESIS"}
    assert not core.store.rows("SELECT * FROM events WHERE event_type='memory.console_decided'")


@pytest.mark.parametrize("decision", ["confirm", "forget"])
def test_console_correction_content_cannot_be_attached_to_another_decision(client, core, decision):
    background(core, nickname_enabled=True)
    memory_id = confirmed_profile_material(core, confirm=False)
    path = "/v1/console/memories/" + memory_id + "/decision"
    headers = login(client)
    body = {"decision": decision, "corrected_content": "另一份内容"}
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 400, response.text
    repeated = client.post(path, json=body, headers=headers)
    assert repeated.status_code == response.status_code and repeated.json() == response.json()
    assert core.store.one('SELECT state FROM console_operations WHERE request_id=%s',
                          (headers["Idempotency-Key"],)) == {"state": "REJECTED"}
    assert core.store.one('SELECT status FROM memories WHERE memory_id=%s', (memory_id,)) == {
        "status": "HYPOTHESIS"}
    assert not core.store.rows("SELECT * FROM events WHERE event_type='memory.console_decided'")
