"""Request-time selection over native SDK parts, original custody and erasure."""

import asyncio
from contextlib import closing
from copy import deepcopy

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic_ai import Agent
from pydantic_ai.capabilities import Hooks
from pydantic_ai.toolsets import FunctionToolset

from companion import agent_runtime, beta_observation
from companion import context_planner as planner
from companion.agent_messages import from_wire_messages, request_wire, to_wire_messages
from companion.agent_persistence import ConversationStore, recoverable
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.model import ModelClient, ModelResponse
from companion.postgres import Database
from companion.privacy import advance_epoch, epoch
from companion.store import Store, encode
from companion.token_measurement import measure_request
from tests.owner_model_fixtures import authorized_job
from tests.postgres_fixture import isolated_database, logical_state
from tests.sdk_reader_fixture import Provider, answer, call
from tests.support.context_planner import initial, request_context
from tests.support.owner_runtime import chat
from tests.support.paid_image_budget import image_approval, image_messages


def test_merged_sdk_parts_trim_references_then_whole_old_turns(core, tmp_path):
    _, job = chat(core, tmp_path)
    messages, snapshots = initial(core, job)
    original = deepcopy(messages)
    core.settings.context_soft_tokens = 1
    context = request_context(messages)
    assert len(context.messages) < len(messages)  # Public SDK merged consecutive requests.
    selected = planner.plan_request(core, job, snapshots, context)
    assert to_wire_messages(selected) == [messages[0], messages[-1]]
    plan = planner.check(core, job, snapshots)
    assert [item["kind"] for item in plan["pending"]["dropped"]] == ["reference", "history"]
    assert messages == original
    planner.check_request(
        core, job, snapshots, request_wire(selected, context.model_request_parameters), ()
    )
    assert not core.store.rows("SELECT * FROM model_usage")


def test_actual_resolved_instructions_and_tool_description_change_selection(core, tmp_path):
    _, job = chat(core, tmp_path)
    messages, snapshots = initial(core, job, turns=0)
    core.settings.context_soft_tokens = (
        measure_request(core.settings, messages).reserved_input_tokens + 10
    )
    observed = []

    def lookup() -> str:
        return "observed result"

    lookup.__doc__ = "actual resolved tool description " * 400

    async def before(ctx, request_context):
        parameters = request_context.model_request_parameters
        assert (
            parameters.instruction_parts
            and parameters.function_tools[0].description == lookup.__doc__.strip()
        )
        selected = planner.plan_request(core, job, snapshots, request_context)
        request_context.messages = selected
        ctx.messages[:] = selected
        return request_context

    async def complete(wire, *, tools):
        planner.check_request(core, job, snapshots, wire, tools)
        observed.append(wire)
        return ModelResponse("synthetic result", 100, 4)

    async def scenario():
        result = await Agent(
            agent_runtime.ControlledModel("synthetic-planner", complete),
            instructions="resolved trusted instruction " * 80,
            toolsets=[FunctionToolset([lookup])],
            capabilities=[Hooks(before_model_request=before)],
        ).run(message_history=from_wire_messages(messages), infer_name=False)
        assert result.output == "synthetic result"

    asyncio.run(scenario())
    assert len(observed) == 1 and len(observed[0]) == 3
    assert not any("optional-reference" in message["content"] for message in observed[0])
    assert planner.check(core, job, snapshots)["pending"]["dropped"]
    assert not core.store.rows("SELECT * FROM model_usage")  # This was an SDK mapping probe.


