"""Native chat routes use original owner sessions and independent API Store sessions."""

import asyncio
import base64
import binascii
import json
import time
from contextlib import nullcontext
from types import SimpleNamespace

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import Field, ValidationError
from starlette.datastructures import UploadFile
from starlette.responses import Response, StreamingResponse

from companion import (
    companion_avatar,
    companion_profile,
    moments,
    native_chat,
    native_devices,
    native_images,
)
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


class AvatarMetadata(StrictModel):
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{8,64}$")
    # When set, the owner's upload becomes the avatar in the same transaction.
    expected_revision: int | None = Field(default=None, ge=0, strict=True)


class AvatarUpload(AvatarMetadata):
    mime_type: str = Field(pattern=r"^image/(png|jpeg|webp)$")
    # Base64 of at most MAX_UPLOAD_BYTES; Core checks the decoded bytes again.
    data_base64: str = Field(min_length=4, max_length=(companion_avatar.MAX_UPLOAD_BYTES + 2)
                             // 3 * 4)


class AvatarChoice(StrictModel):
    asset_id: str = Field(pattern=r"^avt_[a-f0-9]{32}$")
    expected_revision: int = Field(ge=0, strict=True)
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{8,64}$")


class MomentLike(StrictModel):
    liked: bool


class MomentVisibility(StrictModel):
    hidden: bool


class DeviceRevocation(StrictModel):
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{8,64}$")


def _decoded(text):
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as error:
        raise CoreError("AVATAR_IMAGE_INVALID", 400) from error


async def _upload_body(request, *, json_model, metadata_model, field, file_field, count, limit):
    """Both encodings enter the same authenticated operation with ordered originals."""
    media = request.headers.get("content-type", "application/json").split(";", 1)[0].strip().lower()
    try:
        if media == "application/json" or media.endswith("+json"):
            return json_model.model_validate_json(await request.body()), None
        if media != "multipart/form-data":
            raise CoreError("UNSUPPORTED_MEDIA_TYPE", 415)
        async with request.form(max_files=count, max_fields=1, max_part_size=128 * 1024) as form:
            values = form.getlist(field)
            uploads = form.getlist(file_field)
            if (set(form) != {field, file_field} or len(values) != 1
                    or not isinstance(values[0], str) or not 1 <= len(uploads) <= count):
                raise CoreError("INVALID_CONTRACT", 400)
            metadata = metadata_model.model_validate_json(values[0])
            files = []
            remaining = limit
            for part in uploads:
                if not isinstance(part, UploadFile):
                    raise CoreError("INVALID_CONTRACT", 400)
                if part.size is None or part.size > remaining:
                    raise CoreError("BODY_TOO_LARGE", 413)
                data = await part.read(remaining + 1)
                if len(data) > remaining:
                    raise CoreError("BODY_TOO_LARGE", 413)
                files.append((part.content_type, data))
                remaining -= len(data)
            return metadata, files
    except ValidationError as error:
        raise RequestValidationError(error.errors(include_input=False)) from None


def _feed_snapshot(store, previous=None):
    # Called inside one repeatable read. Only the public change marker is reusable;
    # the stream checks the session, device, original sources and active turns each tick.
    token = store.change_token()
    if previous is not None and token == previous[0]:
        return previous
    return token, moments.signature(store)


def _moment_event(signature):
    return "event: moment\ndata: " + json.dumps({"signature": signature}) + "\n\n"


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
    async def submit(request: Request):
        body, files = await _upload_body(request, json_model=native_chat.Submit,
            metadata_model=native_chat.Submit, field="message", file_field="images",
            count=native_images.MAX_IMAGES, limit=native_images.MAX_TOTAL_BYTES)
        if files is not None and "images" in body.model_fields_set:
            raise CoreError("INVALID_CONTRACT", 400)
        options = app.state.core.content.options
        return await execute(request, lambda ctx, check: native_chat.submit(
            ctx, body, content_options=options, files=files))

    @app.post(prefix + '/devices/{device_id}/revoke', dependencies=auth)
    async def revoke_device(device_id: str, body: DeviceRevocation, request: Request):
        captured = _request_fields(request)

        def apply(context, check):
            if context.session.get('native_device_id') not in (None, device_id):
                raise CoreError('NATIVE_SESSION_SCOPE_REQUIRED', 403)
            return _operation(context, captured,
                'native_revoke:'+body.operation_id, {'device_id': device_id},
                lambda: native_devices.revoke(context, device_id), owner_check=check)

        return await execute(request, apply)

    @app.get(prefix + "/messages", dependencies=auth)
    async def history(request: Request, before: int | None = None, limit: int = 50):
        return await execute(request, lambda ctx, check: native_chat.history(
            ctx, before=before, limit=limit), readonly=True)

    @app.get(prefix + "/events", dependencies=auth)
    async def events(request: Request):
        # Admission before response headers preserves normal HTTP authentication errors.
        initial = await execute(request, lambda ctx, check: (
            native_chat.changes(ctx, {}), companion_profile.view(ctx.store, ctx.settings),
            _feed_snapshot(ctx.store)), readonly=True)

        async def stream():
            ((items, signatures), profile, feed), heartbeat = initial, time.monotonic()
            seen, announced = {}, False
            while True:
                for item in items:
                    body = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                    if seen.get(item["event_id"]) != body:
                        seen[item["event_id"]] = body
                        yield "event: turn\ndata: " + body + "\n\n"
                if not announced:
                    # After current turns; devices apply only a newer profile revision.
                    announced = True
                    yield _profile_event(profile)
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
                    (items, signatures), latest, current = await execute(request,
                        lambda ctx, check, prior=signatures, previous=feed: (
                            native_chat.changes(ctx, prior),
                            companion_profile.view(ctx.store, ctx.settings),
                            _feed_snapshot(ctx.store, previous)),
                        readonly=True)
                    if latest != profile:
                        profile = latest
                        yield _profile_event(profile)
                    changed = current[1] != feed[1]
                    feed = current
                    if changed:
                        yield _moment_event(feed[1])
                except CoreError as error:
                    # No old text accompanies a revoked/expired subscription.
                    yield "event: session\ndata: " + json.dumps({"error": error.code}) + "\n\n"
                    return

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get(prefix + "/moments", dependencies=auth)
    async def moments_feed(request: Request, before: str | None = None, limit: int = 30):
        if not 1 <= limit <= 60:
            raise CoreError("INVALID_PAGE", 400)
        return await execute(request, lambda ctx, check: moments.page(
            moments.feed, ctx.store, before, limit), readonly=True)

    @app.get(prefix + "/moments/latest", dependencies=auth)
    async def moments_latest(request: Request, after: str | None = None):
        # The chat page's preview card: her newest post since the device last looked.
        return await execute(request, lambda ctx, check: {"item": moments.latest_unseen(
            ctx.store, after)}, readonly=True)

    @app.get(prefix + "/moments/{moment_id}/comments", dependencies=auth)
    async def moment_comments(moment_id: str, request: Request):
        return await execute(request, lambda ctx, check: {"items": moments.comments(
            ctx.store, moment_id)}, readonly=True)

    @app.get(prefix + "/moments/memories", dependencies=auth)
    async def moments_memories(request: Request):
        return await execute(request, lambda ctx, check: {"items": moments.memories(
            ctx.store, ctx.settings, ctx.clock())}, readonly=True)

    @app.post(prefix + "/moments/{moment_id}/like", dependencies=auth)
    async def moment_like(moment_id: str, body: MomentLike, request: Request):
        return await execute(request, write(lambda ctx: moments.set_liked(
            ctx.store, moment_id, body.liked, ctx.clock())))

    @app.post(prefix + "/moments/{moment_id}/visibility", dependencies=auth)
    async def moment_visibility(moment_id: str, body: MomentVisibility, request: Request):
        return await execute(request, write(lambda ctx: moments.set_hidden(
            ctx.store, moment_id, body.hidden, ctx.clock())))

    @app.post(prefix + "/moments/{moment_id}/forget", dependencies=auth)
    async def moment_forget(moment_id: str, request: Request):
        def run(ctx, check):
            with ctx.store.transaction(control_write=True):
                check()
                if not moment_id.startswith("moment_"):
                    # The owner's own post is an owner message: native erasure removes it,
                    # its images and her reply, and replays from older backups.
                    moments.require_target(ctx.store, moment_id)
                    ctx.ledger.expire_events(ctx.store, [moment_id], ctx.clock())
                    return {"moment_id": moment_id, "state": "ERASED"}
                return moments.forget(ctx.store, ctx.ledger, moment_id, ctx.clock())

        return await execute(request, run)

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

    @app.get(prefix + "/profile/avatars", dependencies=auth)
    async def profile_avatars(request: Request):
        return await execute(request, lambda ctx, check: {
            "avatars": companion_avatar.available(ctx.store)}, readonly=True)

    @app.post(prefix + "/profile/avatars", dependencies=auth)
    async def profile_avatar_upload(request: Request):
        body, files = await _upload_body(request, json_model=AvatarUpload,
            metadata_model=AvatarMetadata, field="profile", file_field="image",
            count=1, limit=companion_avatar.MAX_UPLOAD_BYTES)
        mime, data = files[0] if files else (body.mime_type, _decoded(body.data_base64))

        def upload(ctx):
            if body.expected_revision is not None and companion_profile.current(
                    ctx.store, ctx.settings)["revision"] != body.expected_revision:
                # Refuse before any bytes reach disk; a rollback cannot remove a file.
                raise CoreError("PROFILE_REVISION_CONFLICT", 409)
            asset = companion_avatar.create(ctx.store, ctx.clock(), data=data,
                                            mime=mime,
                                            operation_id=body.operation_id)
            result = {"asset": {key: asset[key] for key in (
                "asset_id", "width", "height", "created_at")}}
            if body.expected_revision is not None:
                result["profile"] = companion_profile.set_avatar(
                    ctx.store, ctx.settings, ctx.clock(), asset_id=asset["asset_id"],
                    expected_revision=body.expected_revision,
                    operation_id=body.operation_id)
            return result

        return await execute(request, write(upload))

    @app.put(prefix + "/profile/avatar", dependencies=auth)
    async def profile_avatar(body: AvatarChoice, request: Request):
        return await execute(request, write(lambda ctx: companion_profile.set_avatar(
            ctx.store, ctx.settings, ctx.clock(), asset_id=body.asset_id,
            expected_revision=body.expected_revision, operation_id=body.operation_id)))

    @app.get(prefix + "/profile/assets/{asset_id}", dependencies=auth)
    async def profile_asset(asset_id: str, request: Request):
        data = await execute(request, lambda ctx, check: companion_avatar.read_bytes(
            ctx.store, asset_id), readonly=True)
        # Asset ids never change content; the session check above still runs each time.
        return Response(data, media_type="image/png", headers={
            "Cache-Control": "private, max-age=86400, immutable",
            "X-Content-Type-Options": "nosniff"})

    @app.get(prefix + "/messages/{event_id}/images/{slot}", dependencies=auth)
    async def message_image(event_id: str, slot: int, request: Request):
        data, mime = await execute(request, lambda ctx, check: native_chat.image(
            ctx, event_id, slot), readonly=True)
        # The session and the message's erasure state are checked on every request.
        return Response(data, media_type=mime, headers={
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @app.get(prefix + "/moments/{moment_id}/images/{slot}", dependencies=auth)
    async def moment_image(moment_id: str, slot: int, request: Request):
        from companion import moment_images

        data, mime = await execute(request, lambda ctx, check: moment_images.read(
            ctx.store, moment_id, slot), readonly=True)
        return Response(data, media_type=mime, headers={
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @app.get(prefix + "/replies/{message_id}/sticker", dependencies=auth)
    async def reply_sticker(message_id: str, request: Request):
        data, mime = await execute(request, lambda ctx, check: native_chat.sticker(
            ctx, message_id), readonly=True)
        return Response(data, media_type=mime, headers={
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @app.get(prefix + "/messages/{event_id}", dependencies=auth)
    async def status(event_id: str, request: Request):
        return await execute(request, lambda ctx, check: native_chat.history(
            ctx, event_id=event_id), readonly=True)

    @app.post(prefix + "/messages/{event_id}/reply-now", dependencies=auth)
    async def reply_now(event_id: str, request: Request):
        return await execute(request, lambda ctx, check: native_chat.reply_now(
            ctx, event_id, check))
