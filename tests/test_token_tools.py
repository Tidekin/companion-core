"""The pinned renderer counts native SDK tool schemas and continuation payloads."""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from companion import budget, paid_budget
from companion import token_measurement as tm
from companion.contracts import CoreError
from companion.model import ModelClient, model_payload
from companion.store import encode
from tests.owner_model_fixtures import authorized_job
from tests.support.token_measurement import configure
from tests.support.token_tools import CALL, CONTINUATION, MESSAGES, TOOLS


@pytest.mark.parametrize("messages", [MESSAGES, CONTINUATION])
def test_exact_tools_match_upstream_case_loader_without_mutating_wire(core, tmp_path, messages):
    configure(core, tmp_path, "exact_required")
    wire = model_payload(core.settings, messages, "chat", tools=TOOLS)
    original = encode(wire)
    case = tmp_path / "upstream-tools.json"
    case.write_text(json.dumps({"messages": messages, "tools": TOOLS}), encoding="ascii")
    renderer, tokenizer = tm._local_encoder()
    expected, images = renderer.encode_case(renderer.load_cases(str(case))[0], "chat")
    measured = tm.measure_request(core.settings, messages, tools=TOOLS)
    assert not images
    assert measured.count_kind == "exact_local_render"
    assert measured.method == "deepseek-v41-chat-tools-v1"
    assert measured.input_tokens == len(tokenizer.encode(expected, add_special_tokens=False).ids)
    assert measured.payload_sha256 == paid_budget.sha(wire)
    assert encode(wire) == original
    assert "current_time" in expected
    assert measured.input_tokens > tm.measure_request(core.settings, MESSAGES).input_tokens


def test_tool_mapping_requires_verified_system_role_and_plain_text(core, tmp_path):
    configure(core, tmp_path, "exact_required")
    for messages in (MESSAGES[1:], [{"role": "system", "content": "rules"},
                                    {"role": "user", "content": []}]):
        measured = tm.measure_request(core.settings, messages, tools=TOOLS)
        assert measured.count_kind == "unsupported"
        with pytest.raises(CoreError, match="MODEL_TOKEN_COUNT_UNSUPPORTED"):
            _ = measured.reserved_input_tokens
    changed = deepcopy(TOOLS)
    changed[0]["function"]["unverified_extension"] = True
    assert tm.measure_request(core.settings, MESSAGES, tools=changed).count_kind == "unsupported"


@pytest.mark.parametrize("tamper", [False, True])
def test_tool_payload_admission_and_actual_transport_share_measurement(core, tmp_path, tamper):
    configure(core, tmp_path, "exact_required")
    request_id = budget.reserve(core.store, core.settings, authorized_job(core),
        "chat", CONTINUATION,
                                core.clock(), tools=TOOLS)
    saved = core.store.one('SELECT * FROM model_admissions WHERE request_id=%s', (request_id,))
    measurement = tm.measure_request(core.settings, CONTINUATION, tools=TOOLS)
    assert saved["input_bound"] == measurement.reserved_input_tokens
    assert json.loads(saved["measurement_json"]) == measurement.receipt()
    received = []

    async def hook(request):
        if tamper:
            body = json.loads(request.content)
            body["tools"][0]["function"]["description"] = "changed schema"
            request._content = json.dumps(body).encode()

    def provider(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "synthetic"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 2}})

    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider),
                                    event_hooks={"request": [hook]}) as client:
            return await budget.complete(ModelClient(core.settings, client), core.store,
                core.settings, request_id, CONTINUATION, "chat", core.clock, tools=TOOLS)

    if tamper:
        with pytest.raises(CoreError):
            asyncio.run(invoke())
        assert received == []
    else:
        assert asyncio.run(invoke()).text == "synthetic"
        assert received == [model_payload(core.settings, CONTINUATION, "chat", tools=TOOLS)]
        assert core.store.one('SELECT state FROM model_usage WHERE request_id=%s',
                              (request_id,))["state"] == "SETTLED"


def test_native_sdk_tool_roundtrip_keeps_exact_required_usable(core, tmp_path):
    from companion.agent_runtime import run_conversation
    from companion.agent_tools import core_query_tools

    configure(core, tmp_path, "exact_required")
    job_id = authorized_job(core)
    job = core.store.one((
        'SELECT j.*,e.conversation_id,e.body_json,e.trace_id FROM jobs j JOIN'
        ' events e USING(event_id) WHERE job_id=%s'
    ), (job_id,))
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        response = {"content": "synthetic tool result"}
        if len(requests) == 1:
            response = {"content": None, "tool_calls": [CALL]}
        return httpx.Response(200, json={"choices": [{"message": response}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 2}})

    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            core.model = ModelClient(core.settings, client)
            return await run_conversation(core, job, core.context(job),
                toolsets=[core_query_tools(core, job, [])])

    assert asyncio.run(invoke()).output == "synthetic tool result"
    admissions = core.store.rows(
        'SELECT a.*,u.state FROM model_admissions a JOIN model_usage u '
        'USING(request_id) ORDER BY a.record_sequence'
    )
    assert len(admissions) == len(requests) == 2
    for admission, payload in zip(admissions, requests, strict=True):
        measured = json.loads(admission["measurement_json"])
        assert measured["count_kind"] == "exact_local_render"
        assert measured["method"] == "deepseek-v41-chat-tools-v1"
        assert measured["payload_sha256"] == paid_budget.sha(payload)
        assert admission["state"] == "SETTLED"
    assert any(message["role"] == "tool" for message in requests[-1]["messages"])
    assert core.store.rows("SELECT * FROM outbox") == []