@pytest.mark.parametrize("resume", [False, True])
def test_long_tool_result_replans_saved_history_without_replaying_settled_response(
    core,
    tmp_path,
    resume,
):
    _, job = chat(core, tmp_path)
    core.settings.context_soft_tokens = 2500
    messages, snapshots = initial(core, job)
    persistence = ConversationStore(core, job, snapshots)
    executions = []

    def lookup() -> str:
        """Read a synthetic long result."""
        executions.append(True)
        return "required-tool-result " * 300

    provider = Provider(answer(calls=[call("lookup-once", "lookup", {})]), answer("complete"))

    class SimulatedExit(BaseException):
        pass

    original = persistence.after_model_request

    async def stop_after_save(ctx, *, request_context, response):
        await original(ctx, request_context=request_context, response=response)
        raise SimulatedExit

    if resume:
        persistence.after_model_request = stop_after_save

    async def scenario():
        nonlocal persistence, snapshots
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            core.model = ModelClient(core.settings, client)
            if resume:
                with pytest.raises(SimulatedExit):
                    await agent_runtime.run_conversation(
                        core,
                        job,
                        messages,
                        toolsets=[FunctionToolset([lookup])],
                        snapshots=snapshots,
                        persistence=persistence,
                    )
                assert persistence.row()["phase"] == "READY" and recoverable(
                    core.store, job["job_id"]
                )
                persistence = ConversationStore(core, job, [])
                conversation = persistence.load()
                snapshots = persistence.snapshots
                result = await agent_runtime.run_conversation(
                    core,
                    job,
                    [],
                    conversation=conversation,
                    toolsets=[FunctionToolset([lookup])],
                    snapshots=snapshots,
                    persistence=persistence,
                )
            else:
                result = await agent_runtime.run_conversation(
                    core,
                    job,
                    messages,
                    toolsets=[FunctionToolset([lookup])],
                    snapshots=snapshots,
                    persistence=persistence,
                )
            assert result.output == "complete"

    asyncio.run(scenario())
    assert len(provider.requests) == 2 and executions == [True]
    assert any("optional-reference" in str(m) for m in provider.requests[0]["messages"])
    assert not any(
        "optional-reference" in str(m) or "old question" in str(m)
        for m in provider.requests[1]["messages"]
    )
    assert any(
        m["role"] == "tool" and m["tool_call_id"] == "lookup-once"
        for m in provider.requests[1]["messages"]
    )
    plan = planner.check(core, job, snapshots)
    assert len(plan["requests"]) == 2 and plan["pending"] is None
    assert [r["kind"] for r in plan["requests"][1]["dropped"]] == ["reference", "history"]
    saved = persistence.load()
    responses = [m for m in saved.messages if (m.metadata or {}).get("core_request_id")]
    assert len(responses) == saved.usage.requests == 2
    assert saved.usage.input_tokens == 200 and saved.usage.output_tokens == 40
    assert len(core.store.rows("SELECT * FROM model_usage WHERE state='SETTLED'")) == 2
    assert not core.store.rows("SELECT * FROM outbox")


def test_required_overflow_refuses_before_reserve_and_retry_keeps_checkpoint(core, tmp_path):
    _, job = chat(core, tmp_path)
    core.settings.context_soft_tokens = 1
    messages, snapshots = initial(core, job)
    # This changes only runtime output settings, not the approved paid contract.
    core.settings.max_output_tokens = core.settings.paid_model.max_output_tokens + 1
    persistence = ConversationStore(core, job, snapshots)
    provider = Provider(answer("retry complete"))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            core.model = ModelClient(core.settings, client)
            with pytest.raises(CoreError, match="MODEL_OUTPUT_LIMIT"):
                await agent_runtime.run_conversation(
                    core, job, messages, snapshots=snapshots, persistence=persistence
                )
            assert persistence.row()["phase"] == "READY"
            assert planner.check(core, job, snapshots)["pending"]["state"] == "NOT_ADMITTED"
            assert not provider.requests and not core.store.rows("SELECT * FROM model_usage")
            core.settings.max_output_tokens = 300
            restored = ConversationStore(core, job, [])
            conversation = restored.load()
            assert to_wire_messages(conversation.messages) == [messages[0], messages[-1]]
            result = await agent_runtime.run_conversation(
                core,
                job,
                [],
                conversation=conversation,
                snapshots=restored.snapshots,
                persistence=restored,
            )
            assert result.output == "retry complete"
            records = planner.check(core, job, restored.snapshots)["requests"]
            assert len(records) == 1
            assert [item["kind"] for item in records[0]["dropped"]] == ["reference", "history"]

    asyncio.run(scenario())
    assert len(provider.requests) == 1


@pytest.mark.parametrize("damage", [
    "job", "epoch", "group", "priority", "assembly", "proof", "wire", "duplicate"])
def test_recognized_damaged_plan_never_reaches_paid_io(core, tmp_path, damage):
    _, job = chat(core, tmp_path)
    messages, snapshots = initial(core, job)
    plan = planner.check(core, job, snapshots)
    if damage == "job":
        plan["job_id"] = "other"
    elif damage == "epoch":
        plan["privacy_epoch"] = epoch(core.store) + 1
    elif damage == "group":
        plan["groups"][1]["wire_count"] = True
    elif damage == "priority":
        plan["groups"][1]["priority"] = True
    elif damage == "assembly":
        plan["assembly_dropped"] = [{"index": 0, "kind": "history",
                                     "priority": True, "reason": "SOFT_LIMIT"}]
    elif damage == "proof":
        plan["initial_source_proofs_sha256"] = "f" * 64
    elif damage == "wire":
        messages[-1]["content"] += " changed"
    else:
        snapshots.append(deepcopy(snapshots[-1]))
    provider = Provider()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            core.model = ModelClient(core.settings, client)
            with pytest.raises(CoreError, match="CONTEXT_PLAN_(INVALID|CHANGED)"):
                await agent_runtime.run_conversation(core, job, messages, snapshots=snapshots)

    asyncio.run(scenario())
    assert not provider.requests and not core.store.rows("SELECT * FROM model_usage")


