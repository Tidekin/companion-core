"""Content-free Android notifications for already published private proactive messages."""

import asyncio
import base64
import hashlib
import hmac
import re
from datetime import datetime
from functools import partial

import httpx
from cryptography.fernet import Fernet, InvalidToken
from psycopg.errors import UniqueViolation
from pydantic import Field, SecretStr, field_validator

from companion import native_chat, native_devices, owner_authority, owner_delivery
from companion.console_api import _credential
from companion.contracts import CoreError, StrictModel
from companion.store import after, uid


class Registration(StrictModel):
    token: SecretStr
    route: str = Field(pattern="^[a-f0-9]{32}$")

    @field_validator("token")
    @classmethod
    def token_format(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9:._~-]{16,4096}", value.get_secret_value()):
            raise ValueError("Invalid registration token format")
        return value


def _cipher(settings):
    # A database copy contains neither usable registration tokens nor the decryption key.
    secret = settings.control_token.get_secret_value()
    if not secret:
        raise CoreError("CONSOLE_OWNER_CREDENTIAL_REQUIRED", 503)
    key = hmac.digest(secret.encode(), b"companion/native-push/token/v1", "sha256")
    return Fernet(base64.urlsafe_b64encode(key))


def _current_session(store, session):
    """An authenticated device can withdraw an older opt-in, including after an offline logout."""
    device = session["native_device_id"]
    if not device:
        raise CoreError("NATIVE_DEVICE_SESSION_REQUIRED", 403)
    row = store.one("SELECT d.push_session_sequence,s.record_sequence FROM native_devices d "
                    "JOIN console_sessions s ON s.token_sha256=%s WHERE d.device_id=%s "
                    "FOR UPDATE OF d", (session["token_sha256"], device))
    if row is None or row["record_sequence"] < row["push_session_sequence"]:
        return False
    if row["record_sequence"] > row["push_session_sequence"]:
        store.execute("UPDATE native_devices SET push_session_sequence=%s WHERE device_id=%s",
                      (row["record_sequence"], device))
    return True


def register(context, check, body):
    store, settings = context.store, context.settings
    if not settings.native_push_project_id:
        raise CoreError("NATIVE_PUSH_DISABLED", 409)
    with store.transaction(control_write=True):
        session = check()
        device, view = native_chat.device_view(context)
        if not _current_session(store, session):
            raise CoreError("NATIVE_PUSH_SESSION_CHANGED", 409)
        revision = native_devices.active(store, device, view)["revision_id"]
        token = body.token.get_secret_value().encode()
        try:
            store.execute("""
                INSERT INTO native_push_registrations AS r
                    (device_id,device_revision,session_hash,binding_id,project_id,route,
                     token_hash,token_ciphertext)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(device_id) DO UPDATE SET
                    device_revision=excluded.device_revision,session_hash=excluded.session_hash,
                    binding_id=excluded.binding_id,project_id=excluded.project_id,
                    route=excluded.route,token_hash=excluded.token_hash,
                    token_ciphertext=excluded.token_ciphertext
                WHERE (r.device_revision,r.session_hash,r.project_id,r.route,r.token_hash)
                    IS DISTINCT FROM (excluded.device_revision,excluded.session_hash,
                                      excluded.project_id,excluded.route,excluded.token_hash)
                """, (device, revision, session["token_sha256"], uid("push"),
                      settings.native_push_project_id, body.route,
                      hashlib.sha256(token).hexdigest(),
                      _cipher(settings).encrypt(token).decode()))
        except UniqueViolation:
            raise CoreError("NATIVE_PUSH_REGISTRATION_CONFLICT", 409) from None
    return {"registered": True}


def unregister(context, check):
    with context.store.transaction(control_write=True):
        session = check()
        withdraw_session(context.store, session)
    return {"registered": False}


def withdraw_session(store, session):
    if _current_session(store, session):
        store.execute("DELETE FROM native_push_registrations WHERE device_id=%s",
                      (session["native_device_id"],))


