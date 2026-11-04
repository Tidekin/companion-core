"""Native SDK recall plus read-only historical approval compatibility checks."""

import hashlib
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError
from websockets.asyncio.server import serve

from companion import live_text
from companion.config import LiveTextWindowApproval
from companion.model import ModelClient
from companion.privacy import advance_epoch, epoch
from companion.retrieval import INDEX_VERSION
from companion.store import after, encode
from companion_delivery_protocol import digest
from companion_onebot_bridge import network
from companion_onebot_bridge.spool import Spool
from companion_onebot_bridge.window import TestWindow, WindowPlan
from tests.conftest import run
from tests.sdk_reader_fixture import answer, call
from tests.support.failure_notice import assert_failure_notice
from tests.support.native_recall_entry import native_recall, returned
from tests.support.onebot import provider
from tests.test_live_text_window import live_fixture
from tests.test_memory_commands import fact
from tests.test_memory_review_output import assert_settled_calls
from tests.test_onebot_capture import bootstrap_frames, capture_approval
from tests.test_onebot_delivery import core_client
from tests.test_onebot_direct import raw_event
from tests.test_onebot_window import reply_approval


def scope_facts(core):
    memory_id = fact(core, 900, "本次临时项目的验收代号是青竹七号。")
    outsider = fact(core, 901, "未批准的另一个项目代号是紫云三号。")
    return (
        memory_id,
        outsider,
        [
            live_text.memory_fact(
                core.store,
                core.settings,
                core.clock,
                memory_id,
            )
        ],
    )


@asynccontextmanager
async def readonly_window(
    core,
    tmp_path,
    *,
    respond=None,
    options=None,
    paid=None,
    request_hook=None,
    core_options=None,
    query="本次临时项目的验收代号是什么？",
    queries=None,
    raw_messages=None,
):
    memory_id, outsider, pins = scope_facts(core)
    calls, sends, scores = [], [], []
    original_scores = core.memory.index.scores_async
    # An existing out-of-scope fact with missing evidence must remain untouched.
    core.store.execute("DELETE FROM memory_evidence WHERE memory_id=?", (outsider,))
    core.memory.observe_trait(
        "未批准私密特征",
        core.store.one("SELECT * FROM events WHERE event_id='evt_test_901'"),
        core.clock(),
    )
    core.store.execute("UPDATE traits SET confidence=1")

    async def semantic(query, rows, **kwargs):
        scores.append([r["memory_id"] for r in rows])
        return await original_scores(query, rows, **kwargs)

    core.memory.index.scores_async = semantic
    number = 0

    async def source(ws):
        nonlocal number
        number += 1
        for frame in bootstrap_frames():
            await ws.send(json.dumps(frame))
        event = raw_event(number, text=queries[number - 1] if queries else query)
        if raw_messages:
            event["message"] = raw_messages[number - 1]
        await ws.send(json.dumps(event))
        await ws.wait_closed()

    def model_response(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if respond:
            return respond(request, payload, len(calls))
        last = payload["messages"][-1]["content"]
        if '"operation":"recall"' in last:
            content = json.dumps({"relevant": [m["ref"] for m in json.loads(last)["memories"]]})
        else:
            content = "青竹七号。"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    async with serve(source, "127.0.0.1", 0) as server:
        cfg, plan = live_fixture(
            core,
            tmp_path,
            core_options={"memory_enabled": True, **(core_options or {})},
            bridge_options={
                "capture_signing_key": Ed25519PrivateKey.generate().private_bytes_raw().hex(),
                "onebot_ws_url": f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
            },
            paid_options={"per_request_calls": 2, **(paid or {})},
            memory_scope="approved_facts",
            memory_facts=pins,
            memory_approval_sha256="f" * 64,
            **(options or {}),
        )
        async with (
            core_client(core) as client,
            network.private_http_client(
                cfg.onebot_http_url,
                cfg.onebot_token,
                transport=httpx.MockTransport(lambda r: provider(r, sends)),
            ) as protocol,
            httpx.AsyncClient(
                transport=httpx.MockTransport(model_response),
                event_hooks={
                    "request": [request_hook] if request_hook else [],
                },
            ) as model,
        ):
            client.base_url = cfg.core_url
            core.model = ModelClient(core.settings, model)
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: core.clock.now)
                await window.open(client)

                async def capture():
                    await window.capture_once(client, protocol)
                    await window.approve_captured(client, capture_approval(window))

                async def send():
                    return await window.send_once(client, protocol, reply_approval(plan, core))

                yield window, client, capture, send, calls, sends, scores, memory_id, outsider


