"""Bounded observations of authorized account fields; never account writes.

SnowLuma 2.0.3 may serve its full profile from cache. These receipts establish
recent API/CDN observation, not a fresh network read of QQ's profile database.
"""

import asyncio
import hashlib
import io
import json
import re
import time
from datetime import datetime
from urllib.parse import urlencode

import httpx
from PIL import Image

from .event_normalizer import decode, identifier
from .files import _get
from .images import _private_download, global_address, owner_media_request, resolve_public
from .journal import digest
from .network import checked_http_client, private_request_matches, seal_protocol_content
from .onebot_client import bounded_post

STATE = "/internal/v1/account-actions/profile-baselines/state"
SUBMIT = "/internal/v1/account-actions/profile-baselines"
ACTION = "/get_stranger_info"
FIELDS = {"nickname", "signature", "avatar"}
COOLDOWN_SECONDS = 60
MAX_AVATAR_BYTES = 2 * 1024 * 1024


def validate_record(value):
    if (not isinstance(value, dict) or set(value) != {"profiles"}
        or not isinstance(value["profiles"], list) or not 1 <= len(value["profiles"]) <= 3):
        raise ValueError("PROFILE_JOURNAL_CHANGED")
    seen = set()
    for row in value["profiles"]:
        if (not isinstance(row, dict) or set(row) != {
            "field", "observed_at", "value_sha256", "receipt_sha256"}
            or row["field"] not in FIELDS or row["field"] in seen
            or any(not isinstance(row[key], str) or not re.fullmatch(r"[a-f0-9]{64}", row[key])
                   for key in ("value_sha256", "receipt_sha256"))
            or datetime.fromisoformat(row["observed_at"]).utcoffset() is None):
            raise ValueError("PROFILE_JOURNAL_CHANGED")
        seen.add(row["field"])


def local(window):
    window.verify()
    if window.db.execute("SELECT 1 FROM inbox WHERE state='pending' LIMIT 1").fetchone():
        raise ValueError("PROFILE_OWNER_INPUT_PENDING")


async def grant(window, client):
    local(window)
    permission = await window.check(client, purpose="account_actions")
    expected = {"authority": window.authority,
                "authorization_sha256": permission["capability_policies"]["account_actions"]}

    async def check():
        await window.check(client, purpose="social")
        local(window)

    query = {**expected, "authority": json.dumps(expected["authority"])}
    value = await owner_media_request(window, client, STATE + "?" + urlencode(query),
        purpose="account_actions", before=check, method="GET")
    local(window)
    if (not isinstance(value, dict)
        or set(value) != {*expected, "fields", "baseline_seconds"}
        or any(value[key] != item for key, item in expected.items())
        or not isinstance(value["fields"], list)
        or any(not isinstance(field, str) or field not in FIELDS for field in value["fields"])
        or len(set(value["fields"])) != len(value["fields"])
        or type(value["baseline_seconds"]) is not int
        or not 1 <= value["baseline_seconds"] <= 86400):
        raise ValueError("PROFILE_GRANT_CHANGED")
    return value


def record(window, kind, material):
    with window.db:
        window.append(kind, material)


def due(window):
    from .owner_runtime import OwnerRuntime

    if not isinstance(window, OwnerRuntime):
        return False
    records = window.verify(record_kinds={"PROFILE_PROBE_STARTED"})
    previous = next((row for row in reversed(records)
                     if row["kind"] == "PROFILE_PROBE_STARTED"), None)
    return previous is None or (window.clock() - datetime.fromisoformat(
        previous["at"])).total_seconds() >= COOLDOWN_SECONDS


def _complete_profile(data):
    # SnowLuma v2.0.3 db142db9: contact-actions.ts formatStrangerInfo.
    # Native profiles may omit UID; the six-field fallback lacks this full shape.
    required = {
        "user_id": int, "uid": str, "qid": str, "nickname": str, "remark": str,
        "sex": str, "age": int, "long_nick": str, "qq_level": int, "qqLevel": int,
        "level": int, "status": int, "extStatus": int, "ext_status": int,
        "batteryStatus": int, "customStatusDescInfo": str, "qidian_master_flag": int,
        "qidian_crew_flag": int, "qidian_crew_flag_2": int, "qidian_enterprise_name": str,
    }
    optional = {"reg_time": int, "is_vip": bool, "is_years_vip": bool, "vip_level": int}
    fields = required.keys() | {"customStatus"}
    return (
        fields <= data.keys() <= fields | optional.keys()
        and all(type(data[key]) is kind for key, kind in required.items())
        and all(type(data[key]) is kind for key, kind in optional.items() if key in data)
        and (data["customStatus"] is None or isinstance(data["customStatus"], dict))
        and data["qq_level"] == data["qqLevel"] == data["level"]
        and data["extStatus"] == data["ext_status"]
    )


