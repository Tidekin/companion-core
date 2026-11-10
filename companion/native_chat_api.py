"""Native chat routes use original owner sessions and independent API Store sessions."""

from contextlib import nullcontext
from types import SimpleNamespace

from fastapi import Request

from companion import native_chat
from companion.console_api import _operation, _request_fields
from companion.privacy import ErasureLedger


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

    @app.get(prefix + "/messages/{event_id}", dependencies=auth)
    async def status(event_id: str, request: Request):
        return await execute(request, lambda ctx, check: native_chat.history(
            ctx, event_id=event_id), readonly=True)
