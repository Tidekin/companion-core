"""Device credentials change live without rebinding paid work or other clients."""

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import native_devices, owner_authority, owner_input, transport
from companion.console_api import COOKIE, digest
from companion_delivery_protocol.native_chat import device_identity
from tests.conftest import run
from tests.sdk_reader_fixture import answer, response, worker_for
from tests.support.native_chat import enroll, packet
from tests.support.native_history_delivery import sdk_step
from tests.support.owner_console import client as client
from tests.support.owner_input import captured


def use_cookie(client, cookies):
    client.cookies.clear()
    client.cookies.set(COOKIE, cookies, domain='console.example.test', path='/v1/console')


def test_enroll_while_another_device_has_work_and_sync_both_sides(client, core, tmp_path):
    core.settings.memory_enabled = False
    original, _ = captured(core, tmp_path)
    key_a, key_b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    before = Path(core.settings.paid_model.journal_path).read_bytes()
    headers_a, authority = enroll(client, key_a)
    cookie_a = client.cookies.get(COOKIE)
    assert authority == original.authority
    first = packet(core, key_a, authority, "来自第一台设备")
    accepted = client.post("/v1/console/chat/messages", json=first, headers=headers_a)
    assert accepted.status_code == 202, accepted.text
    job = dict(
        core.store.one(
            "SELECT * FROM jobs WHERE event_id=%s AND kind='chat'", (first["capture"]["event_id"],)
        )
    )
    after_admission = Path(core.settings.paid_model.journal_path).read_bytes()
    assert after_admission.startswith(before)
    client.cookies.clear()
    _, same = enroll(client, key_b)
    assert same == authority
    assert core.store.one('SELECT 1 FROM console_sessions WHERE token_sha256=%s',
                          (digest(cookie_a),)), 'first session lost during second login'
    assert dict(core.store.one("SELECT * FROM jobs WHERE job_id=%s", (job["job_id"],))) == job
    assert Path(core.settings.paid_model.journal_path).read_bytes() == after_admission
    run(
        sdk_step(
            core, tmp_path, first["capture"]["event_id"], "hot", reply="两台设备都能看见这条回复"
        )
    )
    synced = client.get("/v1/console/chat/messages").json()["items"]
    assert synced[0]["text"] == "来自第一台设备"
    assert [item["text"] for item in synced[0]["replies"]] == ["两台设备都能看见这条回复"]
    use_cookie(client, cookie_a)
    assert client.get("/v1/console/chat/messages").json()["items"] == synced
    assert (
        owner_authority.current(core.store, core.settings, core.clock).authority.as_dict()
        == authority
    )


