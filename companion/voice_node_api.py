"""Outbound voice-node HTTP v2 boundary, with credentials separate from owner/bridge APIs."""

import hmac
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import ValidationError

from companion.contracts import CoreError
from companion.voice_jobs import VoiceJobs
from companion.voice_protocol import (
    MAX_RESULT_BODY_BYTES,
    ROUTE_PREFIX,
    VoiceClaimRequest,
    VoiceControlRequest,
    VoiceLeaseRef,
    VoiceRecovery,
    VoiceResult,
    VoiceStopRequest,
)


def _authenticate(request, *, terminal=False):
    settings = request.app.state.core.settings
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer ") or not terminal and not settings.voice.enabled:
        raise CoreError("VOICE_NODE_UNAUTHORIZED", 401)
    token = value[7:]
    if not token.isascii() or not 32 <= len(token) <= 256:
        raise CoreError("VOICE_NODE_UNAUTHORIZED", 401)
    reserved = [
        getattr(settings, key).get_secret_value()
        for key in ("bridge_token", "control_token", "diary_admin_token")
    ]
    if any(hmac.compare_digest(token.encode(), secret.encode()) for secret in reserved):
        raise CoreError("VOICE_NODE_UNAUTHORIZED", 401)
    matches = [
        key
        for key, node in settings.voice.nodes.items()
        if hmac.compare_digest(token, node.token.get_secret_value()) and (terminal or node.enabled)
    ]
    if len(matches) != 1:
        raise CoreError("VOICE_NODE_UNAUTHORIZED", 401)
    return matches[0]


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


async def _body(request, model, limit=65536):
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != ("application/json")
        or request.headers.get("content-encoding", "identity").lower().strip() != "identity"
    ):
        raise CoreError("VOICE_REQUEST_FORMAT_INVALID", 415)
    length = request.headers.get("content-length")
    if length is not None and (
        not length.isascii() or not length.isdecimal() or len(length) > 10 or int(length) > limit
    ):
        raise CoreError("VOICE_REQUEST_TOO_LARGE", 413)
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise CoreError("VOICE_REQUEST_TOO_LARGE", 413)
        data.extend(chunk)
    if length is not None and len(data) != int(length):
        raise CoreError("VOICE_REQUEST_INVALID", 400)
    try:
        value = json.loads(data, object_pairs_hook=_object)
        if model is VoiceClaimRequest and isinstance(value, dict) and (
            value.get("schema_version") == 1 or "request_id" in value):
            raise CoreError("VOICE_UPGRADE_REQUIRED", 410)
        return model.model_validate(value)
    except (ValueError, TypeError, ValidationError, RecursionError):
        raise CoreError("VOICE_REQUEST_INVALID", 400) from None


def _service(request):
    core = request.app.state.core
    return VoiceJobs(core.store, core.settings, core.clock)


def _json(value):
    return JSONResponse(value.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


def _path(job_id, ref):
    if job_id != ref.job_id:
        raise CoreError("VOICE_LEASE_PATH_MISMATCH", 409)


class VoiceRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                return await original(request)
            except CoreError as error:
                return JSONResponse({"error": error.code}, error.status,
                                    headers={"Cache-Control": "no-store"})
        return handle


def register_voice_node_routes(app):
    router = APIRouter(route_class=VoiceRoute)
    @router.api_route("/internal/v1/voice-node/{path:path}", methods=["GET", "POST", "PUT"],
                      include_in_schema=False)
    async def retired(request: Request, path: str):
        _authenticate(request, terminal=True)
        return JSONResponse({"error": "VOICE_UPGRADE_REQUIRED"}, 410,
                            headers={"Cache-Control": "no-store"})

    @router.get(ROUTE_PREFIX + "/session")
    async def session(request: Request):
        return _json(_service(request).session(_authenticate(request, terminal=True)))

    @router.get(ROUTE_PREFIX + "/nodes/{node_id}/cleanup")
    async def cleanup_status(node_id: str, request: Request):
        token = request.app.state.core.settings.control_token.get_secret_value()
        owner = bool(token and hmac.compare_digest(
            request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()))
        if not owner and _authenticate(request, terminal=True) != node_id:
            raise CoreError("VOICE_NODE_SCOPE_REQUIRED", 403)
        service = _service(request)
        service.session(node_id)
        rows = service.store.rows("SELECT job_id,generation,state,cleanup_state,cleanup_epoch "
            "FROM voice_jobs WHERE node_id=? AND cleanup_state='PENDING' ORDER BY job_id LIMIT 64",
            (node_id,))
        count = service.store.one("SELECT COUNT(*) n FROM voice_jobs WHERE node_id=? "
                                  "AND cleanup_state='PENDING'", (node_id,))["n"]
        return JSONResponse({"pending_count": count, "jobs": rows},
                            headers={"Cache-Control": "no-store"})

    @router.post(ROUTE_PREFIX + "/nodes/{node_id}/recover")
    async def recover(node_id: str, request: Request):
        token = request.app.state.core.settings.control_token.get_secret_value()
        if not token or not hmac.compare_digest(request.headers.get("authorization", "").encode(),
                                                ("Bearer " + token).encode()):
            raise CoreError("VOICE_OWNER_CONTROL_REQUIRED", 401)
        body = await _body(request, VoiceRecovery)
        return _json(_service(request).recover_node(node_id, body))

    @router.post(ROUTE_PREFIX + "/jobs/{job_id}/stop")
    async def stop(job_id: str, request: Request):
        node_id = _authenticate(request, terminal=True)
        body = await _body(request, VoiceStopRequest)
        _path(job_id, body.ref)
        return _json(_service(request).stop(node_id, body))

    @router.post(ROUTE_PREFIX + "/claim")
    async def claim(request: Request):
        node_id = _authenticate(request)
        body = await _body(request, VoiceClaimRequest)
        if request.headers.get("idempotency-key") != body.request_id:
            raise CoreError("VOICE_IDEMPOTENCY_KEY_REQUIRED", 400)
        return _json(_service(request).claim(node_id, body))

    @router.post(ROUTE_PREFIX + "/jobs/{job_id}/control")
    async def control(job_id: str, request: Request):
        node_id = _authenticate(request)
        body = await _body(request, VoiceControlRequest)
        _path(job_id, body.ref)
        return _json(_service(request).control(node_id, body))

    @router.post(ROUTE_PREFIX + "/jobs/{job_id}/input")
    async def input_bytes(job_id: str, request: Request):
        node_id = _authenticate(request)
        body = await _body(request, VoiceLeaseRef)
        _path(job_id, body)
        data, mime = _service(request).input(node_id, body)
        return Response(data, media_type=mime, headers={"Cache-Control": "no-store"})

    @router.put(ROUTE_PREFIX + "/jobs/{job_id}/result")
    async def result(job_id: str, request: Request):
        node_id = _authenticate(request)
        from companion_delivery_protocol.singing import MAX_RESULT_BODY_BYTES as song_limit

        node = request.app.state.core.settings.voice.nodes[node_id]
        body = await _body(request, VoiceResult,
                           song_limit if "sing" in node.capabilities else MAX_RESULT_BODY_BYTES)
        _path(job_id, body.ref)
        if request.headers.get("idempotency-key") != (
            f"voice-result:{body.ref.job_id}:{body.ref.generation}"
        ):
            raise CoreError("VOICE_IDEMPOTENCY_KEY_REQUIRED", 400)
        return _json(_service(request).submit(node_id, body))

    app.include_router(router)
