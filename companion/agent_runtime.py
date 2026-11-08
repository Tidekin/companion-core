"""Pydantic AI runs the conversation; Core owns each actual paid request.

The runtime accepts native Toolsets and Conversation objects. It does not implement
a second tool registry, loop, checkpoint protocol, delivery path or database store.
Native hooks checkpoint SDK results against the original leased Core job;
unknown provider or tool effects stop recovery instead of repeating I/O.
"""

from collections.abc import Awaitable, Callable
from time import perf_counter_ns

from pydantic_ai import Agent, Conversation, DeferredToolRequests
from pydantic_ai.capabilities import Hooks
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from companion import budget, context_planner, owner_authority, owner_input
from companion.agent_messages import (
    from_wire_messages,
    request_wire,
    to_runtime_response,
)
from companion.contracts import CoreError
from companion.model import ModelResponse, ModelResponseError
from companion.telemetry import emit, phase


class ControlledModel(Model):
    """Map the mature Model interface to an injected completion, without authority."""

    def __init__(self, model_name: str, complete: Callable[..., Awaitable[ModelResponse]], *,
                 image_input: bool = False):
        super().__init__(profile={
            "supports_tools": True, "supports_text_output": True,
            "supports_inline_system_prompts": True,
            "supports_image_input": image_input,
            "supports_document_input": False, "supports_audio_input": False,
            "supports_audio_file_input": False, "supports_video_input": False,
            "supports_json_schema_output": False, "supports_json_object_output": False,
            "supports_thinking": False,
        })
        self._model_name = model_name
        self._complete = complete

    @property
    def model_name(self):
        return self._model_name

    @property
    def system(self):
        return "companion-controlled"

    async def request(self, messages, model_settings, model_request_parameters):
        settings, parameters = self.prepare_request(model_settings, model_request_parameters)
        if (settings or parameters.native_tools
            or parameters.output_mode not in {"text", "tool"}):
            raise CoreError("CORE_MODEL_CONFIGURATION_REQUIRED", 503)
        # Pydantic AI prepares histories before invoking Model.request, including
        # retry feedback and ordered tool results. Request instructions are also
        # provided by its resolved parameters, not rebuilt from our own state.
        wire = request_wire(messages, parameters)
        response = await self._complete(
            wire, tools=[*parameters.function_tools, *parameters.output_tools])
        return to_runtime_response(response, model_name=self.model_name, provider_name=self.system)


def check_conversation(core, job, snapshots=(), *, current_request=None, purpose="chat"):
    """Recheck the original job lease and Core's existing source/snapshot contracts."""
    if not core.store.in_transaction:
        # One synchronous validation reads one native snapshot. This scope ends
        # before any await; the next boundary takes a fresh custody check.
        return core.store.read_or_write(lambda: check_conversation(
            core, job, snapshots, current_request=current_request, purpose=purpose,
        ))
    if job["kind"] != "chat":
        raise CoreError("CONVERSATION_CHAT_JOB_REQUIRED", 403)
    view = owner_authority.check_job(
        core.store, core.settings, core.clock, job, purpose="chat", executing=True,
    )
    if purpose not in view.policy.purposes:
        raise CoreError("OWNER_AUTHORITY_PURPOSE_REQUIRED", 403)
    owner_input._checked_source(core.store, core.settings, view, job["event_id"], core.clock())
    core._check_context_snapshots(job, snapshots, current_request=current_request,
                                  owner_view=view)
    from companion import agent_images, agent_reading

    agent_reading.check_results(core, job, snapshots)
    agent_images.input_sources(core.store, core.settings, core.clock, job)
    return view


