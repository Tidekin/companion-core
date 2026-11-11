"""Native chat routes use original owner sessions and independent API Store sessions."""

import asyncio
import json
import time
from contextlib import nullcontext
from types import SimpleNamespace

from fastapi import Request
from pydantic import Field
from starlette.responses import StreamingResponse

from companion import companion_profile, native_chat
from companion.console_api import _operation, _request_fields
from companion.contracts import CoreError, StrictModel
from companion.privacy import ErasureLedger


class ProfileSettings(StrictModel):
    expected_settings_revision: int = Field(ge=0, strict=True)
    auto_name: bool | None = None
    auto_avatar: bool | None = None


class ProfileAlias(StrictModel):
    expected_settings_revision: int = Field(ge=0, strict=True)
    alias: str | None = Field(default=None, min_length=1, max_length=64)


class ProfileRestore(StrictModel):
    change_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=0, strict=True)
    pause_name: bool = False
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{8,64}$")


def _profile_event(view):
    return ("event: profile\ndata: "
            + json.dumps(view, ensure_ascii=False, separators=(",", ":")) + "\n\n")


def register_routes(app, settings, prefix, auth, authenticate):
    async def execute(request, operation, *, readonly=False):
        clock, check = app.state.core.clock, authenticate(request)

        def run(store):
            with store.read_snapshot() if readonly else nullcontext():
                session = check(store)
                context = SimpleNamespace(store=store, settings=settings, clock=clock,
                    ledger=ErasureLedger(settings.erasure_ledger), session=session)
                return operation(context, lambda: check(store))

        return await app.state.api_store.run(run)

    @app.post(prefix + "/devices/preview", dependencies=auth)
    async def preview(body: native_chat.Device, request: Request):
        return await execute(request, lambda ctx, check: native_chat.device_preview(ctx, body))

    @app.post(prefix + "/devices/authorize", dependencies=auth)
    async def authorize(body: native_chat.DeviceApproval, request: Request):
        captured = _request_fields(request)

        def apply(context, check):
            # One privileged enrollment per original login session. Retrying this
            # receipt cannot regrant a subsequently revoked device.
            _operation(context, captured,
                "native_device:" + context.session["token_sha256"], body.model_dump(),
                lambda: native_chat.authorize_device(context, body),
                recover=lambda key, fingerprint: native_chat.recover_authorization(context, body),
                owner_check=check)
            return native_chat.recover_authorization(context, body) or {"registered": False}

        return await execute(request, apply)

    @app.post(prefix + "/messages", dependencies=auth, status_code=202)
    async def submit(body: native_chat.Submit, request: Request):
        options = app.state.core.content.options
        return await execute(request, lambda ctx, check: native_chat.submit(
            ctx, body, content_options=options))

    @app.get(prefix + "/messages", dependencies=auth)
    async def history(request: Request, before: int | None = None, limit: int = 50):
        return await execute(request, lambda ctx, check: native_chat.history(
            ctx, before=before, limit=limit), readonly=True)

    @app.get(prefix + "/events", dependencies=auth)
    async def events(request: Request):
        # Admission before response headers preserves normal HTTP authentication errors.
        initial = await execute(request, lambda ctx, check: (
            native_chat.changes(ctx, {}), companion_profile.view(ctx.store, ctx.settings)),
                                readonly=True)

        async def stream():
            ((items, signatures), profile), heartbeat = initial, time.monotonic()
            seen = {}
            # Devices apply only a newer revision; reconnects start from this snapshot.
            yield _profile_event(profile)
            while True:
                for item in items:
                    body = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                    if seen.get(item["event_id"]) != body:
                        seen[item["event_id"]] = body
                        yield "event: turn\ndata: " + body + "\n\n"
                # Only the bounded recent window is retained per connection. A reconnect
                # starts with current snapshots; original task/status reads cover older work.
                present = set(signatures)
                seen = {key: value for key, value in seen.items() if key in present}
                if time.monotonic() - heartbeat >= 10:
                    yield ": heartbeat\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.25)
                if await request.is_disconnected():
                    return
                try:
                    (items, signatures), latest = await execute(request,
                        lambda ctx, check, prior=signatures: (native_chat.changes(ctx, prior),
                            companion_profile.view(ctx.store, ctx.settings)),
                        readonly=True)
                    if latest != profile:
                        profile = latest
                        yield _profile_event(profile)
                except CoreError as error:
                    # No old text accompanies a revoked/expired subscription.
                    yield "event: session\ndata: " + json.dumps({"error": error.code}) + "\n\n"
                    return

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get(prefix + "/profile", dependencies=auth)
    async def profile(request: Request):
        return await execute(request, lambda ctx, check: companion_profile.view(
            ctx.store, ctx.settings), readonly=True)

    @app.get(prefix + "/profile/changes", dependencies=auth)
    async def profile_changes(request: Request, before: int | None = None, limit: int = 50):
        return await execute(request, lambda ctx, check: {"changes": companion_profile.changes(
            ctx.store, before=before, limit=limit)}, readonly=True)

    def write(operation):
        def run(ctx, check):
            with ctx.store.transaction():
                check()
                return operation(ctx)
        return run

    @app.put(prefix + "/profile/settings", dependencies=auth)
    async def profile_settings(body: ProfileSettings, request: Request):
        return await execute(request, write(lambda ctx: companion_profile.update_settings(
            ctx.store, ctx.settings, ctx.clock(),
            expected_settings_revision=body.expected_settings_revision,
            auto_name=body.auto_name, auto_avatar=body.auto_avatar)))

    @app.put(prefix + "/profile/alias", dependencies=auth)
    async def profile_alias(body: ProfileAlias, request: Request):
        return await execute(request, write(lambda ctx: companion_profile.set_alias(
            ctx.store, ctx.settings, ctx.clock(),
            expected_settings_revision=body.expected_settings_revision, alias=body.alias)))

    @app.post(prefix + "/profile/restore", dependencies=auth)
    async def profile_restore(body: ProfileRestore, request: Request):
        return await execute(request, write(lambda ctx: companion_profile.restore(
            ctx.store, ctx.settings, ctx.clock(), change_id=body.change_id,
            expected_revision=body.expected_revision, pause_name=body.pause_name,
            operation_id=body.operation_id)))

    @app.get(prefix + "/messages/{event_id}", dependencies=auth)
    async def status(event_id: str, request: Request):
        return await execute(request, lambda ctx, check: native_chat.history(
            ctx, event_id=event_id), readonly=True)
