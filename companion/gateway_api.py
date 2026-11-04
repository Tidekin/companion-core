"""Loopback-only OpenAI text Passthrough. No Core conversation or memory construction."""

import asyncio
import ipaddress
import time
from contextlib import closing
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from companion import gateway
from companion.config import Settings
from companion.contracts import CoreError
from companion.gateway_contract import ChatRequest, RequestContext
from companion.store import Store, utcnow


def _error(code, status, request_id=None):
    headers = {"X-Request-Id": request_id} if request_id else None
    return JSONResponse({"error": {"message": code, "type": "gateway_error", "param": None,
                                  "code": code}}, status_code=status, headers=headers)


def create_app(settings=None, *, clock=utcnow):
    settings = settings or Settings.load()
    app = FastAPI(title="Companion Gateway", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def boundary(request, call_next):
        try:
            if request.client is None or not ipaddress.ip_address(request.client.host).is_loopback:
                raise CoreError("GATEWAY_LOOPBACK_REQUIRED", 403)
            gateway.config()
            return await call_next(request)
        except CoreError as error:
            return _error(error.code, error.status)
        except (ValueError, ValidationError):
            return _error("GATEWAY_INVALID_REQUEST", 400)

    def auth(store, request):
        for name in ("Idempotency-Key", "X-Companion-Mode", "X-Companion-Conversation-Id",
                     "X-Companion-World-Id"):
            if len(request.headers.getlist(name)) > 1:
                raise CoreError("GATEWAY_AMBIGUOUS_HEADER", 400)
        values = request.headers.getlist("authorization")
        if len(values) != 1 or not values[0].startswith("Bearer "):
            raise CoreError("GATEWAY_INVALID_KEY", 401)
        return gateway.authenticate(store, settings, values[0][7:], clock())

    @app.exception_handler(CoreError)
    async def core_error(request, error):
        return _error(error.code, error.status, getattr(request.state, "gateway_request_id", None))

    def read_receipt(store, request, request_id):
        grant = auth(store, request)
        result = gateway.receipt(store, settings, grant.client_id, request_id, clock())
        auth(store, request)
        now = clock()
        if datetime.fromisoformat(now) >= datetime.fromisoformat(result["expires_at"]):
            result = gateway.receipt(store, settings, grant.client_id, request_id, now)
        return result

    @app.get("/v1/models")
    async def models(request: Request):
        with closing(Store(settings.database)) as store:
            grant = auth(store, request)
            if request.headers.get("X-Companion-Mode", "passthrough") != "passthrough":
                raise CoreError("GATEWAY_MODE_NOT_IMPLEMENTED", 403)
            gateway.require_reference(store, settings, grant, clock())
            aliases = gateway.config().aliases
            return {"object": "list", "data": [{"id": item.model_alias, "object": "model",
                    "created": 0, "owned_by": "companion-core"}
                    for item in aliases if item.model_alias in grant.model_aliases]}

    @app.get("/v1/gateway/requests/{request_id}")
    async def receipt(request_id: str, request: Request):
        with closing(Store(settings.database)) as store:
            result = read_receipt(store, request, request_id)
            return JSONResponse(result, headers={"X-Request-Id": request_id})

    @app.post("/v1/chat/completions")
    async def complete(request: Request):
        with closing(Store(settings.database)) as store:
            grant = auth(store, request)
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 524288:
                    raise CoreError("GATEWAY_INPUT_TOO_LARGE", 400)
            try:
                body = ChatRequest.model_validate_json(data)
                context = RequestContext(mode=request.headers.get("X-Companion-Mode"),
                    conversation_id=request.headers.get("X-Companion-Conversation-Id"),
                    world_id=request.headers.get("X-Companion-World-Id"))
            except (ValidationError, ValueError):
                raise CoreError("GATEWAY_INVALID_REQUEST", 400) from None
            # Authentication may expire or be revoked during an untrusted slow upload.
            grant = auth(store, request)
            request_id = gateway.submit(store, settings, grant, body, context,
                                        request.headers.get("Idempotency-Key"), clock())
            request.state.gateway_request_id = request_id
            deadline = time.monotonic() + gateway.config().wait_seconds
            while True:
                result = read_receipt(store, request, request_id)
                if result["status"] == "SUCCEEDED":
                    return JSONResponse(result["completion"], headers={"X-Request-Id": request_id})
                if result["status"] not in {"QUEUED", "RUNNING"}:
                    return _error(result["error_code"] or "GATEWAY_RECEIPT_EXPIRED",
                                  result["error_status"] or 503,
                                  request_id)
                if time.monotonic() >= deadline:
                    return _error("GATEWAY_REQUEST_PENDING", 503, request_id)
                # The worker owns execution; disconnecting this waiter never resubmits it.
                await asyncio.sleep(0.05)

    return app