def test_revocation_is_scoped_and_old_generation_cannot_return(client, core, tmp_path):
    core.settings.memory_enabled = False
    captured(core, tmp_path)
    key_a, key_b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    headers_a, authority = enroll(client, key_a)
    cookie_a = client.cookies.get(COOKIE)
    old = packet(core, key_a, authority, "撤销前的消息")
    assert client.post("/v1/console/chat/messages", json=old, headers=headers_a).status_code == 202
    client.cookies.clear()
    headers_b, _ = enroll(client, key_b)
    assert core.store.one('SELECT 1 FROM console_sessions WHERE token_sha256=%s',
                          (digest(cookie_a),)), 'first session lost during second login'
    cookie_b = client.cookies.get(COOKIE)
    other = packet(core, key_b, authority, "另一台设备继续处理")
    assert (
        client.post("/v1/console/chat/messages", json=other, headers=headers_b).status_code == 202
    )
    use_cookie(client, cookie_a)
    device_id, _ = device_identity(key_a.public_key().public_bytes_raw().hex())
    core.clock.advance(1)
    revoked = client.post(
        "/v1/console/chat/devices/" + device_id + "/revoke",
        json={"operation_id": "revoke-first-device"},
        headers=headers_a,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["state"] == "REVOKED"
    assert client.get("/v1/console/chat/messages").status_code == 403
    assert client.post("/v1/console/chat/messages", json=old, headers=headers_a).status_code == 403
    assert (
        owner_input.verify(core.store, core.settings, old["capture"])[0].event_id
        == old["capture"]["event_id"]
    )
    states = {
        r["event_id"]: r["state"]
        for r in core.store.rows("SELECT event_id,state FROM jobs WHERE kind='chat'")
    }
    assert states[old["capture"]["event_id"]] == "CANCELLED"
    assert states[other["capture"]["event_id"]] == "QUEUED"
    use_cookie(client, cookie_b)
    run(sdk_step(core, tmp_path, other["capture"]["event_id"], "other", reply="另一台设备仍然正常"))
    assert any(
        r["text"] == "另一台设备仍然正常"
        for t in client.get("/v1/console/chat/messages").json()["items"]
        for r in t["replies"]
    )
    use_cookie(client, cookie_a)
    public = key_a.public_key().public_bytes_raw().hex()
    denied = client.post(
        "/v1/console/chat/devices/preview", json={"public_key": public}, headers=headers_a
    )
    assert denied.status_code == 403 and denied.json()["error"] == "NATIVE_DEVICE_RELOGIN_REQUIRED"
    core.clock.advance(1)
    fresh_headers, same = enroll(client, key_a)
    assert same == authority
    new = packet(core, key_a, authority, "重新登记后的消息")
    assert new["capture"]["protocol_epoch"] != old["capture"]["protocol_epoch"]
    assert (
        client.post("/v1/console/chat/messages", json=old, headers=fresh_headers).status_code == 403
    )
    assert (
        client.post("/v1/console/chat/messages", json=new, headers=fresh_headers).status_code == 202
    )


def test_registration_identity_is_durable_and_not_in_the_global_policy(client, core, tmp_path):
    captured(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    before = owner_authority.current(core.store, core.settings, core.clock)
    _, authority = enroll(client, key)
    device_id, _ = device_identity(key.public_key().public_bytes_raw().hex())
    row = native_devices._row(core.store, device_id)
    assert row["state"] == "ACTIVE" and row["version"] == 1
    assert json.loads(row["identity_json"])["protocol_epoch"].startswith("native-v2-")
    assert device_id not in before.policy.native_devices
    after = owner_authority.current(core.store, core.settings, core.clock)
    assert after.authority.as_dict() == authority == before.authority.as_dict()
    assert after.policy == before.policy


def test_sent_replies_survive_device_revocation_and_erasure_is_explicit(client, core, tmp_path):
    core.settings.memory_enabled = False
    captured(core, tmp_path)
    key_a, key_b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    headers_a, authority = enroll(client, key_a)
    cookie_a = client.cookies.get(COOKIE)
    submitted = client.post('/v1/console/chat/messages',
        json=packet(core, key_a, authority, '保留的原始消息'), headers=headers_a).json()
    run(sdk_step(core, tmp_path, submitted['event_id'], 'sent-before-revoke',
                 reply='已经发送的回复'))
    client.cookies.clear()
    headers_b, _ = enroll(client, key_b)
    cookie_b = client.cookies.get(COOKIE)
    device_id, _ = device_identity(key_a.public_key().public_bytes_raw().hex())
    url = '/v1/console/chat/devices/'+device_id+'/revoke'
    assert client.post(url, json={'operation_id': 'wrong-device-revoke'},
                       headers=headers_b).status_code == 403
    use_cookie(client, cookie_a)
    assert client.post(url, json={'operation_id': 'revoke-sent-device'},
                       headers=headers_a).status_code == 200
    use_cookie(client, cookie_b)
    original = client.get('/v1/console/chat/messages').json()['items'][0]
    assert original['replies'][0]['text'] == '已经发送的回复'
    assert not original['replies'][0].get('content_erased', False)
    with core.store.transaction(control_write=True):
        transport.revoke_deliveries(core.store, 'origin_event_id=%s',
            (submitted['event_id'],), erase_content=True)
    erased = client.get('/v1/console/chat/messages').json()['items'][0]
    assert erased['text'] == '保留的原始消息'
    assert erased['replies'][0]['state'] == 'SENT'
    assert erased['replies'][0]['text'] == ''
    assert erased['replies'][0]['content_erased'] is True


@pytest.mark.parametrize('revoke', [False, True])
def test_credential_change_during_provider_io_keeps_the_original_financial_result(
    client, core, tmp_path, revoke,
):
    core.settings.memory_enabled = False
    captured(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    accepted = client.post('/v1/console/chat/messages',
        json=packet(core, key, authority), headers=headers).json()
    calls = []

    def provider(request):
        calls.append(1)
        if revoke:
            device_id, _ = device_identity(key.public_key().public_bytes_raw().hex())
            result = client.post('/v1/console/chat/devices/'+device_id+'/revoke',
                json={'operation_id': 'revoke-during-provider'}, headers=headers)
            assert result.status_code == 200, result.text
        else:
            client.cookies.clear()
            _, unchanged = enroll(client, Ed25519PrivateKey.generate())
            assert unchanged == authority
        return response(answer('原来那台设备的回复'),
                        stream=json.loads(request.content).get('stream', False))

    async def scenario():
        async with worker_for(core, provider) as worker:
            assert await worker.run_once('native-credential-io', job_id=accepted['job_id'])
            assert not await worker.run_once('native-credential-no-repeat',
                                             job_id=accepted['job_id'])
            job = worker.store.one('SELECT state,last_error_code FROM jobs WHERE job_id=%s',
                                    (accepted['job_id'],))
            assert job['state'] == ('CANCELLED' if revoke else 'SUCCEEDED'), dict(job)
            assert bool(worker.store.rows('SELECT 1 FROM outbox')) is not revoke
            usages = worker.store.rows('SELECT state FROM model_usage')
            assert len(usages) == 1 and usages[0]['state'] in {'SETTLED', 'UNKNOWN'}
    run(scenario())
    assert calls == [1]
    assert owner_authority.current(
        core.store, core.settings, core.clock).authority.as_dict() == authority
