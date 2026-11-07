"""Same-origin owner console. The browser never receives transport or model credentials."""

import hashlib
import hmac
import json
import secrets
from datetime import date
from types import SimpleNamespace
from typing import Literal

from fastapi import Depends, Header, Request, Response
from pydantic import Field, SecretStr, StrictFloat, StrictInt
from starlette.datastructures import MutableHeaders

from companion import cloud_budget, console_read, delegation, operator_config
from companion.affect import AffectEngine
from companion.contracts import CoreError, StrictModel
from companion.diary import DiaryCorrection, DiaryService
from companion.goals import GoalEngine
from companion.memory import Memory
from companion.privacy import ErasureLedger
from companion.reply_recovery import RecoveryRequest
from companion.source_grants import SourceGrantService
from companion.store import after, encode
from companion.transport import reconcile_delivery, set_proactive_paused

COOKIE = "companion_owner_session"
# These native Memory failures leave no committed change after its transaction rolls back.
# Keep allowlists at each caller: a generic CoreError is not proof of no effect.
MEMORY_REJECTIONS = frozenset(
    {
        "MEMORY_NOT_FOUND",
        "MEMORY_NOT_ACTIVE",
        "CORRECTION_REQUIRED",
        "CREDENTIAL_MEMORY_DENIED",
        "CORRECTION_NOT_APPLICABLE",
    }
)


class Login(StrictModel):
    password: SecretStr


class Mode(StrictModel):
    target: Literal["diary", "life", "proactive"]
    enabled: bool = Field(strict=True)


class MemoryChange(StrictModel):
    decision: Literal["confirm", "correct", "forget"]
    corrected_content: str | None = Field(default=None, min_length=1, max_length=500)


class DeliveryReconciliation(StrictModel):
    outcome: Literal["SENT", "CANCELLED"]


class Delegation(StrictModel):
    mode: Literal["owner", "propose", "self"]
    allow_pause: bool = Field(strict=True)
    ranges: dict[str, dict[str, StrictInt | StrictFloat]] = Field(
        default_factory=dict, max_length=4
    )


class AdjustmentDecision(StrictModel):
    decision: Literal["approve", "reject", "revert"]


class _PrivateHeaders:
    def __init__(self, app, *, prefix):
        self.app, self.prefix = app, prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(self.prefix):
            return await self.app(scope, receive, send)

        async def private_start(message):
            if message["type"] == "http.response.start":
                # The TLS proxy also owns static and internal-redirect headers.
                MutableHeaders(scope=message).update(
                    {
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                        "Referrer-Policy": "no-referrer",
                        "X-Frame-Options": "DENY",
                        "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                        "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                    }
                )
            await send(message)

        await self.app(scope, receive, private_start)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _origin(request, settings):
    if request.headers.get("origin") != settings.console.public_origin:
        raise CoreError("CONSOLE_ORIGIN_REQUIRED", 403)


def _credential(settings):
    return digest("companion/session/v1:" + settings.control_token.get_secret_value())


def _request_fields(request):
    """Materialize finite authentication and receipt fields before entering a worker."""
    return SimpleNamespace(
        method=request.method,
        url=SimpleNamespace(path=request.url.path),
        cookies={COOKIE: request.cookies.get(COOKIE, "")},
        headers={name: request.headers.get(name, "") for name in ("origin", "x-csrf-token")},
    )


