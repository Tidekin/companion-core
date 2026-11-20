"""Device and session lifecycle cannot redirect or silently revive push notifications."""

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import native_push
from companion_delivery_protocol.native_chat import device_identity
from tests.support.native_chat import enroll
from tests.support.owner_console import client as client
from tests.support.owner_console import login
from tests.support.owner_input import captured

PUSH = "/v1/console/chat/push"
BODY = {"token": "synthetic-reception-token", "route": "b" * 32}


@pytest.mark.parametrize("end", ["disable", "logout", "revoke"])
def test_authenticated_device_registration_is_revocable(client, core, tmp_path, end):
    captured(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    device = device_identity(key.public_key().public_bytes_raw().hex())[0]
    headers, _ = enroll(client, key)
    assert client.put(PUSH, headers=headers, json=BODY).json()["error"] == "NATIVE_PUSH_DISABLED"
    core.settings.native_push_project_id = "demo-companion-push"
    rejected = client.put(PUSH, headers={**headers, "X-CSRF-Token": "wrong"}, json=BODY)
    assert rejected.status_code == 403
    assert client.put(PUSH, headers=headers, json=BODY).json() == {"registered": True}
    existing = core.store.one("SELECT * FROM native_push_registrations")
    assert BODY["token"] not in repr(existing)
    assert client.put(PUSH, headers=headers, json=BODY).json() == {"registered": True}
    assert existing == core.store.one("SELECT * FROM native_push_registrations")
    replacement = {**BODY, "token": "replacement-token-123"}
    assert client.put(PUSH, headers=headers, json=replacement).is_success
    assert existing["binding_id"] != core.store.one(
        "SELECT binding_id FROM native_push_registrations")["binding_id"]
    # A late UNREGISTERED response refers to an earlier token, not the replacement.
    with core.store.transaction():
        native_push._response(core.store, {"binding_id": existing["binding_id"],
            "notification_id": "earlier-notification"}, httpx.Response(404, json={"error": {
                "details": [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                             "errorCode": "UNREGISTERED"}]}}), core.clock)
    assert core.store.one("SELECT 1 FROM native_push_registrations")
    if end == "disable":
        assert client.delete(PUSH, headers=headers).json() == {"registered": False}
    elif end == "logout":
        assert client.delete("/v1/console/session", headers=headers).is_success
    else:
        assert client.post(f"/v1/console/chat/devices/{device}/revoke", headers=headers,
                           json={"operation_id": "native-push-revoke"}).is_success
    assert not core.store.one("SELECT 1 FROM native_push_registrations")
    owner = login(client)
    denied = client.put(PUSH, headers=owner, json=BODY)
    assert denied.json()["error"] == "NATIVE_DEVICE_SESSION_REQUIRED"


def test_later_login_can_apply_offline_opt_out_and_fences_stale_put(client, core, tmp_path):
    captured(core, tmp_path)
    core.settings.native_push_project_id = "demo-companion-push"
    key = Ed25519PrivateKey.generate()
    earlier, _ = enroll(client, key)
    assert client.put(PUSH, headers=earlier, json=BODY).is_success
    previous = httpx.Cookies(client.cookies)
    newer, _ = enroll(client, key)
    assert client.delete(PUSH, headers=newer).is_success
    assert not core.store.one("SELECT 1 FROM native_push_registrations")
    client.cookies.clear()
    client.cookies.update(previous)
    assert client.put(PUSH, headers=earlier, json=BODY).json() == {
        "error": "NATIVE_PUSH_SESSION_CHANGED"}


def test_google_adc_uses_messaging_scope_and_reuses_access_token(tmp_path, monkeypatch):
    import base64
    import json
    from urllib.parse import parse_qs

    import requests
    from cryptography.hazmat.primitives import serialization as pem
    from cryptography.hazmat.primitives.asymmetric import rsa

    from tests.conftest import run

    path = tmp_path / "synthetic-adc.json"
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_text(json.dumps({"type": "service_account", "project_id": "demo-companion-push",
        "private_key_id": "synthetic", "client_email": "test@example.invalid",
        "token_uri": "https://oauth2.googleapis.com/token",
        "private_key": private.private_bytes(
            pem.Encoding.PEM, pem.PrivateFormat.PKCS8, pem.NoEncryption()).decode()}))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    scopes = []

    def oauth(session, method, url, **kwargs):
        assert method == "POST" and url == "https://oauth2.googleapis.com/token"
        assertion = parse_qs(kwargs["data"])[b"assertion"][0]
        claims = json.loads(base64.urlsafe_b64decode(assertion.split(b".")[1] + b"==="))
        scopes.append(claims["scope"])
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"access_token":"synthetic-scoped-access","expires_in":3600}'
        return response

    monkeypatch.setattr(requests.Session, "request", oauth)
    sender = native_push.FcmSender()
    assert run(sender.access_token()) == run(sender.access_token()) == "synthetic-scoped-access"
    assert scopes == ["https://www.googleapis.com/auth/firebase.messaging"]