def test_unknown_absent_and_oversized_layouts_preserve_all_input(core, tmp_path):
    _, job = chat(core, tmp_path)
    messages, snapshots = initial(core, job)
    core.settings.context_soft_tokens = 1
    snapshots[-1]["metadata"]["core_context"] = {"version": 999, "groups": "untrusted"}
    context = request_context(messages)
    assert planner.plan_request(core, job, snapshots, context) is context.messages
    inexact = []
    planner.initialize(core, job, messages, [("rules", [deepcopy(messages[0])], True, 4)], inexact)
    assert inexact == []
    assert planner.plan_request(core, job, [], context) is context.messages
    messages, snapshots = initial(core, job, references=100, turns=0)
    assert snapshots == []
    context = request_context(messages)
    assert planner.plan_request(core, job, snapshots, context) is context.messages


def test_unrelated_epoch_preserves_capture_binding_without_rewriting_proofs(core, tmp_path):
    _, job = chat(core, tmp_path)
    _, snapshots = initial(core, job, snapshots=[{"kind": "history", "proof": []}])
    original = deepcopy(snapshots)
    captured = epoch(core.store)
    with core.store.transaction(control_write=True):
        advance_epoch(core.store)
    plan = planner.check(core, job, snapshots)
    assert plan["privacy_epoch"] == captured and snapshots == original
    # Source/admission bindings remain strict even when the capture is older.
    snapshots[0]["proof"].append({"event_id": "foreign-source"})
    with pytest.raises(CoreError, match="CONTEXT_PLAN_INVALID"):
        planner.check(core, job, snapshots)
    assert not core.store.rows("SELECT * FROM model_usage")


def test_attached_media_pins_entire_history_group_and_retains_honest_estimate(core, tmp_path):
    image_approval(core, tmp_path)
    job_id = authorized_job(core, purposes=("chat", "image"))
    job = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (job_id,))
    rules = [{"role": "system", "content": "required rules"}]
    history = [*image_messages(), {"role": "assistant", "content": "image answer"}]
    reference = [{"role": "user", "content": "optional text" * 300}]
    current = [{"role": "user", "content": "current input"}]
    messages = [*rules, *reference, *history, *current]
    snapshots = []
    planner.initialize(
        core,
        job,
        messages,
        [
            ("rules", rules, True, 4),
            ("reference", reference, False, 1),
            ("history", history, False, 2),
            ("current", current, True, 4),
        ],
        snapshots,
    )
    core.settings.context_soft_tokens = 1
    context = request_context(messages)
    selected = planner.plan_request(core, job, snapshots, context)
    assert to_wire_messages(selected) == [*rules, *history, *current]
    plan = planner.check(core, job, snapshots)
    assert plan["groups"][1]["kind"] == "history" and plan["groups"][1]["required"]
    assert plan["pending"]["dropped"] == [
        {"group_id": "g1", "kind": "reference", "reason": "SOFT_LIMIT", "priority": 1}
    ]
    measured = measure_request(
        core.settings, request_wire(selected, context.model_request_parameters)
    )
    assert measured.count_kind == "estimate"
    assert measured.output_allowance == core.settings.output_tokens("chat")
    assert not core.store.rows("SELECT * FROM model_usage")


def test_final_wire_drift_is_rejected_and_appended_sources_keep_initial_binding(core, tmp_path):
    _, job = chat(core, tmp_path)
    messages, snapshots = initial(core, job)
    context = request_context(messages)
    selected = planner.plan_request(core, job, snapshots, context)
    snapshots.append({"kind": "history", "proof": {"later": "source-proof"}})
    planner.check(core, job, snapshots)  # Append-only source proof prefix still matches.
    wire = request_wire(selected, context.model_request_parameters)
    planner.check_request(core, job, snapshots, wire, ())
    with pytest.raises(CoreError, match="CONTEXT_PLAN_CHANGED"):
        planner.check_request(
            core, job, snapshots, [*wire, {"role": "user", "content": "unplanned suffix"}], ()
        )
    assert not core.store.rows("SELECT * FROM model_usage")


