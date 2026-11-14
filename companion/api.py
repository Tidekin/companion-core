import base64
import binascii
import hmac
import json
import re
from contextlib import asynccontextmanager, nullcontext
from datetime import date
from ipaddress import ip_address
from uuid import uuid4

from anyio import to_thread
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import AsyncClient
from pydantic import Field

from companion import (
    __version__,
    account_actions,
    cumulative,
    inbox,
    input_protocol,
    live_text,
    meme_core,
    meme_service,
    owner_authority,
    owner_delivery,
    owner_input,
    owner_preflight,
    poke,
    qq_images,
    qq_rich,
)
from companion.affect import AffectEngine
from companion.api_store import ApiStore
from companion.api_telemetry import ApiTelemetry
from companion.config import Settings
from companion.console_api import register_console_routes
from companion.content_api import register_content_routes
from companion.contracts import (
    CoreError,
    DeliveryResult,
    InboundEvent,
    LeaseRequest,
    LeaseToken,
    MemoryDecision,
    SearchRequest,
    StrictModel,
)
from companion.core import Core
from companion.diary import DiaryCorrection, DiaryMode
from companion.evaluation import MAX_HTTP_BODY_BYTES
from companion.exploration import METERED_READS_SQL
from companion.life_protocol import LifeConsumeRequest, TopicClosure
from companion.media_api import register_media_routes
from companion.memory_control import (
    MemoryControlGrant,
    MemoryControlInput,
    StoppedMemoryControl,
    stopped_workers,
)
from companion.model import ModelClient
from companion.owner_authority_protocol import (
    AuthorityReference,
    ModelCarryReference,
    OwnerAuthorityCheck,
    OwnerCapture,
    OwnerRecall,
    OwnerSendCheck,
)
from companion.poke_protocol import OwnerPokeCapture, PokeCapture
from companion.privacy import ErasureLedger
from companion.reading import service_for_store as reading_service
from companion.runtime import Runtime
from companion.transport import begin_send, delivery_result, lease_deliveries
from companion.voice_jobs import VoiceASRProof
from companion.voice_node_api import register_voice_node_routes
from companion.voice_protocol import MAX_RESULT_BODY_BYTES


