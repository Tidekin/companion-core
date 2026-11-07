"""Saved per-attempt diagnostics and offline pressure; no live provider or QQ I/O."""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from companion import beta_observation, budget, paid_budget, token_measurement
from companion.cognition_read import CognitionRead, measurement
from companion.contracts import CoreError
from companion.dialogue_history import fit_context
from companion.model import ModelClient
from companion.store import encode
from tests.support.context_meter_inspector import native_job, recorded_attempts
from tests.support.token_measurement import configure


@pytest.mark.parametrize("unknown", [False, True])
def test_native_inspector_projects_each_complete_request_without_recounting(
    core, tmp_path, monkeypatch, unknown,
):
    job, requests = asyncio.run(recorded_attempts(core, tmp_path, unknown=unknown))
    assert len(requests) == 2
    tool_results = [m for m in requests[1]["messages"] if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in tool_results} == {"first-call", "second-call"}
    assert len(requests[0]["tools"]) == len(requests[1]["tools"]) == 2
    admissions = core.store.rows("SELECT * FROM model_admissions ORDER BY rowid")
    before = list(core.store.db.iterdump())
    ledger = Path(core.settings.paid_model.journal_path).read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("historical diagnostics must not recount or generate")

    monkeypatch.setattr(token_measurement, "measure_request", forbidden)
    result = beta_observation.inspect_context(core.settings, job["trace_id"])
    assert not result["blockers"]
    calls = result["trace"]["jobs"][0]["model_calls"]
    assert len(calls) == 2
    for call, admission, payload in zip(calls, admissions, requests, strict=True):
        saved = json.loads(admission["measurement_json"])
        assert saved["payload_sha256"] == admission["payload_sha256"] == paid_budget.sha(payload)
        assert call["measurement"] == measurement(admission["measurement_json"])
        assert call["measurement"]["count_kind"] == "exact_local_render"
        assert saved["input_tokens"] + saved["reservation_margin"] == admission["input_bound"]
        assert call["context_regions"] == call["trimming_reason"] == "NOT_RECORDED"
        item = CognitionRead(core).page("model", record_id=admission["request_id"])["items"][0]
        assert item["measurement"] == call["measurement"]
        assert item["usage"]["observed"] == (call["state"] == "SETTLED")
        assert item["context_regions"] == item["trimming_reason"] == "NOT_RECORDED"
    if unknown:
        assert calls[-1]["state"] == "UNKNOWN"
        assert calls[-1]["prompt_tokens"] is calls[-1]["completion_tokens"] is None
        assert calls[-1]["accounted_or_reserved_micro_usd"] > 0
    assert "SYNTHETIC_PRIVATE_TOOL_RESULT" not in encode(result)
    assert core.settings.model_url not in encode(result)
    assert list(core.store.db.iterdump()) == before
    assert Path(core.settings.paid_model.journal_path).read_bytes() == ledger


@pytest.mark.parametrize("receipt", ["[]", "null", "1", '"PRIVATE"', "{broken", "x" * 16385],
                         ids=["array", "null", "number", "string", "invalid-json", "oversized"])
def test_bad_saved_measurement_is_explicit_and_content_free(receipt):
    assert measurement(receipt) == {"state": "INCONSISTENT"}


def test_invalid_measurement_fields_cannot_emit_arbitrary_receipt_content():
    result = measurement(encode({"count_kind": ["private"], "input_tokens": True,
        "reservation_margin": -1, "output_allowance": "private", "endpoint": "private"}))
    assert "private" not in encode(result)
    assert result["count_kind"] == "NOT_RECORDED"
    assert all(result[name] is None for name in (
        "input_tokens", "reservation_margin", "output_allowance"))


def test_legacy_and_demo_have_no_invented_measurement_or_observed_usage(core):
    from tests.conftest import ingest, run

    ingest(core)
    run(core.run_once())
    row = core.store.one("SELECT request_id FROM model_usage")
    item = CognitionRead(core).page("model", record_id=row["request_id"])["items"][0]
    assert item["measurement"] == {"state": "NOT_RECORDED"}
    assert item["usage"]["observed"] is False
    assert beta_observation.inspect_context(core.settings, "trace_test_1")["trace"]["jobs"][0][
        "model_calls"][0]["measurement"] == {"state": "NOT_RECORDED"}