@pytest.mark.parametrize(
    "limit,reason", [("input", "INPUT_LIMIT"), ("window", "CONTEXT_WINDOW_LIMIT")]
)
def test_planning_targets_each_hard_limit_without_reducing_output(core, tmp_path, limit, reason):
    _, job = chat(core, tmp_path)
    messages, snapshots = initial(core, job)
    required = measure_request(core.settings, [messages[0], messages[-1]])
    core.settings.context_soft_tokens = 100000
    bound = required.reserved_input_tokens + 1
    change = (
        {"max_input_tokens": bound}
        if limit == "input"
        else {"max_context_tokens": bound + required.output_allowance}
    )
    core.settings.paid_model = core.settings.paid_model.model_copy(update=change)
    context = request_context(messages)
    selected = planner.plan_request(core, job, snapshots, context)
    assert to_wire_messages(selected) == [messages[0], messages[-1]]
    record = planner.check(core, job, snapshots)["pending"]
    assert {item["reason"] for item in record["dropped"]} == {reason}
    assert core.settings.output_tokens("chat") == required.output_allowance
    assert not core.store.rows("SELECT * FROM model_usage")


@pytest.mark.parametrize("count", [100, 1000])
def test_offline_candidate_pressure_does_not_create_model_calls(core, tmp_path, count):
    _, job = chat(core, tmp_path)
    messages, original = initial(core, job)
    core.settings.context_soft_tokens = 1
    for _ in range(count):
        snapshots = deepcopy(original)
        context = request_context(messages)
        selected = planner.plan_request(core, job, snapshots, context)
        assert len(to_wire_messages(selected)) == 2
        assert not planner.check(core, job, snapshots)["requests"]
    assert not core.store.rows("SELECT * FROM model_usage")


def test_native_plan_is_content_free_bound_inspection_and_erased_from_old_backup(core, tmp_path):
    _, job = chat(core, tmp_path)
    snapshots = []
    messages = core.context(job, snapshots=snapshots, planning=True)
    persistence = ConversationStore(core, job, snapshots)
    provider = Provider(answer("private saved answer"))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            core.model = ModelClient(core.settings, client)
            await agent_runtime.run_conversation(
                core, job, messages, snapshots=snapshots, persistence=persistence
            )

    asyncio.run(scenario())
    plan = planner.check(core, job, snapshots)
    assert len(plan["requests"]) == 1 and all(g["required"] for g in plan["groups"])
    assert not any(
        text in encode(plan) for text in ("synthetic owner input", "private saved answer")
    )
    before = logical_state(core.store)
    view = beta_observation._native_context(core.store, job["job_id"], epoch(core.store))
    assert view["state"] == "RECORDED" and view["source_validity"] == "NOT_REVALIDATED"
    assert view["requests"][0]["measurement_sha256"] == plan["requests"][0]["measurement_sha256"]
    assert logical_state(core.store) == before
    core.store.execute("UPDATE model_admissions SET measurement_json='{}'")
    assert (
        beta_observation._native_context(core.store, job["job_id"], epoch(core.store))["state"]
        == "INCONSISTENT"
    )
    core.store.execute(
        'UPDATE model_admissions SET measurement_json=%s',
        (encode(measure_request(core.settings, provider.requests[0]["messages"]).receipt()),),
    )
    key = Fernet.generate_key()
    archive = tmp_path / "before-erasure.enc"
    usage = core.store.rows("SELECT * FROM model_usage")
    backup(core.settings.database, archive, key)
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [job["event_id"]], core.clock())
    assert (
        persistence.row()["snapshots_json"] == "[]"
        and persistence.row()["conversation_json"] is None
    )
    assert core.store.rows("SELECT * FROM model_usage") == usage
    with isolated_database() as target:
        state = tmp_path / "restored-state"
        restore(archive, target, key, core.settings.erasure_ledger, state_directory=state)
        with Database(target) as database, closing(Store(database, state)) as restored:
            row = restored.one("SELECT snapshots_json,conversation_json FROM agent_conversations")
            assert row == {
                "snapshots_json": "[]", "conversation_json": None,
            }
            assert restored.rows("SELECT * FROM model_usage") == usage
    assert (
        beta_observation._native_context(core.store, job["job_id"], epoch(core.store))["state"]
        == "NOT_RECORDED"
    )