def enqueue(store, settings, row, now):
    """Called in the publication transaction, only on the first segment becoming SENT."""
    store.execute("""
        INSERT INTO native_push_deliveries
            (notification_id,group_id,message_id,device_id,binding_id,state,next_at,expires_at)
        SELECT gen_random_uuid()::text,%s,%s,r.device_id,r.binding_id,'PENDING',%s,%s
        FROM native_push_registrations r JOIN console_sessions s ON s.token_sha256=r.session_hash
        JOIN native_devices d ON d.device_id=r.device_id AND d.revision_id=r.device_revision
        WHERE r.project_id=%s AND s.expires_at>%s AND s.credential_tag=%s
        ON CONFLICT(group_id,device_id) DO NOTHING
        """, (row["reply_group_id"] or row["message_id"], row["message_id"], now, after(now, 3600),
              settings.native_push_project_id, now, _credential(settings)))


class FcmSender:
    """Google ADC obtains OAuth off the event loop. Only the trusted FCM endpoint sees a token."""

    def __init__(self, client=None):
        self.client = client
        self.credentials = None

    def _access_token(self):
        import google.auth
        import requests
        from google.auth.transport.requests import Request

        with requests.Session() as session:
            request = partial(Request(session=session), timeout=10)
            if self.credentials is None:
                self.credentials, _ = google.auth.default(request=request, scopes=[
                    "https://www.googleapis.com/auth/firebase.messaging"])
            if not self.credentials.valid:
                self.credentials.refresh(request)
            return self.credentials.token

    async def access_token(self):
        return await asyncio.to_thread(self._access_token)

    async def send(self, project, access_token, payload):
        url = f"https://fcm.googleapis.com/v1/projects/{project}/messages:send"
        options = {"headers": {"Authorization": "Bearer " + access_token},
                   "json": {"message": payload}, "timeout": 10.0, "follow_redirects": False}
        if self.client is not None:
            return await self.client.post(url, **options)
        async with httpx.AsyncClient(trust_env=False) as client:
            return await client.post(url, **options)


def _finish(store, notice, state, code=None, delay=0, *, clock, provider_id=None):
    store.execute("UPDATE native_push_deliveries SET state=%s,error_code=%s,next_at=%s,"
                  "provider_message_id=%s "
                  "WHERE notification_id=%s",
                  (state, code, after(clock(), delay), provider_id, notice["notification_id"]))


def _material(store, settings, clock, notice):
    row = store.one("""
        SELECT r.route,r.token_ciphertext,r.device_revision,s.expires_at
        FROM native_push_registrations r JOIN console_sessions s ON s.token_sha256=r.session_hash
        WHERE r.device_id=%s AND r.binding_id=%s AND r.project_id=%s
          AND s.expires_at>%s AND s.credential_tag=%s
        """, (notice["device_id"], notice["binding_id"], settings.native_push_project_id,
              clock(), _credential(settings)))
    if not row:
        raise CoreError("NATIVE_PUSH_REGISTRATION_CHANGED", 403)
    view = owner_authority.current(store, settings, clock, purpose="chat")
    device = native_devices.active(store, notice["device_id"], view)
    if not device or device["revision_id"] != row["device_revision"]:
        raise CoreError("NATIVE_DEVICE_APPROVAL_REQUIRED", 403)
    if not store.one("""
        SELECT 1 FROM outbox o JOIN events e ON e.event_id=o.origin_event_id
        JOIN conversations c ON c.conversation_id=e.conversation_id
        WHERE o.message_id=%s AND o.action_type='native.private.proactive' AND o.state='SENT'
          AND o.conversation_id=c.conversation_id AND c.platform='native.chat'
          AND c.platform_session_key='native:owner' AND c.owner_actor_id='owner'
          AND c.scope='private' AND e.redacted=0 AND e.privacy_scope='private'
          AND o.content_sha256<>'' AND o.content_json<>'{}'
        """, (notice["message_id"],)):
        raise CoreError("NATIVE_PUSH_SOURCE_UNAVAILABLE", 409)
    permission = owner_delivery.check_delivery(store, settings, clock, notice["message_id"])
    deadlines = [notice["expires_at"], row["expires_at"]]
    if permission.get("lease_until"):
        deadlines.append(permission["lease_until"])
    ttl = int((min(map(datetime.fromisoformat, deadlines))
               - datetime.fromisoformat(clock())).total_seconds())
    if ttl <= 0:
        raise CoreError("NATIVE_PUSH_EXPIRED", 409)
    token = _cipher(settings).decrypt(row["token_ciphertext"].encode()).decode()
    return {"token": token, "notification": {"title": "Companion", "body": "有新消息"},
            "data": {"route": row["route"]},
            "android": {"priority": "high", "ttl": f"{min(ttl, 3600)}s",
                        "notification": {"channel_id": "companion_proactive",
                                         "tag": notice["notification_id"]}}}


