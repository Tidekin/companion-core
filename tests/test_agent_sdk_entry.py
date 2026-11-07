"""Native SDK custody through real grants, HTTP ingress, worker and send-check."""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from companion import agent_runtime, budget, model, paid_budget, token_measurement
from companion.agent_tools import core_query_tools
from companion.contracts import CoreError
from tests.test_owner_runtime import chat


@pytest.fixture
def sdk_modules():
    return {"model": model, "paid_budget": paid_budget,
            "token_measurement": token_measurement, "budget": budget}


def test_tools_and_results_are_measured_in_exact_reserved_payload(core, tmp_path, sdk_modules):
    _, job = chat(core, tmp_path)
    tools = [{"type": "function", "function": {"name": "clock", "parameters": {
        "type": "object", "properties": {}}}}]
    messages = [{"role": "user", "content": "time"}]
    measured = sdk_modules["token_measurement"].measure_request(
        core.settings, messages, tools=tools)
    plain = sdk_modules["token_measurement"].measure_request(core.settings, messages)
    assert measured.count_kind == "estimate"
    assert measured.input_tokens > plain.input_tokens
    assert measured.payload_sha256 != plain.payload_sha256
    request = sdk_modules["budget"].reserve(core.store, core.settings, job["job_id"], "chat",
        messages, core.clock(), clock=core.clock, tools=tools)
    row = core.store.one('SELECT * FROM model_admissions WHERE request_id=%s', (request,))
    assert json.loads(row["measurement_json"])["payload_sha256"] == measured.payload_sha256
    with pytest.raises(CoreError):
        sdk_modules["budget"]._check_measurement(core.settings, row, messages, "chat")