async def native_profile(window, protocol, check):
    config = window.config
    body = {"user_id": int(config.bot_qq_id)}

    async def boundary(request):
        await check()
        content = request.content
        if (request.method != "POST"
            or not private_request_matches(request, config.onebot_http_url, ACTION)
            or request.headers.get("Authorization") != "Bearer " + config.onebot_token
            or "Cookie" in request.headers or decode(content, 1024) != body):
            raise ValueError("PROFILE_ACTION_CHANGED")
        local(window)
        seal_protocol_content(request, content)

    await check()
    async with checked_http_client(protocol, boundary) as scoped:
        value = await bounded_post(scoped, ACTION, body)
    observed_at = window.clock().isoformat()
    await check()
    data = value.get("data")
    if (value.get("status") != "ok" or type(value.get("retcode")) is not int
        or value["retcode"] != 0 or not isinstance(data, dict)
        or identifier(data.get("user_id")) != config.bot_qq_id
        or not isinstance(data.get("uid"), str)
        or (re.fullmatch(r"u_[A-Za-z0-9_-]{20,64}", data["uid"]) is None
            and not (data["uid"] == "" and _complete_profile(data)))):
        raise ValueError("PROFILE_FULL_ACCOUNT_REQUIRED")
    return data, observed_at, digest(value)


def text_baselines(data, fields, bot, at, receipt):
    profiles = []
    for field, native, maximum in (("nickname", "nickname", 128),
                                    ("signature", "long_nick", 1000)):
        if field not in fields:
            continue
        value = data.get(native)
        if (not isinstance(value, str) or len(value) > maximum
            or (field == "nickname" and not value.strip())
            or any(ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                   or ord(char) < 32 and (field == "nickname" or char not in "\n\t")
                   for char in value)):
            continue
        profiles.append({"bot_qq_id": bot, "field": field, "value": value,
                         "observed_at": at, "receipt_sha256": receipt})
    return profiles


async def avatar_baseline(window, check, *, transport=None, resolver=resolve_public):
    """One exact public bot-avatar GET, with pinned DNS and no redirect or credentials."""
    bot = window.config.bot_qq_id
    target = httpx.URL("https://q1.qlogo.cn/g?b=qq&nk=" + bot + "&s=100")
    delegate = transport or httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    response = None
    quiet = _private_download.set(True)
    try:
        async with asyncio.timeout(10):
            await check()
            addresses = await resolver(target.host, 443)
            if not addresses or any(not global_address(ip) for ip in addresses):
                raise ValueError("PROFILE_AVATAR_NONPUBLIC_ADDRESS")
            response = await _get(delegate, target, addresses[0], check)
            if response.status_code != 200:
                raise ValueError("PROFILE_AVATAR_FETCH_REJECTED")
            mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if (mime not in {"image/jpeg", "image/png", "image/webp"}
                or response.headers.get("content-encoding", "identity").lower() != "identity"):
                raise ValueError("PROFILE_AVATAR_TYPE_INVALID")
            size = response.headers.get("content-length")
            if size is not None and (re.fullmatch(r"[0-9]{1,8}", size) is None
                                     or int(size) > MAX_AVATAR_BYTES):
                raise ValueError("PROFILE_AVATAR_SIZE_INVALID")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(chunk) > MAX_AVATAR_BYTES - len(body):
                    raise ValueError("PROFILE_AVATAR_TOO_LARGE")
                body.extend(chunk)
            observed_at = window.clock().isoformat()
            if not body or (size is not None and int(size) != len(body)):
                raise ValueError("PROFILE_AVATAR_SIZE_INVALID")
            with Image.open(io.BytesIO(body)) as picture:
                if (picture.format != {"image/jpeg": "JPEG", "image/png": "PNG",
                                       "image/webp": "WEBP"}[mime]
                    or not 1 <= picture.width <= 2048 or not 1 <= picture.height <= 2048
                    or getattr(picture, "n_frames", 1) != 1):
                    raise ValueError("PROFILE_AVATAR_IMAGE_INVALID")
                picture.verify()
            await check()
            sha = hashlib.sha256(body).hexdigest()
            receipt = digest({"kind": "qq_current_avatar_observation", "status": 200,
                "url_sha256": hashlib.sha256(str(target).encode()).hexdigest(),
                "body_sha256": sha, "mime_type": mime, "size_bytes": len(body),
                "observed_at": observed_at})
            return {"bot_qq_id": bot, "field": "avatar", "value": sha,
                    "observed_at": observed_at, "receipt_sha256": receipt}
    finally:
        try:
            if response is not None:
                await response.aclose()
            if transport is None:
                await delegate.aclose()
        finally:
            _private_download.reset(quiet)