async def run_conversation(core, job, messages, *, toolsets=(), conversation=None,
                           deferred_tool_results=None, snapshots=(), persistence=None):
    """Run or resume a mature conversation over the same leased job and paid ledger.

    The caller provides trusted tool handlers and any externally obtained results.
    Conversation persistence is caller-owned: this entry point stores no new state.
    Native handlers must retain their existing source and permission checks.
    """
    if job["kind"] != "chat":
        raise CoreError("CONVERSATION_CHAT_JOB_REQUIRED", 403)
    if conversation is not None and not isinstance(conversation, Conversation):
        raise TypeError("expected a Pydantic AI Conversation")
    if conversation is not None and conversation.conversation_id != job["job_id"]:
        raise CoreError("CONVERSATION_JOB_MISMATCH", 403)
    def check(*, current_request=None):
        check_conversation(core, job, snapshots, current_request=current_request)

    with phase("agent_guard", job["trace_id"]):
        check()

    async def complete(wire, *, tools):
        with phase("request_guard", job["trace_id"]):
            check()
        try:
            with phase("request_validate", job["trace_id"]):
                context_planner.check_request(core, job, snapshots, wire, tools)
            with phase("request_reserve", job["trace_id"]):
                request_id = budget.reserve(core.store, core.settings, job["job_id"], "chat", wire,
                                            core.clock(), clock=core.clock, tools=tools)
        except Exception:
            if persistence is not None:
                persistence.not_admitted()
            raise
        registered = False

        def check_request():
            nonlocal registered
            with phase("request_sources", job["trace_id"]):
                if not registered and persistence is not None:
                    persistence.admitted(request_id, wire)
                else:
                    check(current_request=request_id)
                    from companion.agent_images import check_payload

                    check_payload(core.store, core.settings, core.clock, job, wire)
                    if not registered:
                        context_planner.admitted(core, job, snapshots, request_id)
                registered = True

        model_started = perf_counter_ns()
        emit("t_model_request", job["trace_id"], at_ns=model_started)
        try:
            response = await budget.complete(
                core.model, core.store, core.settings, request_id, wire, "chat", core.clock,
                tools=tools, source_check=check_request, trace_id=job["trace_id"],
            )
        except ModelResponseError as error:
            if persistence is not None:
                persistence.failed_response(error.response)
            raise
        model_finished = perf_counter_ns()
        usage = core.store.one('SELECT state, billed_micro_usd FROM model_usage WHERE request_id = '
            '%s',
                               (request_id,))
        emit("t_model_done", job["trace_id"],
             elapsed_ms=(model_finished - model_started) / 1e6, at_ns=model_finished,
             budget_micro_usd=usage["billed_micro_usd"] if usage else None,
             model_usage_state=usage["state"] if usage else None)
        if usage is None or usage["state"] not in {"SETTLED", "UNKNOWN"}:
            # A received response still requires an original accounting row.
            # UNKNOWN retains its original fee uncertainty and may continue.
            raise CoreError("MODEL_OUTCOME_UNKNOWN", 503)
        check()
        return response

    paid = core.settings.paid_model
    from companion.agent_output import restored_output

    historical_output = restored_output(conversation)
    async def before_tool_execute(ctx, *, call, tool_def, args):
        check()
        return args

    async def before_model_request(ctx, request_context):
        if historical_output is not None:
            # Only consume the original settled final response; do not generate with old tools.
            raise CoreError("CONVERSATION_OUTCOME_UNAVAILABLE", 409)
        with phase("request_guard", job["trace_id"]):
            check()
        with phase("request_plan", job["trace_id"]):
            messages = context_planner.plan_request(core, job, snapshots, request_context)
            request_context.messages = messages
            ctx.messages[:] = messages
        if persistence is not None:
            with phase("request_checkpoint", job["trace_id"]):
                return await persistence.before_model_request(ctx, request_context)
        return request_context

    hooks = {"before_tool_execute": before_tool_execute,
             "before_model_request": before_model_request}
    if persistence is not None:
        hooks.update(after_model_request=persistence.after_model_request,
                     tool_execute=persistence.wrap_tool_execute)
    with phase("agent_setup", job["trace_id"]):
        from companion.agent_output import reply_output

        output_types = [str, DeferredToolRequests]
        if output := historical_output or reply_output(core, job):
            output_types.append(output)
        agent = Agent(
            ControlledModel(core.settings.model_name, complete,
                            image_input=bool(paid and paid.image_input)),
            output_type=output_types, toolsets=toolsets,
            retries=1, name="companion", deps_type=object,
            capabilities=[Hooks(**hooks)],
        )
        history = from_wire_messages(messages) if conversation is None else None
    result = await agent.run(
        message_history=history, conversation=conversation,
        conversation_id=job["job_id"] if conversation is None else None,
        deferred_tool_results=deferred_tool_results,
        usage_limits=UsageLimits(request_limit=paid.per_request_calls if paid else 1),
        infer_name=False,
    )
    if persistence is not None:
        persistence.save_result(result)
    return result
