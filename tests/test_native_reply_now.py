"""Releasing the native input window preserves job identity and dispatch fences."""

from concurrent.futures import ThreadPoolExecutor

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion.input_debounce import InputDebounceConfig
from tests.support.native_chat import enroll, packet
from tests.support.owner_console import client as client
from tests.support.owner_input import captured


def test_only_current_pending_native_input_can_be_released(client, core, tmp_path):
    core.settings.input_debounce = InputDebounceConfig()
    captured(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, auth = enroll(client, key)
    base = "/v1/console/chat/messages"

    def accept(text):
        result = client.post(base, json=packet(core, key, auth, text), headers=headers)
        assert result.status_code == 202, result.text
        return result.json()

    first = accept("我还想补一句。")
    last = accept("这次就说完了。")
    route = f"{base}/{last['event_id']}/reply-now"
    assert client.get(f"{base}/{last['event_id']}").json()["items"][0]["can_reply_now"]
    assert client.post(route).status_code == 403
    assert client.post(f"{base}/missing/reply-now", headers=headers).status_code == 404
    stale = client.post(f"{base}/{first['event_id']}/reply-now", headers=headers)
    assert stale.json()["state"] == "CANCELLED" and "can_reply_now" not in stale.json()
    assert core.claim_job("too-soon", job_id=last["job_id"]) is None
    with ThreadPoolExecutor(2) as pool:
        both = list(pool.map(lambda _: client.post(route, headers=headers), range(2)))
    for released in both:
        assert released.status_code == 200, released.text
        assert released.json()["event_id"] == last["event_id"]
        assert "can_reply_now" not in released.json()
    job = core.claim_job("worker", job_id=last["job_id"])
    assert job["job_id"] == last["job_id"] and job["attempts"] == 1
    assert client.post(route, headers=headers).json()["state"] == "RUNNING"
    assert core.claim_job("other", job_id=last["job_id"]) is None
    assert not core.store.rows("SELECT 1 FROM model_usage")
    core.store.execute("UPDATE jobs SET state='RETRY_WAIT',next_attempt_at='2099-01-01' "
                       "WHERE job_id=%s", (last["job_id"],))
    assert "can_reply_now" not in client.post(route, headers=headers).json()
    assert core.store.one("SELECT next_attempt_at FROM jobs WHERE job_id=%s",
                          (last["job_id"],))["next_attempt_at"] == "2099-01-01"