def _check_owner(core, request):
    settings = core.settings
    if not settings.console.enabled:
        raise CoreError("CONSOLE_DISABLED", 404)
    if not settings.control_token.get_secret_value():
        raise CoreError("CONSOLE_OWNER_CREDENTIAL_REQUIRED", 503)
    token = request.cookies.get(COOKIE, "")
    row = (
        core.store.one(
            (
                'SELECT console_sessions.token_sha256 AS token_sha256, '
                'console_sessions.csrf_token AS csrf_token, '
                'console_sessions.credential_tag AS credential_tag, '
                'console_sessions.created_at AS created_at, '
                'console_sessions.expires_at AS expires_at FROM '
                'console_sessions AS console_sessions WHERE '
                'console_sessions.token_sha256 = %s'
            ),
            (digest(token),),
        )
        if token
        else None
    )
    if (
        not row
        or row["expires_at"] <= core.clock()
        or not hmac.compare_digest(row["credential_tag"], _credential(settings))
    ):
        raise CoreError("CONSOLE_LOGIN_REQUIRED", 401)
    if request.method not in {"GET", "HEAD"}:
        _origin(request, settings)
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), row["csrf_token"]):
            raise CoreError("CONSOLE_CSRF_REQUIRED", 403)
    return row


def _operation(
    core, request, key, body, apply, *, recover=None, rejected_errors=frozenset(), owner_check=None
):
    if not key or len(key) > 128 or not key.isascii():
        raise CoreError("IDEMPOTENCY_KEY_REQUIRED", 400)
    s = core.store
    fingerprint = digest(encode([request.method, request.url.path, body]))
    result = None
    with s.transaction():
        if owner_check is not None:
            owner_check()
        now = core.clock()
        row = s.one(
            "SELECT console_operations.request_id AS request_id, "
            "console_operations.request_sha256 AS request_sha256, console_operations.state AS "
            "state, console_operations.result_json AS result_json, console_operations.created_at "
            "AS created_at, console_operations.updated_at AS updated_at FROM console_operations AS "
            "console_operations WHERE console_operations.request_id = %s",
            (key,),
        )
        if row:
            if row["request_sha256"] != fingerprint:
                raise CoreError("IDEMPOTENCY_CONFLICT", 409)
            if row["state"] == "REJECTED":
                failure = json.loads(row["result_json"])
                if (
                    set(failure) != {"error", "status"}
                    or failure["error"] not in rejected_errors
                    or type(failure["status"]) is not int
                    or not 400 <= failure["status"] < 500
                ):
                    raise CoreError("CONSOLE_OPERATION_RECONCILIATION_REQUIRED", 409)
                raise CoreError(failure["error"], failure["status"])
            if row["state"] != "COMPLETE":
                result = recover(key, fingerprint) if recover is not None else None
                if result is None:
                    raise CoreError("CONSOLE_OPERATION_RECONCILIATION_REQUIRED", 409)
            else:
                return json.loads(row["result_json"])
        else:
            s.insert(
                "console_operations",
                request_id=key,
                request_sha256=fingerprint,
                state="STARTED",
                created_at=now,
                updated_at=now,
            )
    # Native services own their transactions and erasure replay. A process death
    # between native commit and this receipt never blindly repeats the operation.
    if result is None:
        try:
            result = apply()
        except CoreError as error:
            if error.code in rejected_errors and not s.in_transaction:
                with s.transaction():
                    s.execute(
                        "UPDATE console_operations SET state = 'REJECTED', result_json = %s, "
                        "updated_at = %s WHERE request_id = %s AND state = 'STARTED'",
                        (encode({"error": error.code, "status": error.status}), core.clock(), key),
                    )
                    s.audit(
                        "console.operation_rejected",
                        key,
                        "console",
                        core.clock(),
                        code=error.code,
                        status=error.status,
                    )
            raise
    safe = {
        name: result[name]
        for name in (
            "memory_id",
            "state",
            "supersedes",
            "entry_id",
            "version",
            "enabled",
            "target",
        )
        if name in result
    }
    if not safe:
        safe = {"state": "COMPLETE"}
    with s.transaction():
        s.execute(
            "UPDATE console_operations SET state = 'COMPLETE', result_json = %s, updated_at "
            "= %s WHERE request_id = %s",
            (encode(safe), core.clock(), key),
        )
        s.audit("console.operation", key, "console", core.clock(), route=request.url.path)
    return safe