def test_native_bge_private_scope_and_sdk_calls_share_one_reply(core, tmp_path):
    async def scenario():
        async with native_recall(core, tmp_path) as value:
            before = epoch(core.store)
            source = await value.capture()
            assert await value.worker.run_once("native-memory-recall")
            assert len(value.calls) == 2
            assert value.scores and all(ids == [value.memory_id] for ids in value.scores)
            assert "紫云三号" not in encode(value.calls)
            assert core.store.one("SELECT status,confidence FROM memories WHERE memory_id=?",
                (value.outsider,)) == {"status": "CONFIRMED", "confidence": 1.0}
            assert core.store.one("SELECT 1 FROM memory_vectors WHERE memory_id=?",
                                  (value.memory_id,))
            assert not core.store.one("SELECT 1 FROM memory_vectors WHERE memory_id=?",
                                      (value.outsider,))
            result = json.loads(returned(value.calls[-1])[0]["content"])
            assert [row["memory_id"] for row in result["facts"]] == [value.memory_id]
            assert result["requires_relevance_review"]
            assert_settled_calls(core, 2)
            admissions = core.store.rows("SELECT * FROM model_admissions")
            assert len(admissions) == 2
            assert len({row["logical_request_id"] for row in admissions}) == 1
            job = core.store.one("SELECT job_id FROM jobs WHERE event_id=?", (source["event_id"],))
            saved = core.store.one("SELECT snapshots_json FROM agent_conversations WHERE job_id=?",
                                   (job["job_id"],))
            memories = [item for item in json.loads(saved["snapshots_json"])
                        if item["kind"] == "memory"]
            assert [row["memory_id"] for row in memories[0]["proof"]["rows"]] == [value.memory_id]
            assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
            assert len(value.sends) == 1 and epoch(core.store) == before
    run(scenario())



def test_bound_memory_apis_and_idle_extraction_cannot_bypass_scope(core, tmp_path):
    async def scenario():
        async with readonly_window(core, tmp_path) as (
            _,
            client,
            _,
            _,
            calls,
            _,
            _,
            memory_id,
            _,
        ):
            before = epoch(core.store)
            response = await client.post(
                "/internal/v1/memories/search",
                json={
                    "query": "*",
                    "requester_actor_id": "owner",
                },
            )
            assert response.status_code == 403
            response = await client.post(
                f"/internal/v1/memories/{memory_id}/decisions",
                json={
                    "actor_id": "owner",
                    "decision": "forget",
                },
            )
            assert response.status_code == 403
            assert not await core.extract_once()
            assert epoch(core.store) == before
            assert calls == []
            assert not core.store.one(
                "SELECT 1 FROM runtime_meta WHERE key='memory_extraction_lease'"
            )

    run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"memory_approval_sha256": None},
        {"memory_scope": "none"},
        {
            "history_scope": "approved_window",
            "max_history_turns": 1,
            "max_history_utf8_bytes": 1000,
        },
    ],
)
def test_r5_scope_is_explicit_and_cannot_implicitly_include_recent_history(core, tmp_path, change):
    _, _, pins = scope_facts(core)
    cfg, plan = live_fixture(core, tmp_path)
    value = core.settings.live_text_window.model_dump()
    value.update(
        memory_scope="approved_facts",
        memory_facts=pins,
        memory_approval_sha256="f" * 64,
        capture_public_key="a" * 64,
    )
    value.update(change)
    with pytest.raises(ValidationError):
        LiveTextWindowApproval.model_validate(value)
    malformed = {
        **plan.value,
        "live_text": {
            **plan.value["live_text"],
            **change,
            "memory_scope": "approved_facts",
            "memory_facts": pins,
        },
    }
    with pytest.raises(ValueError):
        WindowPlan(malformed, cfg, {})


def test_unknown_memory_tool_request_never_enqueues_reply(core, tmp_path):
    def fail(request, payload, number):
        raise httpx.ReadTimeout("synthetic unknown", request=request)

    async def scenario():
        async with native_recall(core, tmp_path, respond=fail) as value:
            await value.capture()
            assert await value.worker.run_once("unknown-tool-request")
            assert len(value.calls) == 1 and not value.sends
            assert core.store.one("SELECT scenario,state FROM model_usage") == {
                "scenario": "chat", "state": "UNKNOWN"}
            assert not core.store.rows("SELECT * FROM agent_tool_calls")
            usage = core.store.one("SELECT billed_micro_usd FROM model_usage")
            admission = core.store.one("SELECT reserved_micro_usd FROM model_admissions")
            assert usage["billed_micro_usd"] == admission["reserved_micro_usd"] > 0
            assert not core.store.rows("SELECT * FROM outbox")
            assert not await value.worker.run_once("no-unknown-replay")
    run(scenario())