async def collect(window, client, protocol, *, transport=None, resolver=resolve_public):
    if not due(window):
        return {"status": "IDLE"}
    local(window)
    permission = await window.check(client)
    if "account_actions" not in permission["capability_policies"]:
        return {"status": "DISABLED"}
    approved = await grant(window, client)
    if not approved["fields"]:
        return {"status": "DISABLED"}
    local(window)
    record(window, "PROFILE_PROBE_STARTED", {"authority": window.authority})

    async def check():
        if await grant(window, client) != approved:
            raise ValueError("PROFILE_GRANT_CHANGED")

    profiles = []
    if set(approved["fields"]) & {"nickname", "signature"}:
        try:
            data, at, receipt = await native_profile(window, protocol, check)
            profiles.extend(text_baselines(data, approved["fields"],
                window.config.bot_qq_id, at, receipt))
        except (ValueError, httpx.HTTPError):
            await check()
    if "avatar" in approved["fields"]:
        try:
            profiles.append(await avatar_baseline(window, check, transport=transport,
                                                   resolver=resolver))
        except (ValueError, httpx.HTTPError, OSError, TimeoutError):
            await check()
    if not profiles:
        return {"status": "UNAVAILABLE"}
    await check()
    body = {key: approved[key] for key in ("authority", "authorization_sha256")}
    body["profiles"] = profiles
    response = await owner_media_request(window, client, SUBMIT, body,
                                         purpose="account_actions", before=check)
    expected = [{"field": item["field"], "observed_at": item["observed_at"]}
                for item in profiles]
    if response != {"accepted": True, "profiles": expected}:
        raise ValueError("PROFILE_CORE_ACK_CHANGED")
    local(window)
    record(window, "PROFILE_PROBE_ACCEPTED", {"profiles": [{"field": row["field"],
        "observed_at": row["observed_at"], "value_sha256": digest(row["value"]),
        "receipt_sha256": row["receipt_sha256"]} for row in profiles]})
    return {"status": "ACCEPTED", "fields": [row["field"] for row in profiles]}


class ProfilePump:
    """A single cancellable idle task; journal cooldown survives service restarts."""

    def __init__(self, window, client, protocol, *, transport=None, resolver=resolve_public):
        self.window, self.client, self.protocol = window, client, protocol
        self.transport, self.resolver, self.task = transport, resolver, None
        self.next_run = 0

    async def execute(self):
        from .metrics import emit

        try:
            result = await collect(self.window, self.client, self.protocol,
                                  transport=self.transport, resolver=self.resolver)
            if result["status"] not in {"IDLE", "DISABLED"}:
                emit("account_profile_probe", status=result["status"])
        except asyncio.CancelledError:
            raise
        except Exception:
            # Read failure cannot prevent an authorized private reply; no baseline
            # is invented and Core still requires a valid one before profile writes.
            emit("account_profile_probe", status="DEFERRED")
        finally:
            self.next_run = time.monotonic() + COOLDOWN_SECONDS

    def interrupt(self):
        if self.running and not self.task.cancelling():
            self.task.cancel()

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def tick(self, *, idle):
        if not idle:
            self.interrupt()
        elif not self.running and time.monotonic() >= self.next_run and due(self.window):
            self.task = asyncio.create_task(self.execute())

    async def close(self):
        self.interrupt()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)