def register_console_routes(app, settings):
    prefix = "/v1/console"

    def enabled():
        if not settings.console.enabled:
            raise CoreError("CONSOLE_DISABLED", 404)
        if not settings.control_token.get_secret_value():
            raise CoreError("CONSOLE_OWNER_CREDENTIAL_REQUIRED", 503)

    def check_owner(request: Request):
        return _check_owner(app.state.core, request)

    async def owner(request: Request):
        return check_owner(request)

    app.add_middleware(_PrivateHeaders, prefix=prefix)

    @app.get("/console/")
    async def index():
        enabled()
        return Response(headers={"X-Accel-Redirect": "/_console_assets/index.html"})

    @app.get("/console/{asset}")
    async def static(asset: str):
        enabled()
        if asset not in {"app.js", "app.css", "icon.svg"}:
            raise CoreError("NOT_FOUND", 404)
        return Response(headers={"X-Accel-Redirect": "/_console_assets/" + asset})

    @app.post(prefix + "/session")
    async def login(body: Login, request: Request, response: Response):
        enabled()
        _origin(request, settings)
        clock, captured = app.state.core.clock, _request_fields(request)
        host = request.client.host if request.client else "unknown"

        def issue(s):
            invalid = False
            with s.transaction():
                enabled()
                _origin(captured, settings)
                now = clock()
                secret = settings.control_token.get_secret_value().encode()
                client_tag = hmac.new(secret, host.encode(), hashlib.sha256).hexdigest()
                s.execute("DELETE FROM console_login_attempts WHERE expires_at <= %s", (now,))
                row = s.one(
                    "SELECT console_login_attempts.client_tag AS client_tag, "
                    "console_login_attempts.attempts AS attempts, "
                    "console_login_attempts.expires_at AS expires_at FROM console_login_attempts "
                    "AS console_login_attempts WHERE console_login_attempts.client_tag = %s",
                    (client_tag,),
                )
                if row and row["attempts"] >= 5:
                    raise CoreError("CONSOLE_LOGIN_RATE_LIMIT", 429)
                if not hmac.compare_digest(body.password.get_secret_value().encode(), secret):
                    s.execute(
                        (
                            (
                                'INSERT INTO console_login_attempts VALUES (%s,1,%s) ON '
                                'CONFLICT(client_tag) DO UPDATE SET '
                                'attempts=console_login_attempts.attempts+1'
                            )
                        ),
                        (client_tag, after(now, 300)),
                    )
                    invalid = True
                else:
                    s.execute(
                        "DELETE FROM console_login_attempts WHERE client_tag = %s", (client_tag,)
                    )
                    s.execute("DELETE FROM console_sessions WHERE expires_at <= %s", (now,))
                    s.execute(
                        "DELETE FROM console_sessions WHERE token_sha256 IN (SELECT "
                        "token_sha256 FROM console_sessions ORDER BY created_at DESC NULLS LAST "
                        "OFFSET %s)",
                        (settings.console.max_sessions - 1,),
                    )
                    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                    seconds = settings.console.session_seconds
                    expires = after(now, seconds)
                    s.insert(
                        "console_sessions",
                        token_sha256=digest(token),
                        csrf_token=csrf,
                        credential_tag=_credential(settings),
                        created_at=now,
                        expires_at=expires,
                    )
                    s.audit("console.login", "owner", "console", now)
            if invalid:
                raise CoreError("CONSOLE_LOGIN_REQUIRED", 401)
            return token, csrf, expires, seconds

        token, csrf, expires, seconds = await app.state.api_store.run(issue)
        response.set_cookie(
            COOKIE,
            token,
            max_age=seconds,
            secure=True,
            httponly=True,
            samesite="strict",
            path=prefix,
        )
        return {"csrf_token": csrf, "expires_at": expires}

    owner_dependency = Depends(owner)

    @app.get(prefix + "/session")
    async def session(row=owner_dependency):
        return {"csrf_token": row["csrf_token"], "expires_at": row["expires_at"]}

    @app.delete(prefix + "/session")
    async def logout(request: Request, response: Response, row=owner_dependency):
        clock, captured = app.state.core.clock, _request_fields(request)

        def remove(store):
            with store.transaction():
                current = _check_owner(
                    SimpleNamespace(store=store, settings=settings, clock=clock), captured
                )
                store.execute(
                    "DELETE FROM console_sessions WHERE token_sha256 = %s",
                    (current["token_sha256"],),
                )

        await app.state.api_store.run(remove)
        response.delete_cookie(COOKIE, path=prefix, secure=True, httponly=True, samesite="strict")
        return {"state": "SIGNED_OUT"}

    auth = [Depends(owner)]

    from companion.qzone_publication_api import register_routes as register_publication_routes

    def publication_check(request):
        captured, clock = _request_fields(request), app.state.core.clock
        return lambda store: _check_owner(
            SimpleNamespace(store=store, settings=settings, clock=clock), captured
        )

    register_publication_routes(
        app, settings, prefix + "/qzone/publications", auth, publication_check
    )

    async def write(request, key, body, apply, *, rejected_errors=frozenset()):
        clock, captured = app.state.core.clock, _request_fields(request)

        def execute(store):
            context = SimpleNamespace(
                store=store,
                settings=settings,
                clock=clock,
                ledger=ErasureLedger(settings.erasure_ledger),
            )

            def owner_check():
                _check_owner(context, captured)

            return _operation(
                context,
                captured,
                key,
                body,
                lambda: apply(context, owner_check),
                owner_check=owner_check,
                rejected_errors=rejected_errors,
            )

        return await app.state.api_store.run(execute)

    @app.get(prefix + "/overview", dependencies=auth)
    async def overview():
        clock = app.state.core.clock

        def read(store):
            memory = Memory(store, settings, ErasureLedger(settings.erasure_ledger))
            return console_read.overview(SimpleNamespace(store=store, settings=settings,
                                                        memory=memory, clock=clock))

        return await app.state.api_store.run(read)

    @app.get(prefix + "/activity", dependencies=auth)
    async def activity(before: int | None = None, limit: int = 30):
        if not 1 <= limit <= 60 or (before is not None and before < 1):
            raise CoreError("INVALID_PAGE", 400)
        return console_read.activity(app.state.core, before, limit)

    @app.get(prefix + "/memories", dependencies=auth)
    async def memories(q: str = "", review: bool = False, limit: int = 30):
        if not 1 <= limit <= 60 or len(q) > 100:
            raise CoreError("INVALID_MEMORY_QUERY", 400)
        return console_read.memories(app.state.core, q, review, limit)

    @app.get(prefix + "/memories/recall", dependencies=auth)
    async def memory_recall(q: str, limit: int = 10):
        if not q.strip() or len(q) > 100 or not 1 <= limit <= 20:
            raise CoreError("INVALID_MEMORY_QUERY", 400)
        if not settings.memory_enabled:
            raise CoreError("MEMORY_DISABLED", 409)
        return console_read.recall(app.state.core, q.strip(), limit)

    @app.post(prefix + "/memories/{memory_id}/decision", dependencies=auth)
    async def memory_decision(
        memory_id: str,
        body: MemoryChange,
        request: Request,
        idempotency_key: str = Header(default=""),
    ):
        origin = request.headers.get("origin", "")

        def apply(context, owner_check):
            with context.store.transaction():
                owner_check()
                return Memory(context.store, settings, context.ledger).decide_from_console(
                    memory_id,
                    body.decision,
                    context.clock(),
                    body.corrected_content,
                    operation_id=idempotency_key,
                    origin=origin,
                    owner_check=owner_check,
                )

        return await write(
            request, idempotency_key, body.model_dump(), apply, rejected_errors=MEMORY_REJECTIONS
        )

    from companion.console_cognition import register_console_cognition_routes

    async def reset_affect(request, key, body):
        clock = app.state.core.clock
        # Only the materialized receipt/authentication fields enter the worker.
        captured = _request_fields(request)

        def reset(store):
            context = SimpleNamespace(store=store, settings=settings, clock=clock)

            def owner_check():
                _check_owner(context, captured)

            def apply():
                with store.transaction():
                    owner_check()
                    return AffectEngine(store, settings, clock).reset(
                        settings.owner_qq_id,
                        key,
                        ErasureLedger(settings.erasure_ledger),
                    )

            return _operation(context, captured, key, body, apply, owner_check=owner_check)

        return await app.state.api_store.run(reset)

    async def source_grant_action(request, key, body, grant_id):
        clock, captured = app.state.core.clock, _request_fields(request)

        def execute(store):
            context = SimpleNamespace(store=store, settings=settings, clock=clock)

            def owner_check():
                _check_owner(context, captured)

            def apply():
                grants = SourceGrantService(store, settings, clock)
                if body["action"] == "revoke":
                    return grants.revoke(grant_id, owner_check=owner_check)
                return grants.forget(
                    grant_id, ErasureLedger(settings.erasure_ledger), owner_check=owner_check
                )

            return _operation(context, captured, key, body, apply, owner_check=owner_check)

        return await app.state.api_store.run(execute)

    register_console_cognition_routes(
        app,
        prefix,
        auth,
        write,
        reset_affect,
        source_grant_action,
    )

    @app.get(prefix + "/thinking", dependencies=auth)
    async def thinking():
        return console_read.decisions(app.state.core)

    @app.get(prefix + "/life", dependencies=auth)
    async def life():
        return console_read.life(app.state.core)

    @app.get(prefix + "/content", dependencies=auth)
    async def content():
        return console_read.content(app.state.core)

    @app.get(prefix + "/memes", dependencies=auth)
    async def memes():
        return console_read.memes(app.state.core)

    @app.get(prefix + "/diagnostics", dependencies=auth)
    async def diagnostics():
        return console_read.diagnostics(app.state.core)

    @app.get(prefix + "/voice", dependencies=auth)
    async def voice():
        from companion.console_media_views import voice

        return voice(app.state.core)

    @app.get(prefix + "/account-actions", dependencies=auth)
    async def account_actions():
        from companion.console_media_views import account_actions

        return account_actions(app.state.core)

    @app.post(prefix + "/deliveries/{message_id}/reconcile", dependencies=auth)
    async def reconcile(
        message_id: str,
        body: DeliveryReconciliation,
        request: Request,
        idempotency_key: str = Header(default=""),
    ):
        from companion import delivery_reconciliation

        clock, captured = app.state.core.clock, _request_fields(request)
        material = body.model_dump()
        fingerprint = digest(encode([captured.method, captured.url.path, material]))

        def execute(store):
            context = SimpleNamespace(store=store, settings=settings, clock=clock)

            def owner_check():
                _check_owner(context, captured)

            def recovered(key, fingerprint):
                # _operation already holds its checked receipt transaction here.
                receipt = delivery_reconciliation.lookup(
                    store,
                    settings,
                    message_id,
                    operation_id=key,
                    operation_sha256=fingerprint,
                    outcome=body.outcome,
                    repair=True,
                    now=clock(),
                )
                return {"state": body.outcome} if receipt is not None else None

            with store.transaction():
                owner_check()
                if not store.one(
                    "SELECT 1 FROM console_operations WHERE request_id = %s", (idempotency_key,)
                ):
                    readiness = delivery_reconciliation.readiness(
                        store, settings, message_id, now=clock()
                    )
                    if not readiness["prepared"]:
                        raise CoreError(readiness["reason"] or "DELIVERY_QUIESCENCE_REQUIRED", 409)

            def apply():
                with store.transaction():
                    owner_check()
                    return reconcile_delivery(
                        store,
                        message_id,
                        body.outcome,
                        clock(),
                        settings,
                        operation_id=idempotency_key,
                        operation_sha256=fingerprint,
                    )

            return _operation(
                context,
                captured,
                idempotency_key,
                material,
                apply,
                recover=recovered,
                owner_check=owner_check,
            )

        return await app.state.api_store.run(execute)

    @app.get(prefix + "/deliveries/{message_id}/recovery", dependencies=auth)
    async def recovery_preview(message_id: str, request: Request):
        from companion import reply_recovery

        captured, clock = _request_fields(request), app.state.core.clock

        def execute(store):
            context = SimpleNamespace(store=store, settings=settings, clock=clock)
            _check_owner(context, captured)
            return reply_recovery.preview(store, settings, clock, message_id)

        return await app.state.api_store.run(execute)

    @app.post(prefix + "/deliveries/{message_id}/recovery", dependencies=auth)
    async def recover_reply(
        message_id: str,
        body: RecoveryRequest,
        request: Request,
        idempotency_key: str = Header(default=""),
    ):
        from companion import reply_recovery

        captured, clock = _request_fields(request), app.state.core.clock
        material = body.model_dump()
        fingerprint = digest(encode([captured.method, captured.url.path, material]))

        def execute(store):
            context = SimpleNamespace(store=store, settings=settings, clock=clock)

            def owner_check():
                _check_owner(context, captured)

            def apply():
                with store.transaction():
                    owner_check()
                    return reply_recovery.create(
                        store,
                        settings,
                        clock,
                        message_id,
                        body.expected_sha256,
                        idempotency_key,
                        fingerprint,
                    )

            return _operation(
                context,
                captured,
                idempotency_key,
                material,
                apply,
                recover=lambda key, sha: reply_recovery.create(
                    store, settings, clock, message_id, body.expected_sha256, key, sha
                ),
                owner_check=owner_check,
            )

        return await app.state.api_store.run(execute)

    @app.get(prefix + "/diary/{day}", dependencies=auth)
    async def diary(day: date):
        core = app.state.core
        result = core.diary.get_day(day, settings.owner_qq_id)
        if result is None:
            raise CoreError("DIARY_NOT_FOUND", 404)
        return result

    @app.post(prefix + "/diary/{day}/correction", dependencies=auth)
    async def correction(
        day: date,
        body: DiaryCorrection,
        request: Request,
        idempotency_key: str = Header(default=""),
    ):
        def apply(context, owner_check):
            return DiaryService(
                context.store, settings, None, context.ledger, context.clock
            ).correct(day, body.text, settings.owner_qq_id, owner_check=owner_check)

        return await write(
            request,
            idempotency_key,
            body.model_dump(),
            apply,
            rejected_errors={"INVALID_DIARY_CORRECTION"},
        )

    @app.post(prefix + "/diary/{day}/forget", dependencies=auth)
    async def forget(day: date, request: Request, idempotency_key: str = Header(default="")):
        def apply(context, owner_check):
            return DiaryService(
                context.store, settings, None, context.ledger, context.clock
            ).forget(day, settings.owner_qq_id, owner_check=owner_check)

        return await write(request, idempotency_key, {}, apply)

    @app.post(prefix + "/mode", dependencies=auth)
    async def mode(body: Mode, request: Request, idempotency_key: str = Header(default="")):
        def apply(core, owner_check):
            if body.target == "diary":
                return DiaryService(core.store, settings, None, core.ledger, core.clock).set_mode(
                    body.enabled, settings.owner_qq_id, owner_check=owner_check
                )
            if body.target == "life":
                return GoalEngine(core.store, settings, core.ledger, core.clock).pause_autonomy(
                    not body.enabled, owner_check=owner_check
                )
            if body.enabled and not settings.proactive_enabled:
                raise CoreError("PROACTIVE_DISABLED_IN_CONFIG", 409)
            with core.store.transaction():
                owner_check()
                set_proactive_paused(core.store, not body.enabled, core.clock())
            return {"target": body.target, "enabled": body.enabled}

        # Both failures occur before their setters start a native transaction.
        return await write(
            request,
            idempotency_key,
            body.model_dump(),
            apply,
            rejected_errors={"DIARY_DISABLED_IN_CONFIG", "PROACTIVE_DISABLED_IN_CONFIG"},
        )

    @app.get(prefix + "/autonomy", dependencies=auth)
    async def autonomy():
        core = app.state.core
        with core.store.transaction():
            return delegation.view(core.store, settings, core.clock())

    @app.put(prefix + "/autonomy/{capability}", dependencies=auth)
    async def change_autonomy(
        capability: str,
        body: Delegation,
        request: Request,
        idempotency_key: str = Header(default=""),
    ):
        if capability not in delegation.DELEGABLE:
            raise CoreError("DELEGATION_INVALID", 404)
        # Validate before taking an operation receipt so a rejected range never needs repair.
        delegation.validate_ranges(settings, capability, body.ranges)

        def apply(core, owner_check):
            with core.store.transaction():
                owner_check()
                delegation.set_delegation(
                    core.store,
                    settings,
                    capability,
                    body.mode,
                    body.allow_pause,
                    body.ranges,
                    core.clock(),
                )
            return {"state": "COMPLETE"}

        return await write(request, idempotency_key, body.model_dump(), apply)

    @app.post(prefix + "/autonomy/adjustments/{override_id}/decision", dependencies=auth)
    async def decide_adjustment(
        override_id: str,
        body: AdjustmentDecision,
        request: Request,
        idempotency_key: str = Header(default=""),
    ):
        clock, captured = app.state.core.clock, _request_fields(request)

        def execute(store):
            context = SimpleNamespace(store=store, settings=settings, clock=clock)

            def owner_check():
                _check_owner(context, captured)

            with store.transaction():
                owner_check()
                if not store.one(
                    "SELECT 1 FROM console_operations WHERE request_id = %s", (idempotency_key,)
                ):
                    delegation.check_decision(store, settings, override_id, body.decision)

            def apply():
                with store.transaction():
                    owner_check()
                    return delegation.decide(store, settings, override_id, body.decision, clock())

            return _operation(
                context,
                captured,
                idempotency_key,
                body.model_dump(),
                apply,
                owner_check=owner_check,
            )

        return await app.state.api_store.run(execute)

    @app.get(prefix + "/configuration", dependencies=auth)
    async def configuration():
        core = app.state.core
        return {
            **operator_config.view(core.store, settings),
            "cloud_budget": cloud_budget.policy(core.store, settings, core.clock()),
        }

    @app.put(prefix + "/configuration", dependencies=auth)
    async def change_configuration(
        body: operator_config.ConfigurationChange,
        request: Request,
        idempotency_key: str = Header(default=""),
    ):
        if not idempotency_key or len(idempotency_key) > 128 or not idempotency_key.isascii():
            raise CoreError("IDEMPOTENCY_KEY_REQUIRED", 400)
        clock, captured = app.state.core.clock, _request_fields(request)

        def update(store):
            context = SimpleNamespace(store=store, settings=settings, clock=clock)
            changed = operator_config.update(
                store,
                settings,
                body,
                idempotency_key,
                clock(),
                owner_check=lambda: _check_owner(context, captured),
                clock=clock,
            )
            return {**changed, "cloud_budget": cloud_budget.policy(store, settings, clock())}

        return await app.state.api_store.run(update)

    @app.put(prefix + "/cloud-budget", dependencies=auth)
    async def cloud_budget_change(
        body: operator_config.ConfigurationChange,
        request: Request,
        idempotency_key: str = Header(default=""),
    ):
        clock, captured = app.state.core.clock, _request_fields(request)

        def update(store):
            context = SimpleNamespace(store=store, settings=settings, clock=clock)
            return cloud_budget.configure(
                store,
                settings,
                body.model_dump(),
                idempotency_key=idempotency_key,
                now=clock(),
                owner_check=lambda: _check_owner(context, captured),
                clock=clock,
            )

        return await app.state.api_store.run(update)