@pytest.mark.parametrize("mutation", [
    "content", "evidence", "status", "needs_review", "epoch", "evidence_links",
])
def test_memory_proof_drift_after_model_receive_prevents_reply(core, tmp_path, mutation):
    def provider(request, payload, number):
        if not returned(payload):
            return httpx.Response(200, json=answer(calls=[
                call("recall-project", "memory_recall", {"query": "*"})]))
        if mutation == "content":
            core.store.execute("UPDATE memories SET content='漂移' WHERE memory_id=?",
                               (value.memory_id,))
        elif mutation == "evidence":
            core.store.execute("UPDATE events SET redacted=1 WHERE event_id=?",
                               (value.source["event_id"],))
        elif mutation == "status":
            core.store.execute("UPDATE memories SET status='HYPOTHESIS' WHERE memory_id=?",
                               (value.memory_id,))
        elif mutation == "needs_review":
            core.store.execute("UPDATE memories SET needs_review=1 WHERE memory_id=?",
                               (value.memory_id,))
        elif mutation == "epoch":
            advance_epoch(core.store)
        else:
            core.store.execute("DELETE FROM memory_evidence WHERE memory_id=?", (value.memory_id,))
        return httpx.Response(200, json=answer("旧事实不能发送。"))

    async def scenario():
        nonlocal value
        async with native_recall(core, tmp_path, respond=provider) as value:
            await value.capture()
            assert await value.worker.run_once("post-tool-source-drift")
            assert len(value.calls) == 2 and not value.sends
            assert_settled_calls(core, 2)
            assert not core.store.rows("SELECT * FROM outbox")
            assert not await value.worker.run_once("no-stale-source-replay")
    value = None
    run(scenario())



def test_native_memory_requests_share_the_five_call_cap(core, tmp_path):
    async def scenario():
        async with native_recall(core, tmp_path,
                                 paid={"window_calls": 5, "daily_calls": 5}) as value:
            for _ in range(2):
                await value.capture()
                assert await value.worker.run_once("bounded-tool-recall")
                assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
            source = await value.capture()
            assert await value.worker.run_once("bounded-final-answer")
            assert len(value.calls) == 5 and len(value.sends) == 2
            assert_settled_calls(core, 5)
            assert core.store.one("SELECT COUNT(*) n FROM model_admissions")["n"] == 5
            assert core.store.one("SELECT COUNT(*) n FROM outbox")["n"] == 3
            assert_failure_notice(core, source["event_id"])
            assert core.store.one("SELECT state,last_error_code FROM jobs WHERE event_id=?",
                (source["event_id"],)) == {"state": "FAILED",
                                         "last_error_code": "MODEL_DAILY_BUDGET_EXHAUSTED"}
    run(scenario())



def test_duplicate_memory_tool_ids_across_requests_are_rejected(core, tmp_path):
    def provider(request, payload, number):
        return httpx.Response(200, json=answer(calls=[
            call("same-tool-id", "memory_recall", {"query": "*"})]))

    async def scenario():
        async with native_recall(core, tmp_path, respond=provider) as value:
            source = await value.capture()
            assert await value.worker.run_once("duplicate-native-tool-id")
            assert len(value.calls) == 2
            assert_settled_calls(core, 2)
            assert core.store.one("SELECT COUNT(*) n FROM agent_tool_calls")["n"] == 1
            assert core.store.one("SELECT state,last_error_code FROM jobs WHERE event_id=?",
                (source["event_id"],)) == {"state": "FAILED",
                                         "last_error_code": "CONVERSATION_TOOL_ID_CONFLICT"}
            assert_failure_notice(core)
            assert not await value.worker.run_once("no-duplicate-tool-replay")
    run(scenario())



def test_empty_recall_continues_in_the_same_sdk_conversation(core, tmp_path):
    async def scenario():
        async with native_recall(core, tmp_path) as value:
            core.store.execute("UPDATE memories SET valid_to=? WHERE memory_id=?",
                               (after(core.clock(), -1), value.memory_id))
            await value.capture()
            assert await value.worker.run_once("no-memory-candidate")
            assert len(value.calls) == 2
            result = json.loads(returned(value.calls[-1])[0]["content"])
            assert result["state"] == "NO_RECORD" and result["facts"] == []
            assert "青竹七号" not in encode(value.calls)
            assert_settled_calls(core, 2)
            assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
            assert len(value.sends) == 1
    run(scenario())