def test_native_sdk_tool_roundtrip_uses_current_authority_and_paid_ledger(
    core, tmp_path, sdk_modules,
):
    _, job = chat(core, tmp_path)
    requests = []

    async def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        message = {"role": "assistant", "content": "checked"}
        if len(requests) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "clock-call", "type": "function", "function": {
                    "name": "current_time", "arguments": "{}"}}]}
        return httpx.Response(200, json={"choices": [{"message": message}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            core.model = sdk_modules["model"].ModelClient(core.settings, client)
            result = await agent_runtime.run_conversation(core, job, core.context(job),
                toolsets=[core_query_tools(core, job, [])])
            assert result.output == "checked"

    asyncio.run(scenario())
    assert len(requests) == 2
    assert any(m["role"] == "tool" and m["tool_call_id"] == "clock-call"
               for m in requests[1]["messages"])
    assert len(core.store.rows("SELECT * FROM model_usage WHERE state='SETTLED'")) == 2
    assert not core.store.rows("SELECT * FROM outbox")



def test_saved_response_resumes_without_repeating_paid_request(core, tmp_path, sdk_modules):
    from companion.agent_persistence import ConversationStore, recoverable

    _, job = chat(core, tmp_path)
    snapshots = []
    persistence = ConversationStore(core, job, snapshots)
    calls = []

    class SimulatedExit(BaseException):
        pass

    original_save = persistence.after_model_request

    async def stop_after_save(ctx, *, request_context, response):
        result = await original_save(ctx, request_context=request_context, response=response)
        raise SimulatedExit(result)

    persistence.after_model_request = stop_after_save

    async def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "saved result"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            core.model = sdk_modules["model"].ModelClient(core.settings, client)
            with pytest.raises(SimulatedExit):
                await agent_runtime.run_conversation(core, job, core.context(job),
                    snapshots=snapshots, persistence=persistence)
            assert recoverable(core.store, job["job_id"])
            restored = ConversationStore(core, job, [])
            conversation = restored.load()
            result = await agent_runtime.run_conversation(core, job, [],
                conversation=conversation, persistence=restored)
            assert result.output == "saved result"
            assert restored.row()["phase"] == "OUTPUT"
            with core.store.transaction():
                restored.complete()
            assert restored.row()["phase"] == "COMPLETE"

    asyncio.run(scenario())
    assert len(calls) == 1
    assert len(core.store.rows("SELECT * FROM model_usage WHERE state='SETTLED'")) == 1


@pytest.mark.parametrize("phase", ["MODEL_PENDING", "READY"])
def test_uncertain_execution_is_not_replayed(core, tmp_path, sdk_modules, phase):
    from pydantic_ai import Conversation

    from companion.agent_persistence import ConversationStore, recoverable

    _, job = chat(core, tmp_path)
    persistence = ConversationStore(core, job, [])
    persistence._save(Conversation(conversation_id=job["job_id"]), phase)
    if phase == "READY":
        request = sdk_modules["budget"].reserve(core.store, core.settings, job["job_id"],
            "chat", core.context(job), core.clock(), clock=core.clock)
        core.store.insert("agent_tool_calls", job_id=job["job_id"], tool_call_id="uncertain",
            tool_name="current_time", args_sha256="unused", model_request_id=request,
            state="STARTED", created_at=core.clock(), updated_at=core.clock())
    assert not recoverable(core.store, job["job_id"])
    assert persistence.row()["phase"] == "STOPPED"
    with pytest.raises(CoreError, match="CONVERSATION_OUTCOME_UNAVAILABLE"):
        persistence.load()
    if phase == "READY":
        assert core.store.one("SELECT state FROM agent_tool_calls")["state"] == "UNKNOWN"
    assert not core.store.rows("SELECT * FROM outbox")



def test_core_entry_commits_sdk_output_with_original_outbox(core, tmp_path, sdk_modules):
    _, job = chat(core, tmp_path)
    core.store.execute("UPDATE jobs SET state='QUEUED',lease_token=NULL WHERE job_id=%s",
                       (job["job_id"],))
    calls = []

    async def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "sdk_modules owner reply"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            core.model = sdk_modules["model"].ModelClient(core.settings, client)
            assert await core.run_once("sdk-entry-worker", job_id=job["job_id"])

    asyncio.run(scenario())
    state = core.store.one('SELECT state,last_error_code FROM jobs WHERE job_id=%s',
                          (job["job_id"],))
    assert state["state"] == "SUCCEEDED", dict(state)
    assert core.store.one("SELECT phase FROM agent_conversations")["phase"] == "COMPLETE"
    assert len(calls) == 1
    assert len(core.store.rows('SELECT * FROM outbox WHERE origin_event_id=%s',
                               (job["event_id"],))) == 1


def test_chat_only_checkpoint_needs_no_reader_permission(core, tmp_path, sdk_modules):
    from pydantic_ai import Conversation

    from companion.agent_reading import deferred_results

    _, job = chat(core, tmp_path)
    assert deferred_results(core, job, [], Conversation(conversation_id=job["job_id"])) == (
        True, None)



@pytest.mark.parametrize("concurrency", [2, 10])
def test_real_http_ingress_and_independent_sdk_worker(core, tmp_path, sdk_modules, monkeypatch,
                                                    concurrency):
    import pickle

    from pydantic import SecretStr

    from tests import test_owner_input
    from tests.support.core_http import real_core_http
    from tests.test_owner_input import captured, inbound

    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    core.settings.control_token = SecretStr("synthetic-sdk-owner-control")
    original_prepare = test_owner_input.prepare

    def prepare(*args):
        policy = original_prepare(*args)
        core.settings.paid_model = core.settings.paid_model.model_copy(
            update={"concurrency": concurrency, "concurrency_basis": (
                "local_active_attempts_v2" if concurrency == 10 else "unresolved_liabilities_v1")})
        return policy.model_copy(update={"paid_contract_sha256":
            paid_budget.sha(paid_budget.snapshot(core.settings))})

    monkeypatch.setattr(test_owner_input, "prepare", prepare)
    capture, _ = captured(core, tmp_path)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as client:
            approved = await client.post("/internal/v1/owner-authority/inputs/approve",
                                         json=capture.model_dump(mode="json"))
            assert approved.status_code == 200, approved.text
            accepted = await client.post("/internal/v1/inbox/events",
                json=inbound(core, capture).model_copy(
                    update={"trace_id": "trace_" + "a" * 32}).model_dump(mode="json"),
                headers={"X-Idempotency-Key": "sdk-real-http-input"})
            assert accepted.status_code == 202, accepted.text
            job_id = accepted.json()["job_id"]
        configuration = tmp_path / "synthetic-worker.pickle"
        configuration.write_bytes(pickle.dumps((core.settings, core.clock.now, job_id)))
        process = await asyncio.create_subprocess_exec(sys.executable,
            str(Path(__file__).with_name("agent_sdk_worker.py")), str(configuration),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, diagnostics = await asyncio.wait_for(process.communicate(), 40)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, (output + diagnostics).decode(errors="replace")
        assert "independent synthetic reasoning" not in (output + diagnostics).decode(
            errors="replace")
        metrics = [json.loads(line) for line in output.decode().splitlines()
                   if line.startswith("{")]
        stages = {row["stage"] for row in metrics}
        assert stages >= {"context_build", "request_plan", "request_checkpoint", "request_reserve",
                          "transport_sources", "model_transport", "t_model_request", "t_model_done"}
        from scripts.benchmark_qq_beta import FIELDS, summarize

        assert all(set(row) <= FIELDS for row in metrics)
        assert summarize(metrics, "OFFLINE_FIXTURE")["model_budget_estimate_complete"]
        assert len(core.store.rows("SELECT * FROM model_usage WHERE state='SETTLED'")) == 1
        assert len(core.store.rows('SELECT * FROM outbox WHERE origin_event_id=%s',
                                  (capture.event_id,))) == 1
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as client:
            denied = await client.post("/internal/v2/cognition", json={"kind": "model"})
            assert denied.status_code == 401
            diagnostic = await client.post("/internal/v2/cognition", json={"kind": "model"},
                headers={"Authorization": "Bearer synthetic-sdk-owner-control"})
            assert diagnostic.status_code == 200, diagnostic.text
            assert diagnostic.headers["cache-control"] == "no-store"
            assert diagnostic.json()["items"][0]["reasoning"]["text"] == (
                "independent synthetic reasoning")
            missing = await client.post("/internal/v1/deliveries/lease", json={
                "worker_id": "sdk-delivery"})
            assert missing.status_code >= 400
            leased = await client.post("/internal/v1/deliveries/lease", json={
                "worker_id": "sdk-delivery", "authority": capture.authority})
            assert leased.status_code == 200, leased.text
            item = leased.json()["items"][0]
            check = {"authority": item["authority"], "message_id": item["message_id"],
                     "event_id": item["origin_event_id"], "content_sha256": item["content_sha256"],
                     "lease_token": item["lease_token"], "reply_revision": item["reply_revision"]}
            before = await client.post("/internal/v1/owner-authority/send-check", json=check)
            assert before.status_code >= 400
            begun = await client.post(
                f"/internal/v1/deliveries/{item['message_id']}/begin-send",
                json={"lease_token": item["lease_token"]})
            assert begun.status_code == 200, begun.text
            approved = await client.post("/internal/v1/owner-authority/send-check", json=check)
            assert approved.status_code == 200 and approved.json()["approved"]
            changed = await client.post("/internal/v1/owner-authority/send-check",
                json={**check, "content_sha256": "f" * 64})
            assert changed.status_code >= 400
            from companion import owner_authority
            from companion.owner_authority_protocol import AuthorityStop

            owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                operation_id="sdk-http-stop-before-send", authority=capture.authority))
            stopped = await client.post("/internal/v1/owner-authority/send-check", json=check)
            assert stopped.status_code >= 400
        assert core.store.one("SELECT state FROM outbox")["state"] == "SENDING"

    asyncio.run(scenario())



def test_erasure_contract_scrubs_sdk_state_without_refunding_usage(core, tmp_path, sdk_modules):
    from pydantic_ai import Conversation

    from companion.agent_persistence import ConversationStore
    from companion.erasure import ErasureScope

    _, job = chat(core, tmp_path)
    persistence = ConversationStore(core, job, [])
    persistence._save(Conversation(conversation_id=job["job_id"]), "READY")
    request = sdk_modules["budget"].reserve(core.store, core.settings, job["job_id"],
        "chat", core.context(job), core.clock(), clock=core.clock)
    core.store.insert("agent_tool_calls", job_id=job["job_id"], tool_call_id="unknown-effect",
        tool_name="current_time", args_sha256="private-args", model_request_id=request,
        state="STARTED", result_json='{"private":"result"}', metadata_json='{"private":"source"}',
        created_at=core.clock(), updated_at=core.clock())
    before = dict(core.store.one('SELECT * FROM model_usage WHERE request_id=%s', (request,)))
    from companion import erasure

    contracts = [item for item in erasure.cleanup_contracts()
                 if item.name in {"agent-conversations", "captured-images"}]
    assert len(contracts) == 2
    with core.store.transaction():
        for _ in range(2):
            for contract in contracts:
                contract.erase(core.store, ErasureScope(frozenset({job["event_id"]}), True))
    row = persistence.row()
    assert row["phase"] == "STOPPED"
    assert row["conversation_json"] is None and row["output_text"] is None
    assert row["snapshots_json"] == "[]"
    tool = core.store.one("SELECT * FROM agent_tool_calls")
    assert tool["state"] == "UNKNOWN" and tool["args_sha256"] == ""
    assert tool["metadata_json"] is None and tool["result_json"] is None
    after = core.store.one('SELECT * FROM model_usage WHERE request_id=%s', (request,))
    assert dict(after) == before


def test_snapshot_revalidation_checks_original_tool_purpose(core, tmp_path, sdk_modules):
    _, job = chat(core, tmp_path)
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_PURPOSE_REQUIRED"):
        core._check_context_snapshots(job, [{"kind": "sources", "purpose": "goals",
                                            "event_ids": [job["event_id"]]}])
