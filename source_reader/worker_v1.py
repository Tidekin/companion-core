"""Opt-in v3 HTTP worker for Reading v1 evidence; no local durable state."""

import asyncio
import re
from contextlib import AsyncExitStack
from datetime import UTC, datetime

import httpx

from source_reader.network import ReadFailure
from source_reader.protocol import decode_lease, exact_hosts
from source_reader.reading import read_observation
from source_reader.web import WebHTTP, WebReader

VALIDATION_TIMEOUT = 8.0


def validation_error(response):
    try:
        code = response.json()["error"]
        if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", code):
            return code
    except (ValueError, KeyError, TypeError):
        pass
    return "READER_VALIDATION_FAILED"


async def tick(
    core_client, reader, adapter_id, *, source, local_hosts=None, renderer=None,
    validation_interval=0.75, clock=lambda: datetime.now(UTC),
    media_limits=None, media_work_dir=None,
):
    if (not isinstance(adapter_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", adapter_id)
        or not 0.5 <= validation_interval <= 1):
        raise ValueError("Invalid reader identity or validation interval")
    local_scope = exact_hosts(local_hosts) if local_hosts is not None else None
    prefix = "/internal/v3/readers/" + adapter_id
    snapshot = getattr(reader, "grant_snapshot", lambda **kwargs: None)
    current_grant = snapshot(required=True)
    lease_request = {"source_grant": current_grant} if current_grant else {}
    if media_limits is not None:
        from companion.bili_video_protocol import CAPABILITY

        if source != "bilibili":
            raise ValueError("media capability is Bili-only")
        lease_request["capabilities"] = [CAPABILITY]
    response = await core_client.post(prefix + "/lease", json=lease_request)
    response.raise_for_status()
    lease = decode_lease(response.json(), source, clock(), media_limits=media_limits)
    if lease is None:
        return False
    item, context, hosts, until = lease
    if item.get("source_grant") != current_grant:
        raise ReadFailure("SOURCE_GRANT_LEASE_MISMATCH")
    path = prefix + "/" + item["request_id"]
    binding = {"lease_token": item["lease_token"]}
    if current_grant is not None:
        binding["source_grant"] = current_grant
    cancelled, validation_lock = asyncio.Event(), asyncio.Lock()
    stop_code = None

    async def validate_read(bound_context):
        if bound_context != context:
            raise ReadFailure("READER_CONTEXT_CHANGED")
        async with validation_lock:
            remaining = (until - clock()).total_seconds()
            if remaining <= 0:
                raise ReadFailure("STALE_READER_LEASE")
            if snapshot(required=True) != current_grant:
                raise ReadFailure("SOURCE_GRANT_REVISION_CHANGED")
            # Core answers from a SQLite write transaction; under lock contention that
            # alone can take seconds, which must not turn into a cancelled read.
            wait = min(VALIDATION_TIMEOUT, remaining)
            async with asyncio.timeout(wait):
                response = await core_client.post(path + "/validate", json=binding, timeout=wait)
            if response.status_code != 200:
                raise ReadFailure(validation_error(response))
            try:
                decision = response.json()
            except ValueError:
                raise ReadFailure("READER_VALIDATION_FAILED") from None
            if (not isinstance(decision, dict) or set(decision) != {"valid"}
                or decision["valid"] is not True):
                raise ReadFailure("READER_VALIDATION_FAILED")
            if until <= clock():
                raise ReadFailure("STALE_READER_LEASE")
            if snapshot(required=True) != current_grant:
                raise ReadFailure("SOURCE_GRANT_REVISION_CHANGED")

    async def watch():
        nonlocal stop_code
        while True:
            await asyncio.sleep(validation_interval)
            try:
                await validate_read(context)
            except ReadFailure as error:
                stop_code = str(error)
                cancelled.set()
                return
            except (TimeoutError, httpx.HTTPError):
                stop_code = "READER_VALIDATION_UNKNOWN"
                cancelled.set()
                return

    async with AsyncExitStack() as stack:
        if source == "web":
            # Only Core's lease can grant hosts. A local option can reduce that grant.
            if local_scope is not None:
                if hosts == {"*"}:
                    hosts = local_scope
                elif local_scope != {"*"}:
                    hosts &= local_scope
            if not hosts:
                result = {
                    "schema_version": 1, "status": "FAILED", "observations": [],
                    "error_code": "READER_HOST_SCOPE_EMPTY", "used_requests": 0,
                    "used_bytes": 0, "observed_requests": 0, "observed_bytes": 0,
                }
                response = await core_client.post(path + "/result", json={**result, **binding})
                response.raise_for_status()
                return True
            public = await stack.enter_async_context(WebHTTP(hosts))
            reader = WebReader(public, renderer, clock=lambda: clock().isoformat())
        watcher = asyncio.create_task(watch())
        try:
            if item.get("media") is not None:
                from source_reader.bili_media import run_media

                return await run_media(
                    core_client, reader, item, context, path, validate_read,
                    limits=media_limits, until=until, cancel_event=cancelled,
                    clock=clock, work_dir=media_work_dir,
                )
            remaining = min(20.0, (until - clock()).total_seconds())
            # Expiry between decoding and scheduling must not dispatch a new read.
            if remaining <= 0:
                raise ReadFailure("STALE_READER_LEASE")
            result = await read_observation(
                reader, item, context, validate_read, cancel_event=cancelled,
                timeout_seconds=remaining, clock=lambda: clock().isoformat(),
            )
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    if stop_code is not None:
        result.update(status="CANCELLED", observations=[], error_code=stop_code)
        if result["observed_requests"]:
            result.update(used_requests=None, used_bytes=None)
    # A receipt timeout is not permission to repeat provider I/O. Core owns recovery.
    response = await core_client.post(path + "/result", json={**result, **binding})
    response.raise_for_status()
    return True
