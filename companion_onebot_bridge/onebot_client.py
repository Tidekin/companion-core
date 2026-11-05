import asyncio
import re
import time

import httpx

from .event_normalizer import decode, identifier
from .metrics import core_rpc_finished, http_finished, probe_finished


class PreflightBusy(Exception):
    """Core could not acquire its writer and did not enter the business transaction."""


async def bounded_post(
    client, path, body, *, headers=None, statuses=(200,), with_status=False, max_depth=8,
    max_bytes=65536, preflight=False,
):
    if type(max_bytes) is not int or not 1 <= max_bytes <= 6000000:
        raise ValueError("INVALID_RESPONSE_LIMIT")
    started, error = time.perf_counter_ns(), None
    try:
        async with client.stream("POST", path, json=body, headers=headers) as response:
            http_error = None
            try:
                busy_response = preflight and response.status_code == 503
                if not busy_response and response.status_code not in statuses:
                    response.raise_for_status()
                    raise ValueError("UNEXPECTED_HTTP_STATUS")
                content = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise ValueError("RESPONSE_TOO_LARGE")
            except BaseException as failure:
                http_error = failure
                raise
            finally:
                http_finished(response.request, http_error)
        result = decode(bytes(content), max_bytes, max_depth=max_depth)
        if busy_response:
            from companion_delivery_protocol.preflight import BUSY

            if result == {"error": BUSY}:
                raise PreflightBusy
            response.raise_for_status()
        return (response.status_code, result) if with_status else result
    except BaseException as failure:
        error = failure
        raise
    finally:
        core_rpc_finished(path, started, error)


class OneBotClient:
    def __init__(self, config, client):
        self.config, self.client = config, client

    async def action(self, name, body):
        if name not in {
            "get_login_info", "get_status", "get_version_info", "send_private_msg",
            "get_msg", "get_forward_msg", "get_friend_msg_history",
        }:
            raise ValueError("ACTION_NOT_ALLOWED")
        result = await bounded_post(
            self.client, "/" + name, body,
            max_depth=20 if name in {"get_msg", "get_forward_msg",
                                     "get_friend_msg_history"} else 8,
            max_bytes=1048576 if name == "get_friend_msg_history" else 65536,
        )
        if (
            result.get("status") != "ok"
            or type(result.get("retcode")) is not int
            or result["retcode"] != 0
            or not isinstance(result.get("data"), dict)
        ):
            raise ValueError("INVALID_ONEBOT_ACCEPTANCE")
        return result["data"]

    async def get_msg(self, reference):
        reference = identifier(reference, signed=True)
        if reference == "0":
            raise ValueError("INVALID_REFERENCE")
        # SnowLuma refreshes media URLs internally. Callers project text only and
        # must never download or persist those returned URLs.
        return await self.action("get_msg", {"message_id": int(reference)})

    async def friend_history(self, count):
        """The owner conversation's latest messages (both directions), newest last."""
        if type(count) is not int or not 1 <= count <= 20:
            raise ValueError("INVALID_HISTORY_COUNT")
        return await self.action("get_friend_msg_history",
                                 {"user_id": int(self.config.owner_qq_id), "count": count})

    async def get_forward_msg(self, reference):
        if not isinstance(reference, str) or not re.fullmatch(
            r"[A-Za-z0-9_+/=-]{1,256}", reference,
        ):
            raise ValueError("INVALID_REFERENCE")
        return await self.action("get_forward_msg", {"id": reference})

    async def probe(self):
        async def measured(name):
            started, error = time.perf_counter_ns(), None
            try:
                return await self.action(name, {})
            except BaseException as failure:
                error = failure
                raise
            finally:
                probe_finished(name, started, error)

        try:
            async with asyncio.TaskGroup() as group:
                identity_task = group.create_task(measured("get_login_info"))
                status_task = group.create_task(measured("get_status"))
                version_task = group.create_task(measured("get_version_info"))
        except ExceptionGroup as failures:
            # TaskGroup has joined every sibling. Preserve existing typed failure
            # handling while retaining simultaneous failures as the complete cause.
            first = failures
            while isinstance(first, ExceptionGroup):
                first = first.exceptions[0]
            raise first from failures
        identity, status, version = (
            identity_task.result(), status_task.result(), version_task.result(),
        )
        matches = identifier(identity.get("user_id")) == self.config.bot_qq_id
        expected = (
            version.get("app_name") == "SnowLuma" and version.get("app_version") == "2.0.3-node"
        )
        return {
            "account_matches": matches,
            "online": status.get("online") is True,
            "good": status.get("good") is True,
            # Do not echo arbitrary server strings into logs or reports.
            "expected_version_reported": expected,
            "connection_ready": matches
            and status.get("online") is True
            and status.get("good") is True
            and expected,
            "private_message_delivery_verified": False,
        }

    async def send_text(self, text):
        if not self.config.delivery_enabled:
            raise ValueError("DELIVERY_DISABLED")
        if not isinstance(text, str) or not 1 <= len(text) <= 20000:
            raise ValueError("INVALID_OUTBOUND_TEXT")
        # Arrays of text segments never interpret model-generated CQ as an action.
        return await self.send_message([{"type": "text", "data": {"text": text}}])

    async def send_message(self, message):
        if not self.config.delivery_enabled:
            raise ValueError("DELIVERY_DISABLED")
        result = await self.action(
            "send_private_msg",
            {
                "user_id": int(self.config.owner_qq_id),
                "message": message,
                "auto_escape": True,
            },
        )
        message_id = identifier(result.get("message_id"), signed=True)
        if message_id == "0":
            raise ValueError("INVALID_ONEBOT_MESSAGE_ID")
        return message_id

    async def send_native(self, prepared):
        from .native_actions import send

        if not self.config.delivery_enabled:
            raise ValueError("DELIVERY_DISABLED")
        return await send(self.client, prepared, self.config.owner_qq_id)

    async def send_account(self, prepared, item):
        from .native_actions import send_account

        if not self.config.delivery_enabled:
            raise ValueError("DELIVERY_DISABLED")
        return await send_account(self.client, prepared, item, self.config)


def http_client(url, token, *, transport=None):
    return httpx.AsyncClient(
        base_url=url,
        headers={"Authorization": "Bearer " + token},
        timeout=10,
        trust_env=False,
        follow_redirects=False,
        transport=transport,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    )