def _response(store, notice, response, clock):
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    error = body.get("error")
    error = error if isinstance(error, dict) else {}
    details = error.get("details")
    name = body.get("name")
    if response.status_code == 200 and isinstance(name, str) and re.fullmatch(
        r"projects/[a-zA-Z0-9-]+/messages/[a-zA-Z0-9%:_~.\-]{1,256}", name
    ):
        _finish(store, notice, "ACCEPTED", clock=clock, provider_id=name)
    elif response.status_code == 404 and isinstance(details, list) and any(
        isinstance(item, dict)
        and item.get("@type") == "type.googleapis.com/google.firebase.fcm.v1.FcmError"
        and item.get("errorCode") == "UNREGISTERED" for item in details
    ):
        store.execute("DELETE FROM native_push_registrations WHERE binding_id=%s",
                      (notice["binding_id"],))
        _finish(store, notice, "FAILED", "FCM_UNREGISTERED", clock=clock)
    elif response.status_code in (429, 503) and error.get("status") in (
        "RESOURCE_EXHAUSTED", "UNAVAILABLE",
    ):
        _retry(store, notice, clock, "FCM_RETRY_LATER", _retry_after(response, clock))
    elif 400 <= response.status_code < 500:
        _finish(store, notice, "FAILED", f"FCM_HTTP_{response.status_code}", clock=clock)
    else:
        _finish(store, notice, "UNKNOWN", "FCM_RESPONSE_UNKNOWN", clock=clock)


def _retry_after(response, clock):
    from email.utils import parsedate_to_datetime

    value = response.headers.get("retry-after", "")
    try:
        seconds = int(value) if value.isdecimal() else (
            parsedate_to_datetime(value) - datetime.fromisoformat(clock())).total_seconds()
        return max(0, min(3600, int(seconds)))
    except (ValueError, TypeError, OverflowError):
        return 0


def _retry(store, notice, clock, code, minimum=0):
    delay = max(60 * 2**min(notice["attempts"], 5), minimum)
    state = "PENDING" if after(clock(), delay) < notice["expires_at"] else "FAILED"
    _finish(store, notice, state, code, delay, clock=clock)


async def dispatch_one(store, settings, clock, sender):
    """A committed UNKNOWN fences crashes/timeouts across the external write boundary."""
    if not settings.native_push_project_id:
        return False
    candidate = store.one("SELECT notification_id FROM native_push_deliveries "
                          "WHERE state='PENDING' AND next_at<=%s "
                          "ORDER BY next_at,notification_id LIMIT 1", (clock(),))
    if not candidate:
        return False
    # Fetch credentials before final permission and the dispatch marker. An OAuth failure
    # has sent no message and must not consume the provider attempt.
    credential = None
    try:
        credential = await sender.access_token()
    except Exception:
        pass  # Credential errors can contain private provider details; record only the code.
    with store.transaction():
        notice = store.one("SELECT * FROM native_push_deliveries WHERE notification_id=%s "
                           "AND state='PENDING' AND next_at<=%s FOR UPDATE SKIP LOCKED",
                           (candidate["notification_id"], clock()))
        if not notice:
            return False
        try:
            payload = _material(store, settings, clock, notice)
        except (CoreError, InvalidToken) as error:
            _finish(store, notice, "CANCELLED", getattr(error, "code", "NATIVE_PUSH_KEY_CHANGED"),
                    clock=clock)
            return True
        if not credential:
            _finish(store, notice, "PENDING", "FCM_CREDENTIAL_UNAVAILABLE", 60, clock=clock)
            return True
        store.execute("UPDATE native_push_deliveries SET state='UNKNOWN',attempts=attempts+1,"
                      "error_code='FCM_DISPATCH_UNCONFIRMED' WHERE notification_id=%s",
                      (notice["notification_id"],))
    try:
        response = await sender.send(settings.native_push_project_id, credential, payload)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        # No FCM request bytes were written before connection or pool acquisition failed.
        with store.transaction():
            _retry(store, notice, clock, "FCM_CONNECTION_UNAVAILABLE")
        return True
    except httpx.TransportError:
        return True
    with store.transaction():
        _response(store, notice, response, clock)
    return True
