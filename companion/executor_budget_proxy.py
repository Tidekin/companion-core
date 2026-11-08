"""Private, per-task capped model proxy. Only this service has the model API key."""

import asyncio
import json
import os
import secrets
import time
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response

from companion.executive_protocol import ExecutionEnvelope


def create_budget_proxy(
    admin_token,
    upstream_url,
    api_key,
    model,
    input_rate,
    output_rate,
    transport=None,
    clock=time.monotonic,
):
    if (
        len(admin_token) < 24
        or not api_key
        or admin_token == api_key
        or input_rate <= 0
        or output_rate <= 0
    ):
        raise ValueError(
            "proxy requires distinct admin credentials and known positive upper prices"
        )
    upstream = urlsplit(upstream_url)
    if (
        upstream.scheme != "https"
        or not upstream.hostname
        or upstream.username
        or upstream.password
        or upstream.query
        or upstream.fragment
    ):
        raise ValueError("model upstream requires HTTPS without credentials or query")
    app, lock, grants = FastAPI(), asyncio.Lock(), {}

    class GrantTransport(httpx.AsyncBaseTransport):
        def __init__(self, token, grant):
            self.token, self.grant = token, grant
            self.delegate = transport or httpx.AsyncHTTPTransport(trust_env=False)

        async def handle_async_request(self, request):
            async with lock:
                grant = self.grant
                if (grants.get(self.token) is not grant or grant["revoked"]
                    or grant["expires"] <= clock()):
                    raise HTTPException(403, "MODEL_GRANT_EXPIRED")
                if grant["calls"] >= grant["request"].max_model_calls:
                    raise HTTPException(429, "MODEL_CALL_LIMIT")
                grant["calls"] += 1
                grant["reserved_micro_usd"] += grant["per_call"]
            # No await separates the final admission from transport dispatch.
            # Revocation after this boundary retains the in-flight responsibility.
            return await self.delegate.handle_async_request(request)

        async def aclose(self):
            await self.delegate.aclose()

    def admin(value):
        if not secrets.compare_digest(value or "", "Bearer " + admin_token):
            raise HTTPException(401, "UNAUTHORIZED")

    @app.post("/internal/v1/reservations")
    async def reserve(body: ExecutionEnvelope, authorization: str = Header(default="")):
        admin(authorization)
        # UTF-8 byte bound is conservative for text and tool schemas; no image inputs accepted.
        one_call = (
            body.max_input_bytes * input_rate + body.max_output_tokens * output_rate + 999999
        ) // 1000000
        async with lock:
            for key in list(grants):
                if grants[key]["expires"] <= clock():
                    del grants[key]
            if len(grants) >= 100:
                raise HTTPException(429, "PROXY_CAPACITY")
            token = secrets.token_urlsafe(32)
            grants[token] = {
                "request": body,
                "calls": 0,
                "reserved_micro_usd": 0,
                "per_call": one_call,
                "expires": clock() + body.timeout_seconds + 5,
                "revoked": False,
            }
        return {"token": token, "model": model}

    @app.get("/internal/v1/reservations/{token}")
    async def usage(token: str, authorization: str = Header(default="")):
        admin(authorization)
        grant = grants.get(token)
        if not grant:
            raise HTTPException(404, "RESERVATION_MISSING")
        return {"calls": grant["calls"], "reserved_micro_usd": grant["reserved_micro_usd"]}

    @app.delete("/internal/v1/reservations/{token}")
    async def revoke(token: str, authorization: str = Header(default="")):
        admin(authorization)
        async with lock:
            if grant := grants.get(token):
                # Keep counters readable until expiry, including late/unknown calls.
                grant["revoked"] = True
        return {"revoked": True}

    @app.post("/v1/chat/completions")
    async def complete(request: Request, authorization: str = Header(default="")):
        token = authorization.removeprefix("Bearer ")
        async with lock:
            grant = grants.get(token)
            if not grant or grant["revoked"] or grant["expires"] <= clock():
                raise HTTPException(403, "MODEL_GRANT_EXPIRED")
            bound = grant["request"]
        # Upload is untrusted and can stall. It must not block revocation or
        # admissions for another task; it does not reserve a provider call yet.
        data = bytearray()
        async with asyncio.timeout(max(0.1, grant["expires"] - clock())):
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > bound.max_input_bytes:
                    raise HTTPException(413, "MODEL_INPUT_LIMIT")
        body = json.loads(data)
        if any(
            isinstance(message.get("content"), list)
            and any(
                not isinstance(part, dict) or part.get("type") != "text"
                for part in message["content"]
            )
            for message in body.get("messages", [])
        ):
            raise HTTPException(400, "MODEL_TEXT_ONLY")
        remaining = max(0.1, grant["expires"] - clock())
        body["model"], body["max_tokens"] = model, bound.max_output_tokens
        body["n"] = 1
        body.pop("best_of", None)
        body.pop("max_completion_tokens", None)
        # All requests, including failed/unknown requests and internal compaction, consume a slot.
        async with (
            asyncio.timeout(remaining),
            httpx.AsyncClient(
                timeout=remaining, trust_env=False, follow_redirects=False,
                transport=GrantTransport(token, grant),
            ) as client,
        ):
            async with client.stream(
                "POST",
                upstream_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + api_key, "Accept-Encoding": "identity"},
                json=body,
            ) as upstream:
                if (
                    upstream.headers.get("content-encoding", "identity").strip().lower()
                    != "identity"
                ):
                    raise HTTPException(502, "MODEL_ENCODING_UNSUPPORTED")
                output = bytearray()
                async for chunk in upstream.aiter_bytes():
                    output.extend(chunk)
                    if len(output) > 4 * 1024 * 1024:
                        raise HTTPException(502, "MODEL_OUTPUT_LIMIT")
                return Response(
                    content=bytes(output),
                    status_code=upstream.status_code,
                    media_type="text/event-stream" if body.get("stream") else "application/json",
                )

    return app


def app_from_env():
    return create_budget_proxy(
        os.environ["BUDGET_PROXY_ADMIN_TOKEN"],
        os.environ["EXECUTOR_MODEL_URL"],
        os.environ["EXECUTOR_MODEL_KEY"],
        os.environ["EXECUTOR_MODEL_NAME"],
        int(os.environ["EXECUTOR_INPUT_RATE"]),
        int(os.environ["EXECUTOR_OUTPUT_RATE"]),
    )