def test_known_unstarted_attempt_is_not_provider_observed_usage(core, tmp_path):
    configure(core, tmp_path)
    job = native_job(core)
    messages = core.context(job)
    request_id = budget.reserve(core.store, core.settings, job["job_id"], "chat", messages,
                                core.clock(), clock=core.clock)
    calls = []

    async def tamper(request):
        request._content = b'{}'

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(500)),
            event_hooks={"request": [tamper]}) as client:
            with pytest.raises(CoreError, match="MODEL_ADMISSION_REQUEST_CHANGED"):
                await budget.complete(ModelClient(core.settings, client), core.store, core.settings,
                                      request_id, messages, "chat", core.clock)

    asyncio.run(exercise())
    assert calls == []
    call = beta_observation.inspect_context(core.settings, job["trace_id"])["trace"]["jobs"][0][
        "model_calls"][0]
    assert call["state"] == "SETTLED" and call["prompt_tokens"] is None
    assert call["accounted_or_reserved_micro_usd"] == 0
    item = CognitionRead(core).page("model", record_id=request_id)["items"][0]
    assert item["usage"]["observed"] is False
    assert item["measurement"]["state"] == "RECORDED"


def test_media_reservation_is_an_estimate_and_not_observed_usage(core, tmp_path):
    from tests.support.paid_image_budget import image_approval, image_messages, reserve_image

    configure(core, tmp_path)
    image_approval(core, tmp_path, provider="deepseek", model="deepseek-flash",
                   endpoint="https://api.deepseek.com")
    job_id, request_id = reserve_image(core, image_messages())
    trace = core.store.one("SELECT e.trace_id FROM events e JOIN jobs j USING(event_id) "
                           "WHERE j.job_id=?", (job_id,))["trace_id"]
    call = beta_observation.inspect_context(core.settings, trace)["trace"]["jobs"][0][
        "model_calls"][0]
    assert call["state"] == "RESERVED" and call["prompt_tokens"] is None
    assert call["measurement"]["count_kind"] == "estimate"
    assert call["measurement"]["input_tokens"] >= 1000
    assert call["measurement"]["output_allowance"] == core.settings.output_tokens("content_vision")
    item = CognitionRead(core).page("model", record_id=request_id)["items"][0]
    assert item["usage"]["observed"] is False
    assert "data:image" not in encode(item)