@pytest.mark.parametrize("stage", ["owner_input", "memory_result"])
def test_source_drift_in_http_hook_is_rechecked_before_actual_model_io(core, tmp_path, stage):
    async def hook(request):
        if stage == "owner_input":
            core.store.execute("UPDATE events SET redacted=1 WHERE event_id=?",
                               (source["event_id"],))
        elif returned(json.loads(request.content)):
            core.store.execute("UPDATE memories SET confidence=0 WHERE memory_id=?",
                               (value.memory_id,))

    async def scenario():
        nonlocal value, source
        async with native_recall(core, tmp_path, request_hook=hook) as value:
            source = await value.capture()
            assert await value.worker.run_once("last-request-source-check")
            assert len(value.calls) == (0 if stage == "owner_input" else 1)
            assert not value.sends and not core.store.rows("SELECT * FROM outbox")
    value = source = None
    run(scenario())



@pytest.mark.parametrize("mutation", [
    "source", "epoch", "memory_snapshot", "history_snapshot",
    "memory_proof", "history_proof", "memory_rows",
])
def test_final_native_send_check_rechecks_memory_and_history_proofs(core, tmp_path, mutation):
    async def scenario():
        async with native_recall(core, tmp_path) as value:
            source = await value.capture()
            assert await value.worker.run_once("source-bound-native-reply")
            job = core.store.one("SELECT job_id FROM jobs WHERE event_id=?", (source["event_id"],))
            client = value.entry.client
            leased = await client.post("/internal/v1/deliveries/lease", json={
                "worker_id": "memory-final-check", "authority": value.entry.runtime.authority})
            assert leased.status_code == 200, leased.text
            item = leased.json()["items"][0]
            begun = await client.post(f"/internal/v1/deliveries/{item['message_id']}/begin-send",
                                     json={"lease_token": item["lease_token"]})
            assert begun.status_code == 200, begun.text
            check = dict(authority=value.entry.runtime.authority, message_id=item["message_id"],
                event_id=source["event_id"], lease_token=item["lease_token"],
                content_sha256=digest(item["content"]), reply_revision=item["reply_revision"])
            positive = await client.post("/internal/v1/owner-authority/send-check", json=check)
            assert positive.status_code == 200 and positive.json()["approved"]
            usage = core.store.rows("SELECT * FROM model_usage")
            if mutation == "source":
                core.store.execute("UPDATE memories SET content='变更事实' WHERE memory_id=?",
                                   (value.memory_id,))
            elif mutation == "epoch":
                advance_epoch(core.store)
            else:
                saved = core.store.one("SELECT * FROM agent_conversations WHERE job_id=?",
                                       (job["job_id"],))
                snapshots = json.loads(saved["snapshots_json"])
                kind = "history" if mutation.startswith("history") else "memory"
                if mutation.endswith("snapshot"):
                    snapshots = [entry for entry in snapshots if entry["kind"] != kind]
                else:
                    proof = next(entry["proof"] for entry in snapshots if entry["kind"] == kind)
                    if mutation == "memory_rows":
                        proof["rows"] = []
                    elif mutation == "memory_proof":
                        proof["rows"][0]["content"] = "变更工具结果"
                    else:
                        proof.append({"event_id": value.source["event_id"],
                                      "source_sha256": "a" * 64})
                core.store.execute("UPDATE agent_conversations SET snapshots_json=? WHERE job_id=?",
                                   (encode(snapshots), job["job_id"]))
            rejected = await client.post("/internal/v1/owner-authority/send-check", json=check)
            assert rejected.status_code in {403, 409}, rejected.text
            assert len(value.calls) == 2 and not value.sends
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert core.store.one("SELECT state FROM outbox WHERE message_id=?",
                                 (item["message_id"],)) == {"state": "SENDING"}
    run(scenario())



def test_native_memory_result_survives_the_initial_context_soft_bound(core, tmp_path):
    async def scenario():
        async with native_recall(core, tmp_path, query="项目代号" + "是" * 450,
                                 tool_query="项目验收代号",
                                 core_options={"context_soft_tokens": 1000}) as value:
            await value.capture()
            assert await value.worker.run_once("bounded-sdk-memory-result")
            assert len(value.calls) == 2
            result = json.loads(returned(value.calls[-1])[0]["content"])
            assert [row["memory_id"] for row in result["facts"]] == [value.memory_id]
            assert "青竹七号" in encode(value.calls[-1]["messages"])
            assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
    run(scenario())