class AffectResetRequest(StrictModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


class OwnerInputApproval(OwnerCapture):
    meme_offer: meme_core.MemeOffer | None = None


class _MemoryControlExclusive:
    def __init__(self, app, *, enabled):
        self.app, self.enabled = app, enabled

    async def __call__(self, scope, receive, send):
        if self.enabled and scope["type"] == "http" and scope["path"] not in {
            "/internal/v1/health/live", "/internal/v1/memory-control",
        }:
            return await JSONResponse({"error": "MEMORY_CONTROL_EXCLUSIVE"}, 403)(
                scope, receive, send)
        await self.app(scope, receive, send)


# Base64 of the largest accepted avatar original plus the small JSON envelope. The
# route still parses this with its strict model and Core re-checks the decoded bytes.
def _avatar_upload_body_bytes():
    from companion.companion_avatar import MAX_UPLOAD_BYTES

    return (MAX_UPLOAD_BYTES + 2) // 3 * 4 + 4096


AVATAR_UPLOAD_BODY_BYTES = _avatar_upload_body_bytes()


def _native_submit_body_bytes():
    from companion.native_images import SUBMIT_BODY_BYTES

    return SUBMIT_BODY_BYTES


# A native message may carry its attached images; Core re-checks each decoded original.
NATIVE_SUBMIT_BODY_BYTES = _native_submit_body_bytes()


class BodyLimit:
    def __init__(self, app, image_limit=131072, meme_limit=131072, file_limit=131072,
                 voice_limit=131072, voice_input_limit=131072, video_limit=131072,
                 singing_voice_tokens=(), evaluation_limit=131072):
        self.app = app
        self.image_limit = image_limit
        self.meme_limit = meme_limit
        self.file_limit = file_limit
        self.voice_limit = voice_limit
        self.voice_input_limit = voice_input_limit
        self.video_limit = video_limit
        self.singing_voice_tokens = singing_voice_tokens
        self.evaluation_limit = evaluation_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        body = bytearray()
        limit = (
            self.image_limit
            if re.fullmatch(r"/internal/v2/content/[^/]+/image", scope["path"])
            else self.file_limit
            if re.fullmatch(r"/internal/v2/content/[^/]+/file", scope["path"])
            else self.meme_limit
            if scope["path"] == "/internal/v1/memes/captions"
            else self.voice_limit
            if re.fullmatch(r"/internal/v2/voice-node/jobs/voice_[a-f0-9]{32}/result",
                            scope["path"])
            else self.voice_input_limit
            if re.fullmatch(r"/internal/v1/voice-inputs/[^/]+", scope["path"])
            else self.video_limit
            if re.fullmatch(r"/internal/v3/readers/[^/]+/[^/]+/media", scope["path"])
            else self.evaluation_limit
            if scope["path"] in {"/internal/v2/evaluations", "/internal/v2/evaluations/preview"}
            else AVATAR_UPLOAD_BODY_BYTES
            if scope["path"] == "/v1/console/chat/profile/avatars"
            else NATIVE_SUBMIT_BODY_BYTES
            if scope["path"] == "/v1/console/chat/messages" and scope.get("method") == "POST"
            else 131072
        )
        if re.fullmatch(r"/internal/v2/voice-node/jobs/voice_[a-f0-9]{32}/result", scope["path"]):
            authorization = dict(scope.get("headers", [])).get(b"authorization", b"")
            if any(hmac.compare_digest(authorization, token)
                   for token in self.singing_voice_tokens):
                from companion_delivery_protocol.singing import MAX_RESULT_BODY_BYTES as song_limit

                limit = song_limit
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                return await JSONResponse({"error": "BODY_TOO_LARGE"}, 413)(scope, receive, send)
            if not message.get("more_body"):
                break
        if scope["path"] == "/internal/v1/inbox/events":
            try:
                qq_rich.check_http_json(bytes(body))
            except (ValueError, RecursionError):
                return await JSONResponse({"error": "INVALID_CONTRACT"}, 400)(scope, receive, send)
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings=None, *, memory_control_grant=None, memory_control_sha256=None):
    settings = settings or Settings.load()
    if memory_control_grant is not None:
        memory_control_grant = MemoryControlGrant.model_validate(memory_control_grant)

    @asynccontextmanager
    async def lifespan(app):
        if memory_control_grant is not None:
            with stopped_workers(settings.state_directory), Runtime(settings) as runtime:
                runtime.store.configuration_worker = "api"
                app.state.core = runtime
                app.state.api_store = ApiStore(runtime.database, settings.state_directory)
                yield
            return
        image_model = (
            settings.mode == "live"
            and settings.content_enabled and settings.content.use_chat_model_for_vision
            and qq_images.enabled(settings)
        )
        async with (
            AsyncClient(timeout=settings.timeout_seconds, trust_env=False)
            if image_model else nullcontext()
        ) as client:
            app.state.core = Core(settings, ModelClient(settings, client) if client else None)
            app.state.api_store = ApiStore(app.state.core.database, settings.state_directory)
            app.state.core.store.configuration_worker = "api"
            try:
                yield
            finally:
                app.state.core.close()

    app = FastAPI(
        title="Companion Core",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.observation_instance = uuid4().hex
    app.add_middleware(
        BodyLimit,
        image_limit=settings.content.max_image_bytes if settings.content_enabled else 131072,
        file_limit=(settings.private_files.max_file_bytes
                    if settings.private_files.enabled else 131072),
        meme_limit=6 * 1024 * 1024 if settings.meme_semantics_enabled else 131072,
        voice_limit=MAX_RESULT_BODY_BYTES if settings.voice.enabled else 131072,
        singing_voice_tokens=tuple(("Bearer " + node.token.get_secret_value()).encode()
            for node in settings.voice.nodes.values()
            if settings.voice.enabled and node.enabled and "sing" in node.capabilities),
        voice_input_limit=8_000_000 if settings.voice.enabled else 131072,
        video_limit=(4 * ((settings.bili_video.media.max_total_output_bytes + 2) // 3)
                     + 131072 if settings.bili_video.enabled else 131072),
        evaluation_limit=MAX_HTTP_BODY_BYTES if settings.evaluation_enabled else 131072,
    )

    async def authenticate(authorization: str = Header(default="")):
        expected = "Bearer " + settings.bridge_token.get_secret_value()
        if not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(401, "UNAUTHORIZED")

    async def diary_admin(authorization: str = Header(default="")):
        token = settings.diary_admin_token.get_secret_value()
        if not token or not hmac.compare_digest(
            authorization.encode(), ("Bearer " + token).encode()
        ):
            raise HTTPException(401, "UNAUTHORIZED")

    async def control_admin(authorization: str = Header(default="")):
        token = settings.control_token.get_secret_value()
        if not token or not hmac.compare_digest(
            authorization.encode(), ("Bearer " + token).encode()
        ):
            raise HTTPException(401, "UNAUTHORIZED")

    async def observation_adapter(adapter_id: str, authorization: str = Header(default="")):
        adapter = settings.observation_adapters.get(adapter_id)
        if not adapter or not hmac.compare_digest(
            authorization.encode(), ("Bearer " + adapter.token.get_secret_value()).encode()
        ):
            raise HTTPException(401, "UNAUTHORIZED")

    @app.exception_handler(CoreError)
    async def core_error(request, exc):
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI's normal validation body echoes rejected input, including secrets.
        return JSONResponse(
            {
                "error": "INVALID_CONTRACT",
                "fields": [".".join(map(str, e["loc"])) for e in exc.errors()],
            },
            400,
        )

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        return JSONResponse({"error": "INTERNAL_ERROR"}, 500)

    prefix = "/internal/v1"
    auth = [Depends(authenticate)]
    register_content_routes(app, auth)
    register_console_routes(app, settings)
    register_voice_node_routes(app)

    app.add_middleware(_MemoryControlExclusive, enabled=memory_control_grant is not None)

    @app.post(prefix + "/memory-control", dependencies=[Depends(control_admin)])
    async def memory_control(body: MemoryControlInput, request: Request):
        if memory_control_grant is None:
            raise CoreError("MEMORY_CONTROL_SCOPE_REQUIRED", 403)
        if memory_control_grant.execution == "local_synthetic" and (
            not request.client or not ip_address(request.client.host).is_loopback
        ):
            raise CoreError("MEMORY_CONTROL_OFFLINE_ONLY", 403)
        core = app.state.core
        service = StoppedMemoryControl(core.store, settings, core.ledger, core.clock,
                                       memory_control_grant, memory_control_sha256)
        return service.apply(body)

    @app.get(prefix + "/health/live")
    async def live():
        return {"status": "alive", "worker_health_proven": False}

    @app.get(prefix + "/health/ready", dependencies=auth)
    async def ready():
        from companion.ops_status import schema_readiness

        def check(store):
            with store.read_snapshot():
                store.execute("SET LOCAL statement_timeout='2s'")
                store.execute("SET LOCAL lock_timeout='1s'")
                details, blocker = schema_readiness(store)
                if blocker:
                    raise CoreError(blocker, 503)
                return details["schema"]

        version = await app.state.api_store.run(check)
        return {
            "status": "ready",
            "schema": version,
            "capabilities": [
                "m0",
                "m1",
                "memory_index",
                "diary",
                "observations",
                "affect",
                "consolidation",
                "reflection",
                "goals",
                "exploration",
                "expression",
                "executive",
                "media",
                "content",
            ],
        }

    diary_auth = [Depends(diary_admin)]

    from companion.executive_protocol import ToolDecision, ToolRequest
    from companion.exploration import ReadResult
    from companion.goals import (
        CommitmentRequest,
        GoalContinuationDecision,
        GoalDecision,
        GoalProposal,
    )
    from companion.observation import ObservationEvent
    from companion.policy import ConsentRequest
    from companion.privacy import epoch
    from companion.reading_protocol import ReadingLeaseRequest, ReadObservation
    from companion.reflection import ReflectionDecision
    from companion.social_autonomy import SocialAutonomy
    from companion.social_autonomy_protocol import ReviewedAvatar
    from companion.source_grants import (
        ReaderLeaseRequest,
        ReaderReadValidation,
        SourceGrantRegistration,
        SourceGrantService,
    )

    control_auth = [Depends(control_admin)]
    from companion.cognition_api import register_cognition_routes
    from companion.memory_projection_api import register_memory_projection_routes
    from companion.owner_authority_api import register_owner_authority_routes

    register_owner_authority_routes(app, control_auth)
    register_memory_projection_routes(app, control_auth)
    register_cognition_routes(app, control_auth)
    from companion.qzone import register_routes as register_qzone_routes

    register_qzone_routes(app, control_auth, reading_service, settings)
    adapter_auth = [Depends(observation_adapter)]
    register_media_routes(app, control_auth)

    @app.post("/internal/v2/account-assets", dependencies=control_auth)
    async def register_account_asset(body: ReviewedAvatar):
        core = app.state.core
        return SocialAutonomy(core.store, settings, clock=core.clock).register_asset(body)

    @app.get("/internal/v2/tools", dependencies=control_auth)
    async def list_tools():
        return {
            "items": app.state.core.store.rows(
                'SELECT tool_tasks.task_id AS task_id, tool_tasks.idempotency_key AS '
                'idempotency_key, tool_tasks.request_sha256 AS request_sha256, '
                'tool_tasks.scope_sha256 AS scope_sha256, tool_tasks.executor_id AS executor_id, '
                'tool_tasks.capability AS capability, tool_tasks.scope_json AS scope_json, '
                'tool_tasks.risk_level AS risk_level, tool_tasks.state AS state, '
                'tool_tasks.captured_privacy_epoch AS captured_privacy_epoch, '
                'tool_tasks.lease_token AS lease_token, tool_tasks.lease_until AS lease_until, '
                'tool_tasks.reserved_micro_usd AS reserved_micro_usd, tool_tasks.billed_micro_usd '
                'AS billed_micro_usd, tool_tasks.dispatched_at AS dispatched_at, '
                'tool_tasks.result_event_id AS result_event_id, tool_tasks.last_error_code AS '
                'last_error_code, tool_tasks.created_at AS created_at, tool_tasks.updated_at AS '
                'updated_at, tool_tasks.redacted AS redacted FROM tool_tasks AS tool_tasks WHERE '
                'tool_tasks.redacted = 0 ORDER BY record_sequence DESC NULLS LAST LIMIT 30'
            )
        }

    @app.post("/internal/v2/tools", dependencies=control_auth, status_code=202)
    async def submit_tool(body: ToolRequest):
        return app.state.core.executive.submit(body)

    @app.post("/internal/v2/tools/{task_id}/decision", dependencies=control_auth)
    async def decide_tool(task_id: str, body: ToolDecision):
        return app.state.core.executive.decide(task_id, body)

    @app.post("/internal/v2/tools/{task_id}/revoke", dependencies=control_auth)
    async def revoke_tool(task_id: str):
        return app.state.core.executive.revoke(task_id)

    @app.post("/internal/v2/readers/{adapter_id}/lease", dependencies=adapter_auth)
    async def reader_lease(adapter_id: str, body: ReaderLeaseRequest | None = None):
        grant = body.source_grant if body else None
        clock = app.state.core.clock
        return {"item": await app.state.api_store.run(lambda store: reading_service(
            store, settings, clock,
        ).exploration.lease(adapter_id, grant))}

    @app.post("/internal/v2/readers/{adapter_id}/{request_id}/validate", dependencies=adapter_auth)
    async def reader_validate(adapter_id: str, request_id: str, body: ReaderReadValidation):
        clock = app.state.core.clock
        return await app.state.api_store.run(lambda store: reading_service(
            store, settings, clock,
        ).exploration.validate_read(adapter_id, request_id, body))

    @app.post("/internal/v2/source-grants", dependencies=control_auth, status_code=201)
    async def source_grant_register(body: SourceGrantRegistration):
        clock = app.state.core.clock
        return await app.state.api_store.run(lambda store: SourceGrantService(
            store, settings, clock,
        ).register(body))

    @app.get("/internal/v2/source-grants", dependencies=control_auth)
    async def source_grant_list():
        return {"items": app.state.core.observations.grants.list(), "limit": 100}

    @app.get("/internal/v2/source-grants/legacy", dependencies=control_auth)
    async def source_grant_legacy(offset: int = 0):
        return {
            "items": app.state.core.observations.grants.legacy(offset),
            "page_size": 100, "automatic_attribution": False,
        }

    @app.post("/internal/v2/source-grants/{grant_id}/revoke", dependencies=control_auth)
    async def source_grant_revoke(grant_id: str):
        clock = app.state.core.clock
        return await app.state.api_store.run(lambda store: SourceGrantService(
            store, settings, clock,
        ).revoke(grant_id))

    @app.post("/internal/v2/source-grants/{grant_id}/forget", dependencies=control_auth)
    async def source_grant_forget(grant_id: str):
        clock = app.state.core.clock
        return await app.state.api_store.run(lambda store: SourceGrantService(
            store, settings, clock,
        ).forget(grant_id, ErasureLedger(settings.erasure_ledger)))

    @app.post("/internal/v2/readers/{adapter_id}/{request_id}/result", dependencies=adapter_auth)
    async def reader_result(adapter_id: str, request_id: str, body: ReadResult):
        clock = app.state.core.clock
        return await app.state.api_store.run(lambda store: reading_service(
            store, settings, clock,
        ).exploration.complete(adapter_id, request_id, body))

    @app.post("/internal/v3/readers/{adapter_id}/lease", dependencies=adapter_auth)
    async def reading_lease(adapter_id: str, body: ReadingLeaseRequest | None = None):
        grant = body.source_grant if body else None
        capabilities = body.capabilities if body else ()
        clock = app.state.core.clock
        return {"item": await app.state.api_store.run(lambda store: reading_service(
            store, settings, clock,
        ).lease(adapter_id, grant, capabilities=capabilities))}

    @app.post("/internal/v3/readers/{adapter_id}/{request_id}/validate", dependencies=adapter_auth)
    async def reading_validate(adapter_id: str, request_id: str, body: ReaderReadValidation):
        clock = app.state.core.clock
        return await app.state.api_store.run(lambda store: reading_service(
            store, settings, clock,
        ).validate_read(adapter_id, request_id, body))

    @app.post("/internal/v3/readers/{adapter_id}/{request_id}/result", dependencies=adapter_auth)
    async def reading_result(adapter_id: str, request_id: str, body: ReadObservation):
        clock = app.state.core.clock
        return await app.state.api_store.run(lambda store: reading_service(
            store, settings, clock,
        ).complete(adapter_id, request_id, body))

    @app.post("/internal/v3/readers/{adapter_id}/{request_id}/media/prepare",
              dependencies=adapter_auth)
    async def reading_media_prepare(adapter_id: str, request_id: str, body: dict):
        return app.state.core.bili_video.prepare(adapter_id, request_id, body)

    @app.post("/internal/v3/readers/{adapter_id}/{request_id}/media",
              dependencies=adapter_auth)
    async def reading_media_upload(adapter_id: str, request_id: str, body: dict):
        core = app.state.core
        core.bili_video.model = core.model
        return await core.bili_video.upload(adapter_id, request_id, body)

    @app.post("/internal/v3/readers/{adapter_id}/{request_id}/media/receipt",
              dependencies=adapter_auth)
    async def reading_media_receipt(adapter_id: str, request_id: str, body: dict):
        return app.state.core.bili_video.receipt(adapter_id, request_id, body)

    @app.post("/internal/v3/readers/{adapter_id}/{request_id}/media/result",
              dependencies=adapter_auth)
    async def reading_media_result(adapter_id: str, request_id: str, body: dict):
        return app.state.core.bili_video.result(adapter_id, request_id, body)

    @app.get("/internal/v2/exploration", dependencies=control_auth)
    async def exploration_status():
        return {
            "items": app.state.core.store.rows(
                "SELECT request_id,adapter_id,operation,state,used_bytes,reserved_bytes,"
                "used_requests,reserved_requests,observed_bytes,observed_requests,"
                "accounted_bytes,accounted_requests,last_error_code,created_at "
                f"FROM ({METERED_READS_SQL}) ORDER BY queue_order DESC LIMIT 30"
            )
        }

    @app.get("/internal/v2/affect", dependencies=control_auth)
    async def affect_snapshot():
        return app.state.core.affect.snapshot()

    @app.post("/internal/v2/affect/reset", dependencies=control_auth)
    async def affect_reset(body: AffectResetRequest, response: Response):
        clock = app.state.core.clock
        response.headers["Cache-Control"] = "no-store"
        return await app.state.api_store.run(lambda store: AffectEngine(
            store, settings, clock,
        ).reset(settings.owner_qq_id, body.request_id, ErasureLedger(settings.erasure_ledger)))

    @app.get("/internal/v2/expression-learning", dependencies=control_auth)
    async def expression_learning(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return app.state.core.expression_learning.snapshot(settings.owner_qq_id)

    @app.get("/internal/v2/affect/{event_id}/preview", dependencies=control_auth)
    async def affect_preview(event_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"proposal": app.state.core.affect.preview(event_id)}

    @app.get("/internal/v2/goals", dependencies=control_auth)
    async def goals(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"items": app.state.core.goals.recent()}

    @app.post("/internal/v2/goals", dependencies=control_auth, status_code=202)
    async def propose_goal(body: GoalProposal):
        return {"goal_id": app.state.core.goals.propose(body)}

    @app.post("/internal/v2/goals/{goal_id}/decision", dependencies=control_auth)
    async def decide_goal(goal_id: str, body: GoalDecision):
        engine = app.state.core.goals
        return engine.accept(goal_id) if body.decision == "ACCEPT" else engine.cancel(goal_id)

    @app.post("/internal/v2/goals/{goal_id}/continuation", dependencies=control_auth)
    async def continue_goal(goal_id: str, body: GoalContinuationDecision):
        return app.state.core.goals.continue_plan(goal_id, body)

    @app.post("/internal/v2/commitments", dependencies=control_auth, status_code=202)
    async def accept_commitment(body: CommitmentRequest):
        return app.state.core.goals.accept_commitment(body)

    @app.post("/internal/v2/life/runtime-mode", dependencies=control_auth)
    async def life_mode(body: DiaryMode):
        return app.state.core.goals.pause_autonomy(not body.enabled)

    @app.get("/internal/v2/interests", dependencies=control_auth)
    async def interests(response: Response):
        from companion.interest import profile

        response.headers["Cache-Control"] = "no-store"
        core = app.state.core
        result = profile(core.store, core.settings, core.clock(), core.settings.owner_qq_id)
        return {"items": result["self.interest"], **result}

    @app.get("/internal/v2/timeline", dependencies=control_auth)
    async def timeline(response: Response, topic: str, days: int = 7, limit: int = 40):
        from companion.timeline import recent_timeline

        response.headers["Cache-Control"] = "no-store"
        core = app.state.core
        return recent_timeline(
            core.store,
            core.settings,
            core.clock(),
            core.settings.owner_qq_id,
            topic=topic,
            days=days,
            limit=limit,
        )

    @app.get("/internal/v2/social/{event_id}/preview", dependencies=control_auth)
    async def social_preview(event_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        core = app.state.core
        return {"draft": core.social.preview(event_id, core.settings.owner_qq_id)}

    @app.get("/internal/v2/life/topics", dependencies=control_auth)
    async def life_topics(response: Response):
        response.headers["Cache-Control"] = "no-store"
        core = app.state.core
        return core.life_topics.preview(core.settings.owner_qq_id)

    @app.post("/internal/v2/life/results/{job_id}/consume", dependencies=control_auth)
    async def consume_life_draft(job_id: str, body: LifeConsumeRequest, response: Response):
        response.headers["Cache-Control"] = "no-store"
        core = app.state.core
        return core.life_topics.consume(job_id, body, diary=core.diary)

    @app.post("/internal/v2/life/topics/{topic_id}/close", dependencies=control_auth)
    async def close_life_topic(topic_id: str, body: TopicClosure, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return app.state.core.life_topics.close(topic_id, body.source_event_id)

    @app.get("/internal/v2/consolidation", dependencies=control_auth)
    async def consolidation_status():
        return {"items": app.state.core.consolidation.recent()}

    @app.post("/internal/v2/consolidation/{day}", dependencies=control_auth, status_code=202)
    async def consolidate(day: date):
        run_id = app.state.core.consolidation.schedule_day(day)
        if not run_id:
            raise CoreError("CONSOLIDATION_DISABLED", 403)
        return {"run_id": run_id}

    @app.get("/internal/v2/reflections", dependencies=control_auth)
    async def reflections(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"items": app.state.core.reflections.recent()}

    @app.post("/internal/v2/reflections/{suggestion_id}/decision", dependencies=control_auth)
    async def reflection_decision(suggestion_id: str, body: ReflectionDecision):
        return app.state.core.reflections.decide(suggestion_id, body.decision)

    @app.get("/internal/v2/observations/{adapter_id}/context", dependencies=adapter_auth)
    async def observation_context(adapter_id: str):
        app.state.core.observations.policy(adapter_id)
        return {"privacy_epoch": epoch(app.state.core.store)}

    @app.post("/internal/v2/observations/{adapter_id}", dependencies=adapter_auth, status_code=202)
    async def observe(adapter_id: str, body: ObservationEvent):
        if settings.observation_adapters[adapter_id].source == "qq_image":
            raise CoreError("CONTENT_IMAGE_INTAKE_REQUIRED", 403)
        if settings.observation_adapters[adapter_id].source == "qq_file":
            raise CoreError("CONTENT_FILE_INTAKE_REQUIRED", 403)
        return app.state.core.observations.append(adapter_id, body)

    @app.get("/internal/v2/observations", dependencies=control_auth)
    async def observations():
        return {"items": app.state.core.observations.recent()}

    @app.post("/internal/v2/observations/{event_id}/forget", dependencies=control_auth)
    async def forget_observation(event_id: str):
        core = app.state.core
        if not core.store.one('SELECT 1 FROM observations WHERE event_id = %s', (event_id,)):
            raise CoreError("OBSERVATION_NOT_FOUND", 404)
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, [event_id], core.clock())
        return {"event_id": event_id, "status": "REDACTED"}

    @app.post("/internal/v2/consents", dependencies=control_auth)
    async def grant_consent(body: ConsentRequest):
        return app.state.core.observations.consents.grant(body)

    from companion import evaluation
    from companion.evaluation import Manifest, Submission

    @app.post("/internal/v2/evaluations/preview", dependencies=control_auth)
    async def evaluation_preview(body: Manifest):
        core = app.state.core
        return evaluation.preview(core.store, settings, core.clock, body)

    @app.post("/internal/v2/evaluations", dependencies=control_auth, status_code=202)
    async def evaluation_submit(body: Submission):
        return evaluation.submit(app.state.core, body)

    @app.get("/internal/v2/evaluations/{evaluation_id}", dependencies=control_auth)
    async def evaluation_results(evaluation_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        core = app.state.core
        return evaluation.results(core.store, settings, core.clock, evaluation_id)

    @app.post("/internal/v2/consents/{consent_id}/revoke", dependencies=control_auth)
    async def revoke_consent(consent_id: str):
        return app.state.core.observations.consents.revoke(consent_id)

    @app.get(prefix + "/diary", dependencies=diary_auth)
    async def diary_list(limit: int = 7):
        return {"items": app.state.core.diary.list_recent(settings.owner_qq_id, limit)}

    @app.get(prefix + "/diary/status", dependencies=diary_auth)
    async def diary_status():
        return app.state.core.diary.status(settings.owner_qq_id)

    @app.post(prefix + "/diary/runtime-mode", dependencies=diary_auth)
    async def diary_mode(body: DiaryMode):
        return app.state.core.diary.set_mode(body.enabled, settings.owner_qq_id)

    @app.get(prefix + "/diary/{day}", dependencies=diary_auth)
    async def diary_day(day: date):
        result = app.state.core.diary.get_day(day, settings.owner_qq_id)
        if not result:
            raise CoreError("DIARY_NOT_FOUND", 404)
        return result

    @app.post(prefix + "/diary/{day}/recompute", dependencies=diary_auth, status_code=202)
    async def diary_recompute(day: date):
        core = app.state.core
        run_id = core.diary.recompute_day(day, settings.owner_qq_id)
        state = core.store.one('SELECT state FROM diary_runs WHERE run_id = %s', (run_id,))[
            "state"
        ] if run_id else "NOT_SCHEDULED"
        return {"local_date": day.isoformat(), "run_id": run_id, "state": state}

    @app.post(prefix + "/diary/{day}/forget", dependencies=diary_auth)
    async def diary_forget(day: date):
        return app.state.core.diary.forget(day, settings.owner_qq_id)

    @app.post(prefix + "/diary/{day}/correction", dependencies=diary_auth)
    async def diary_correct(day: date, body: DiaryCorrection):
        return app.state.core.diary.correct(day, body.text, settings.owner_qq_id)

    @app.post(prefix + "/inbox/events", dependencies=auth, status_code=202)
    async def ingest(event: InboundEvent, x_idempotency_key: str = Header()):
        core = app.state.core

        def receive(store):
            store.configuration_worker = "api"
            return inbox.receive(store, core.settings, core.clock, core.ledger, event,
                                 x_idempotency_key, content_options=core.content.options)

        return await app.state.api_store.run(receive)

    @app.post(prefix + "/owner-authority/inputs/approve", dependencies=auth)
    async def owner_input_approve(body: OwnerInputApproval, response: Response):
        clock = app.state.core.clock
        response.headers["Cache-Control"] = "no-store"
        capture = OwnerCapture.model_validate(body.model_dump(exclude={"meme_offer"}))
        return await app.state.api_store.run(lambda store: owner_input.approve(
            store, settings, clock, capture, meme_offer=body.meme_offer,
        ))

    @app.post(prefix + "/owner-authority/check", dependencies=auth)
    async def owner_authority_check(body: OwnerAuthorityCheck, response: Response):
        clock = app.state.core.clock
        response.headers["Cache-Control"] = "no-store"

        def check(store):
            # The Bridge polls this every second; a read snapshot leaves the writer free.
            return store.read_or_write(lambda: owner_authority.check_response(
                owner_authority.current(store, settings, clock,
                                        authority=body.authority, purpose=body.purpose),
                body.purpose))

        return await app.state.api_store.run(check)

    @app.post(prefix + "/owner-authority/inputs/state", dependencies=auth)
    async def owner_input_state(body: OwnerCapture | OwnerPokeCapture, response: Response):
        from companion import owner_input_state

        response.headers["Cache-Control"] = "no-store"
        return await app.state.api_store.run(lambda store: owner_input_state.read(
            store, settings, body))

    @app.post(prefix + "/owner-authority/stop-receipt", dependencies=auth)
    async def owner_stop_receipt(body: AuthorityReference, response: Response):
        from companion import owner_stop_receipt

        response.headers["Cache-Control"] = "no-store"
        return owner_stop_receipt.read(app.state.core.store, settings, body.authority,
                                       chain=True)

    @app.post(prefix + "/owner-authority/model-carry-receipt", dependencies=auth)
    async def owner_model_carry_receipt(body: ModelCarryReference, response: Response):
        from companion import model_carry

        response.headers["Cache-Control"] = "no-store"
        return model_carry.read(app.state.core.store, settings, body.authority,
                                body.carry_sha256, body.successor_policy_sha256)

    @app.post(prefix + "/owner-authority/send-check", dependencies=auth)
    async def owner_send_check(body: OwnerSendCheck, response: Response):
        clock = app.state.core.clock
        response.headers["Cache-Control"] = "no-store"
        return await app.state.api_store.run(lambda store: owner_delivery.send_check(
            store, settings, clock, body,
        ))

    @app.post(prefix + "/owner-authority/inputs/recall", dependencies=auth)
    async def owner_input_recall(body: OwnerRecall, response: Response):
        from companion import owner_recall

        core = app.state.core
        response.headers["Cache-Control"] = "no-store"
        return await app.state.api_store.run(lambda store: owner_recall.receive(
            store, settings, core.ledger, core.clock, body))

    @app.post(prefix + "/live-text/open", dependencies=auth)
    async def live_text_open(body: live_text.OpenWindow):
        core = app.state.core
        return live_text.open_window(core.store, settings, core.clock, body)

    @app.post(prefix + "/live-text/handoff", dependencies=auth)
    async def live_text_handoff(body: live_text.WindowReference):
        core = app.state.core
        return live_text.prepare_handoff(core.store, settings, core.clock, body)

    @app.post(prefix + "/live-text/handoff/abort", dependencies=auth)
    async def live_text_handoff_abort(body: live_text.AbortHandoffReference):
        core = app.state.core
        return live_text.abort_handoff(core.store, settings, core.clock, body)

    @app.post(prefix + "/live-text/inputs/approve", dependencies=auth)
    async def live_text_input(body: live_text.InputApproval):
        core = app.state.core
        return live_text.approve_input(core.store, settings, core.clock, body)

    @app.post(prefix + "/live-text/inputs/cancelled", dependencies=auth)
    async def live_text_cancelled_input(body: live_text.CancelledInputReference):
        core = app.state.core
        return live_text.cancelled_input(core.store, settings, core.clock, body)

    @app.post(prefix + "/live-text/inputs/recall", dependencies=auth)
    async def live_text_recall(body: input_protocol.RecallReceipt):
        core = app.state.core
        return await app.state.api_store.run(lambda store: input_protocol.receive_recall(
            store, settings, core.ledger, core.clock, body))

    @app.get(prefix + "/live-text/inputs/{event_id}/state", dependencies=auth)
    async def live_text_input_state(event_id: str):
        return input_protocol.input_state(app.state.core.store, settings, event_id)

    @app.post(prefix + "/live-text/cancel-before-send", dependencies=auth)
    async def live_text_not_sent(body: input_protocol.CancelBeforeSend):
        core = app.state.core
        return input_protocol.cancel_before_send(core.store, settings, core.clock, body)

    @app.post(prefix + "/owner-authority/cancel-before-send", dependencies=auth)
    async def owner_not_sent(body: input_protocol.OwnerCancelBeforeSend, response: Response):
        core = app.state.core
        response.headers["Cache-Control"] = "no-store"
        return input_protocol.cancel_before_send(core.store, settings, core.clock, body)

    @app.post(prefix + "/owner-authority/expired-preflight", dependencies=auth)
    async def expired_preflight(body: owner_preflight.ExpiredPreflight, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await app.state.api_store.run(lambda store: owner_preflight.settle(
            store, settings, app.state.core.clock, body))

    @app.post(prefix + "/live-text/send-check", dependencies=auth)
    async def live_text_send(body: live_text.SendCheck):
        core = app.state.core
        return live_text.send_check(core.store, settings, core.clock, body)

    @app.post(prefix + "/live-text/pokes", dependencies=auth)
    async def receive_poke(body: PokeCapture):
        core = app.state.core
        return poke.receive(core.store, settings, core.clock, body)

    @app.post(prefix + "/pokes", dependencies=auth)
    async def receive_owner_poke(body: OwnerPokeCapture, response: Response):
        core = app.state.core
        response.headers["Cache-Control"] = "no-store"
        return poke.receive(core.store, settings, core.clock, body)

    @app.post(prefix + "/voice-inputs/{event_id}", dependencies=auth)
    async def voice_input(event_id: str, request: Request,
                          x_voice_proof: str = Header(default=""),
                          content_type: str = Header(default="")):
        from companion.voice_protocol import canonical_bytes

        if not settings.voice.enabled or content_type != "audio/wav":
            raise CoreError("VOICE_ASR_SCOPE_REQUIRED", 403)
        try:
            if not 1 <= len(x_voice_proof) <= 8192:
                raise ValueError()
            raw = base64.b64decode(x_voice_proof, altchars=b"-_", validate=True)
            proof = VoiceASRProof.model_validate_json(raw)
            if (canonical_bytes(proof) != raw
                or base64.urlsafe_b64encode(raw).decode() != x_voice_proof):
                raise ValueError()
        except (ValueError, TypeError, binascii.Error):
            raise CoreError("VOICE_ASR_PROOF_REQUIRED", 403) from None
        wav = await request.body()
        core = app.state.core
        with core.store.transaction():
            return core.voice.submit_signed_asr(event_id, wav, upload_proof=proof)

    @app.get(prefix + "/voice-inputs/{event_id}/state", dependencies=auth)
    async def voice_input_state(event_id: str):
        return app.state.core.voice.input_receipt(event_id)

    @app.post(prefix + "/memes/semantics/profile", dependencies=auth)
    async def meme_profile():
        return app.state.core.memes.profile()

    from companion import meme_delivery

    @app.post(prefix + "/memes/deliveries/{message_id}/asset", dependencies=auth)
    async def meme_delivery_asset(message_id: str, body: meme_delivery.AssetRequest,
                                  response: Response):
        core = app.state.core
        response.headers["Cache-Control"] = "no-store"
        return meme_delivery.read_asset(core.store, settings, core.clock, message_id, body)

    from companion import image_generation

    @app.post(prefix + "/generated-images/deliveries/{message_id}/asset", dependencies=auth)
    async def generated_image_asset(message_id: str, body: image_generation.AssetRequest,
                                    response: Response):
        core = app.state.core
        response.headers["Cache-Control"] = "no-store"
        return image_generation.read_asset(core.store, settings, core.clock, message_id, body)

    @app.post(prefix + "/memes/captions", dependencies=auth)
    async def meme_caption(body: meme_core.CaptionBody):
        return app.state.core.memes.submit(body)

    @app.post(prefix + "/memes/captions/{job_id}", dependencies=auth)
    async def meme_caption_status(job_id: str):
        return app.state.core.memes.outcome(job_id)

    from companion import meme_responsibility

    @app.post(prefix + "/memes/captions/{job_id}/responsibility", dependencies=auth)
    async def meme_caption_responsibility(job_id: str, body: meme_responsibility.EmptyBody,
                                          response: Response):
        response.headers["Cache-Control"] = "no-store"
        return meme_responsibility.read(app.state.core.store, settings, job_id)

    @app.post(prefix + "/memes/embeddings", dependencies=auth)
    async def meme_embedding(body: meme_core.EmbedBody):
        return await app.state.core.memes.embed(body)

    @app.post(prefix + "/memes/query", dependencies=auth)
    async def meme_query(body: meme_core.QueryBody):
        return await app.state.core.memes.query(body)

    @app.post(prefix + "/memes/offers", dependencies=auth)
    async def meme_offer(body: meme_core.OfferBody):
        return app.state.core.memes.offer(body)

    @app.post(prefix + "/memes/discovery/submit", dependencies=auth)
    async def meme_submit(body: meme_service.DiscoveryRequest):
        core = app.state.core
        return meme_service.submit(core.store, settings, core.clock, body)

    @app.post(prefix + "/memes/discovery/lease", dependencies=auth)
    async def meme_lease(body: meme_service.DiscoveryLeaseRequest):
        clock = app.state.core.clock
        return await app.state.api_store.run(lambda store: meme_service.lease(
            store, settings, clock, body))

    @app.post(prefix + "/memes/discovery/{job_id}/check", dependencies=auth)
    async def meme_check(job_id: str, body: meme_service.DiscoveryCheck):
        core = app.state.core
        return meme_service.check(core.store, settings, core.clock, job_id, body)

    @app.post(prefix + "/memes/discovery/{job_id}/reserve", dependencies=auth)
    async def meme_reserve(job_id: str, body: meme_service.DiscoveryReserve):
        core = app.state.core
        return meme_service.reserve(core.store, settings, core.clock, job_id, body)

    @app.post(prefix + "/memes/discovery/{job_id}/result", dependencies=auth)
    async def meme_result(job_id: str, body: meme_service.DiscoveryResult):
        core = app.state.core
        return meme_service.result(core.store, settings, core.clock, job_id, body)

    @app.post(prefix + "/live-text/stop", dependencies=auth)
    async def live_text_stop(body: live_text.WindowReference):
        return live_text.stop_window(app.state.core.store, body, app.state.core.clock)

    @app.get(prefix + "/cumulative/status", dependencies=auth)
    async def cumulative_status():
        core = app.state.core
        return cumulative.status(core.store, settings, core.clock())

    @app.post(prefix + "/cumulative/pause", dependencies=auth)
    async def cumulative_pause(body: cumulative.PauseRequest):
        core = app.state.core
        return cumulative.owner_pause(core.store, settings, core.clock, body)

    @app.post(prefix + "/deliveries/lease", dependencies=auth)
    async def lease(body: LeaseRequest):
        clock = app.state.core.clock
        return await app.state.api_store.run(lambda store: lease_deliveries(
            store, settings, body, clock(),
        ))

    @app.post(prefix + "/deliveries/reconciliation/prepare", dependencies=auth)
    async def prepare_delivery_reconciliation(body: dict):
        from companion import delivery_reconciliation

        core = app.state.core
        return delivery_reconciliation.prepare(core.store, settings, body, core.clock())

    @app.post(prefix + "/deliveries/{message_id}/reconciliation/lookup", dependencies=auth)
    async def lookup_delivery_reconciliation(message_id: str, body: dict):
        from companion import delivery_reconciliation

        if (set(body) != {"proof_sha256"} or not isinstance(body["proof_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", body["proof_sha256"])):
            raise CoreError("DELIVERY_QUIESCENCE_REQUIRED", 400)
        core = app.state.core
        receipt = delivery_reconciliation.lookup(core.store, settings, message_id,
                                                 proof_sha256=body["proof_sha256"])
        return ({"state": "FOUND", "receipt": receipt} if receipt is not None
                else {"state": "NOT_FOUND"})

    @app.post(prefix + "/account-actions/send-check", dependencies=auth)
    async def account_send_check(body: account_actions.SendCheck):
        core = app.state.core
        return account_actions.send_check(core.store, settings, core.clock, body)

    @app.post(prefix + "/account-actions/profile-baselines", dependencies=auth)
    async def account_profiles(body: account_actions.ProfileProbe):
        core = app.state.core
        return account_actions.record_profiles(core.store, settings, core.clock, body)

    @app.get(prefix + "/account-actions/profile-baselines/state", dependencies=auth)
    async def account_profile_probe_state(authority: str, authorization_sha256: str):
        if len(authority) > 1024:
            raise CoreError("INVALID_ACCOUNT_PROFILE_SCOPE", 400)
        try:
            reference = account_actions.AuthorityBody(authority=json.loads(authority)).authority
        except (ValueError, TypeError):
            raise CoreError("INVALID_ACCOUNT_PROFILE_SCOPE", 400) from None
        if not re.fullmatch(r"[a-f0-9]{64}", authorization_sha256):
            raise CoreError("INVALID_ACCOUNT_PROFILE_SCOPE", 400)
        core = app.state.core
        return account_actions.profile_probe_state(
            core.store, settings, core.clock, reference, authorization_sha256)

    @app.post(prefix + "/account-actions/cancel-before-send", dependencies=auth)
    async def account_cancel_before_send(body: account_actions.CancelBeforeSend):
        core = app.state.core
        return account_actions.cancel_before_send(core.store, settings, core.clock, body)

    @app.post(prefix + "/deliveries/{message_id}/begin-send", dependencies=auth)
    async def begin(message_id: str, body: LeaseToken):
        clock = app.state.core.clock
        return await app.state.api_store.run(lambda store: begin_send(
            store, message_id, body.lease_token, clock(), settings, clock=clock,
        ))

    @app.post(prefix + "/deliveries/{message_id}/result", dependencies=auth)
    async def result(message_id: str, body: DeliveryResult):
        clock = app.state.core.clock
        return await app.state.api_store.run(lambda store: delivery_result(
            store, settings, message_id, body, clock(),
        ))

    @app.post(prefix + "/memories/search", dependencies=auth)
    async def search(body: SearchRequest):
        def check_scope():
            core = app.state.core
            if owner_authority.required(core.store, settings):
                owner_authority.current(core.store, settings, core.clock, purpose="memory")
            elif live_text.active(core.store, settings):
                raise CoreError("LIVE_TEXT_MEMORY_API_DISABLED", 403)

        check_scope()
        return {
            "items": await app.state.core.memory.search_async(
                body.query, body.requester_actor_id, body.subject_filter, body.limit,
                now=app.state.core.clock(), source_check=check_scope, clock=app.state.core.clock,
            )
        }

    @app.post(prefix + "/memories/{memory_id}/decisions", dependencies=auth)
    async def decide(memory_id: str, body: MemoryDecision):
        core = app.state.core
        if owner_authority.required(core.store, settings):
            owner_authority.current(core.store, settings, core.clock, purpose="memory")
        elif live_text.active(core.store, settings):
            raise CoreError("LIVE_TEXT_MEMORY_API_DISABLED", 403)
        return core.memory.decide(memory_id, body.decision, core.clock(), body.corrected_content)

    @app.get(prefix + "/ops/status", dependencies=auth)
    async def status(response: Response, summary_only: bool = False):
        from companion.ops_status import runtime_snapshot, snapshot

        response.headers["Cache-Control"] = "no-store"
        clock = app.state.core.clock
        result = {"operations": await to_thread.run_sync(snapshot, settings)} if summary_only else (
            await app.state.api_store.run(lambda store: runtime_snapshot(store, settings, clock))
        )
        return result | {"observation": {
            "api_instance": app.state.observation_instance,
            "worker_health_proven": False,
        }}

    @app.post(prefix + "/tasks", dependencies=auth)
    @app.post(prefix + "/approvals/{approval_id}/decision", dependencies=auth)
    async def disabled(approval_id: str = ""):
        raise CoreError("CAPABILITY_DISABLED_IN_M0_M1", 403)

    app.add_middleware(ApiTelemetry, routes=app.routes)
    return app