@pytest.mark.parametrize("rounds", [100, 1000])
def test_offline_long_dialogue_pressure_replays_full_tool_payloads_and_hard_refusals(
    core, tmp_path, rounds,
):
    """No paid I/O: n candidate requests, real reserve refusals, one settled sample.

    This exercises existing selection and measurement, not answer quality or a
    new planner. Oversized required tool results are preserved and refused.
    """
    configure(core, tmp_path)
    core.settings.paid_model = core.settings.paid_model.model_copy(
        update={"max_input_tokens": 4096})
    job = native_job(core)
    rules = {"role": "system", "content": "可信规则保持完整。"}
    tools = [{"type": "function", "function": {"name": name, "description": "合成只读工具",
        "parameters": {"type": "object", "properties": {}}}} for name in ("one", "two")]
    history, counts, refused, fits = [], [], 0, 0
    for turn in range(rounds):
        current = {"role": "user", "content": f"第{turn}轮：保留原话与🙂。"}
        messages = fit_context(rules, [{"role": "user", "content": "可选参考" * 300}],
            history[-6:], [current], 1000, settings=core.settings)
        assert messages[0] == rules and messages[-1] == current
        calls = [{"id": name, "type": "function", "function": {"name": name,
                  "arguments": "{}"}} for name in ("one", "two")]
        suffix = [{"role": "assistant", "content": None, "tool_calls": calls},
                  *[{"role": "tool", "tool_call_id": name,
                     "content": "必需结果" * (5000 if turn % 16 == 0 else 5)}
                    for name in ("one", "two")]]
        wire = [*messages, *suffix]
        measured = token_measurement.measure_request(core.settings, wire, tools=tools)
        assert measured.count_kind == "exact_local_render"
        assert measured.output_allowance == core.settings.output_tokens("chat")
        counts.append(measured.reserved_input_tokens)
        if measured.reserved_input_tokens > core.settings.paid_model.max_input_tokens:
            with pytest.raises(CoreError, match="MODEL_INPUT_LIMIT"):
                budget.reserve(core.store, core.settings, job["job_id"], "chat", wire,
                               core.clock(), clock=core.clock, tools=tools)
            assert not core.store.rows("SELECT * FROM model_usage")
            refused += 1
        else:
            fits += 1
        history.append([current, {"role": "assistant", "content": "合成回复" * 100}])
    assert refused > 0 and fits > 0
    request_id = budget.reserve(core.store, core.settings, job["job_id"], "chat", wire,
                                core.clock(), clock=core.clock, tools=tools)

    async def settle_sample():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, json={"choices": [{"message": {"content": "sample"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 4}}))) as client:
            await budget.complete(ModelClient(core.settings, client), core.store, core.settings,
                                  request_id, wire, "chat", core.clock, tools=tools)

    asyncio.run(settle_sample())
    ordered = sorted(counts)
    print(encode({"candidate_requests": rounds, "fit_configured_input_limit": fits,
        "reserve_refusals": refused, "mock_provider_requests": 1,
        "reserved_input_p50": ordered[len(ordered) // 2],
        "reserved_input_p95": ordered[(len(ordered) - 1) * 95 // 100]}))


def test_real_http_and_independent_worker_expose_both_sdk_attempts(core, tmp_path):
    from tests.support.native_consumer_entry import environment

    configure(core, tmp_path)

    async def exercise():
        async with environment(core, tmp_path, purposes=("chat",), paid_options={
            "provider": "deepseek", "model": "deepseek-flash",
            "endpoint": "https://api.deepseek.com",
        }) as (entry, _):
            source = await entry.admit(1, "synthetic meter inspection")
            settings = core.settings.model_dump(mode="json")
            for name in ("model_key", "bridge_token", "control_token"):
                settings[name] = getattr(core.settings, name).get_secret_value()
            job_id = entry.accepted[source["event_id"]]["job_id"]
            path = tmp_path / "meter-worker.json"
            path.write_text(encode({"settings": settings, "now": core.clock(),
                                    "job_id": job_id}), "utf-8")
            child = await asyncio.create_subprocess_exec(sys.executable, "-B",
                str(Path(__file__).with_name("context_meter_worker.py")), str(path),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(child.communicate(), 40)
            except BaseException:
                if child.returncode is None:
                    child.kill()
                await child.wait()
                raise
            assert child.returncode == 0, stderr.decode("utf-8", errors="replace")
            assert json.loads(stdout) == {"mock_provider_requests": 2}
            trace_id = core.store.one("SELECT trace_id FROM events WHERE event_id=?",
                                      (source["event_id"],))["trace_id"]
            trace = beta_observation.inspect_context(core.settings, trace_id)["trace"]
            calls = trace["jobs"][0]["model_calls"]
            assert len(calls) == 2 and all(c["state"] == "SETTLED" for c in calls)
            assert all(c["measurement"]["count_kind"] == "exact_local_render" for c in calls)
            context = trace["jobs"][0]["context"]
            assert context["state"] == "RECORDED"
            assert context["source_validity"] == "NOT_REVALIDATED"
            assert context["pending"] is None and len(context["requests"]) == 2
            saved = core.store.one("SELECT snapshots_json FROM agent_conversations WHERE job_id=?",
                                  (job_id,))
            plan = next(item["metadata"]["core_context"]
                        for item in json.loads(saved["snapshots_json"])
                        if item["kind"] == "context_plan")
            assert context["plan_sha256"] == paid_budget.sha(plan)
            admissions = core.store.rows(
                "SELECT a.* FROM model_admissions a JOIN model_usage m USING(request_id) "
                "WHERE m.job_id=? ORDER BY m.rowid", (job_id,))
            assert len(admissions) == 2
            for record, call, admission in zip(context["requests"], calls, admissions, strict=True):
                assert record["state"] == "ADMITTED"
                assert record["request_ref_sha256"] == call["request_ref_sha256"] == (
                    hashlib.sha256(admission["request_id"].encode()).hexdigest())
                assert record["payload_sha256"] == admission["payload_sha256"]
                assert record["measurement_sha256"] == paid_budget.sha(
                    json.loads(admission["measurement_json"]))
            before = core.store.rows("SELECT * FROM model_usage ORDER BY request_id")
            await entry.recall(1)
            erased = beta_observation.inspect_context(core.settings, trace_id)["trace"]
            assert erased["state"] == "REDACTED"
            assert erased["jobs"][0]["context"]["state"] == "REDACTED"
            assert core.store.rows("SELECT * FROM model_usage ORDER BY request_id") == before

    asyncio.run(exercise())