@pytest.mark.parametrize("change", ["expired", "unconfirmed", "conflict", "approval_drift"])
def test_source_eligibility_cannot_be_replaced_with_an_approval_hash(core, tmp_path, change):
    async def scenario():
        async with readonly_window(core, tmp_path) as (
            _,
            _,
            capture,
            _,
            calls,
            sends,
            _,
            memory_id,
            _,
        ):
            if change == "expired":
                core.store.execute(
                    "UPDATE events SET ingested_at=? WHERE event_id='evt_test_900'",
                    (after(core.clock(), -86400 * 10000),),
                )
            elif change == "unconfirmed":
                core.store.execute(
                    "UPDATE memories SET status='HYPOTHESIS' WHERE memory_id=?", (memory_id,)
                )
            elif change == "conflict":
                core.store.execute(
                    "UPDATE memories SET needs_review=1 WHERE memory_id=?", (memory_id,)
                )
            else:
                core.settings.live_text_window = core.settings.live_text_window.model_copy(
                    update={"memory_approval_sha256": "1" * 64},
                )
            with pytest.raises((ValueError, httpx.HTTPStatusError)):
                await capture()
            assert calls == sends == []

    run(scenario())


@pytest.mark.parametrize("result", [
    '{"relevant":["unapproved"]}', '{"relevant":["m0","m0"]}', "invalid",
])
@pytest.mark.parametrize("prefetched", [False, True], ids=["empty-prefetch", "pinned-prefetch"])
def test_invalid_memory_tool_arguments_stop_without_tool_facts_or_fallback(
    core, tmp_path, monkeypatch, result, prefetched,
):
    def provider(request, payload, number):
        requested = call("invalid-recall", "memory_recall", {})
        requested["function"]["arguments"] = result
        return httpx.Response(200, json=answer(calls=[requested]))

    async def scenario():
        async with native_recall(core, tmp_path, respond=provider) as value:
            lookup = value.worker.memory_context.recall_async
            queries = []

            async def prefetch(query, *args, **kwargs):
                queries.append(query)
                assert query == value.query, "Invalid SDK arguments reached memory lookup"
                return await lookup(query, *args, **kwargs) if prefetched else {"rows": []}

            monkeypatch.setattr(value.worker.memory_context, "recall_async", prefetch)
            await value.capture()
            assert await value.worker.run_once("invalid-sdk-memory-arguments")
            assert len(value.calls) == 2 and not value.sends
            assert_settled_calls(core, 2)
            assert not core.store.rows("SELECT * FROM agent_tool_calls")
            assert_failure_notice(core)
            assert ("青竹七号" in encode(value.calls)) is prefetched
            assert all("青竹七号" not in m["content"] for payload in value.calls
                       for m in payload["messages"] if m["role"] == "tool")
            assert queries == [value.query]
            assert not await value.worker.run_once("no-invalid-tool-replay")
    run(scenario())



def test_historical_approved_scope_does_not_repair_or_vectorize_unapproved_facts(core, tmp_path):
    async def scenario():
        async with readonly_window(core, tmp_path) as values:
            _, _, _, _, calls, sends, scores, memory_id, outsider = values
            before = epoch(core.store)
            rows = await core.memory.search_async("项目验收代号", memory_ids=[memory_id],
                                                   now=core.clock())
            assert [row["memory_id"] for row in rows] == [memory_id]
            assert scores and all(ids == [memory_id] for ids in scores)
            assert core.store.one("SELECT status,confidence FROM memories WHERE memory_id=?",
                                  (outsider,)) == {"status": "CONFIRMED", "confidence": 1.0}
            assert not core.store.one("SELECT 1 FROM memory_vectors WHERE memory_id=?", (outsider,))
            assert calls == sends == [] and epoch(core.store) == before
            assert not core.store.rows("SELECT * FROM model_usage")
    run(scenario())


def test_candidate_vector_cache_does_not_consume_one_sql_variable_per_memory(core, monkeypatch):
    ids = [fact(core, number, f"第{number}个合成项目代号是青竹。") for number in range(4)]
    rows = core.store.rows("SELECT * FROM memories ORDER BY rowid")
    for row in rows:
        core.store.insert(
            "memory_vectors",
            memory_id=row["memory_id"],
            model_version=INDEX_VERSION,
            content_hash=hashlib.sha256(row["content"].encode()).hexdigest(),
            vector=np.asarray([1.0], dtype="<f4").tobytes(),
        )
    monkeypatch.setattr(
        core.memory.index,
        "warmup",
        lambda: SimpleNamespace(
            query_embed=lambda query: iter([np.asarray([1.0], dtype="<f4")]),
        ),
    )
    original = core.store.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 2)
    try:
        assert core.memory.index.scores("合成查询", rows[:3]) == {key: 1.0 for key in ids[:3]}
    finally:
        core.store.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, original)
