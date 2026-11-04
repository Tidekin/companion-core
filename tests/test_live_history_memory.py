"""Explicit B1 scopes: native retrieval, loopback capture and synthetic model/send I/O."""

import json
from contextlib import asynccontextmanager
from copy import deepcopy

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from companion import live_text, owner_authority, owner_input, paid_budget
from companion.backup import backup, restore
from companion.config import LiveTextWindowApproval
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from companion.privacy import advance_epoch
from companion.store import after, encode
from companion_onebot_bridge.window import WindowPlan, digest, stored_plan
from tests.conftest import run
from tests.sdk_reader_fixture import answer, call
from tests.support.failure_notice import assert_failure_notice
from tests.test_live_continuity import history_snapshot, native_continuity
from tests.test_live_text_window import live_fixture
from tests.test_memory_commands import fact
from tests.test_owner_authority import activate


def combined_options(**changes):
    return {
        "history_scope": "approved_window",
        "history_approval_sha256": "e" * 64,
        "max_history_turns": 2,
        "max_history_utf8_bytes": 4000,
        **changes,
    }


@asynccontextmanager
async def native_combined(core, tmp_path, *, options=None, core_options=None,
                          queries=None, paid=None, respond=None, prefetched=False):
    """Tool-boundary cases isolate an empty initial prefetch; one case also pins it."""
    memory_id = fact(core, 900, "本次临时项目的验收代号是青竹七号。")
    outsider = fact(core, 901, "未批准的另一个项目代号是紫云三号。")
    # Preserve valid evidence; native owner-private recall excludes public facts.
    core.store.execute("UPDATE memories SET privacy_scope='public' WHERE memory_id=?", (outsider,))
    scores = []
    original_recall = core.memory_context.recall_async
    prefetched_jobs = set()

    async def observed_recall(*args, **kwargs):
        job = core.store.one("SELECT job_id FROM jobs WHERE state='RUNNING' ORDER BY rowid DESC")
        if job and job["job_id"] not in prefetched_jobs:
            prefetched_jobs.add(job["job_id"])
            if not prefetched:
                return {"rows": []}
        result = await original_recall(*args, **kwargs)
        scores.append([row["memory_id"] for row in result["rows"]])
        return result

    core.memory_context.recall_async = observed_recall
    number = 0

    def response(request):
        nonlocal number
        number += 1
        payload = json.loads(request.content)
        if respond:
            return respond(request, payload, number)
        if payload["messages"][-1]["role"] == "tool":
            found = "青竹七号" in payload["messages"][-1]["content"]
            content = "青竹七号。" if found else "未查到记录。"
            return httpx.Response(200, json=answer(content))
        return httpx.Response(200, json=answer(calls=[call(
            f"recall-{number}", "memory_recall", {"query": "本次临时项目的验收代号"},
        )]))

    options = combined_options() if options is None else options
    turns = options.get("max_history_turns", 0)
    try:
        async with native_continuity(
            core, tmp_path, turns=turns,
            byte_limit=options.get("max_history_utf8_bytes", 0),
            texts=queries or ["本次临时项目的验收代号是什么？"] * 5,
            core_options={"memory_enabled": True, **(core_options or {})},
            soft_limit=type(core.settings).model_fields["context_soft_tokens"].default,
            paid_options={"per_request_calls": 2, **(paid or {})},
            purposes=["chat", "memory", *(["history"] if turns else [])],
            respond=response,
        ) as (active, client, calls, sends, capture, _):
            yield active, client, capture, active.pump, calls, sends, scores, memory_id, outsider
    finally:
        core.memory_context.recall_async = original_recall


@pytest.mark.parametrize("prefetched", [False, True], ids=["empty-prefetch", "pinned-prefetch"])
def test_same_job_review_and_chat_use_authenticated_sent_history_and_native_fact(
    core, tmp_path, prefetched,
):
    async def scenario():
        async with native_combined(
            core,
            tmp_path,
            options=combined_options(),
            core_options={"max_recent_messages": 4},
            prefetched=prefetched,
            queries=[
                "本次临时项目在城西旧书店碰头，验收代号仍用已确认的那个。",
                "刚才这个项目在哪里碰头，长期确认的验收代号又是什么？",
            ],
        ) as (_, _, capture, send, calls, sends, scores, memory_id, _):
            await capture()
            assert await core.run_once()
            assert await send()
            await capture()
            assert await core.run_once()
            assert await send()
            assert len(calls) == 4 and len(sends) == 2
            assert scores == [[memory_id]] * (4 if prefetched else 2)
            assert any(m["role"] == "user" and "城西旧书店" in m["content"]
                       for m in calls[2]["messages"])
            chat = calls[3]["messages"]
            assert "城西旧书店" in encode(chat)
            assert "青竹七号" in encode(chat)
            assert chat[-3]["content"] == "刚才这个项目在哪里碰头，长期确认的验收代号又是什么？"
            layout = [m for m in chat if not (
                m["role"] == "user" and m["content"].startswith("你记得的事"))]
            assert [m["role"] for m in layout] == [
                "system",
                "user",
                "assistant",
                "user",
                "assistant",
                "tool",
            ]
            stages = core.store.rows("SELECT job_id,scenario,state FROM model_usage ORDER BY rowid")
            assert [r["scenario"] for r in stages] == ["chat"] * 4
            assert all(r["state"] == "SETTLED" for r in stages)
            assert stages[0]["job_id"] == stages[1]["job_id"]
            assert stages[2]["job_id"] == stages[3]["job_id"]
            frozen = core.store.rows(
                "SELECT value FROM runtime_meta WHERE key LIKE ? OR key LIKE ?",
                (live_text.MEMORY + "%", live_text.CONTEXT + "%"),
            )
            assert "城西旧书店" not in encode(frozen) and "青竹七号" not in encode(frozen)
            assert "城西旧书店" not in encode(history_snapshot(core))

    run(scenario())


@pytest.mark.parametrize("delta,kept", [(-1, False), (0, True)])
def test_utf8_history_boundary_crops_a_whole_pair_before_review(core, tmp_path, delta, kept):
    texts = ["本次临时项目在城西旧书店碰头。", "这个项目的验收代号是什么？"]
    limit = len((texts[0] + "青竹七号。").encode("utf-8")) + delta

    async def scenario():
        async with native_combined(
            core,
            tmp_path,
            queries=texts,
            options=combined_options(max_history_utf8_bytes=limit),
            core_options={"max_recent_messages": 4},
        ) as (_, _, capture, send, calls, _, _, _, _):
            await capture()
            assert await core.run_once()
            assert await send()
            await capture()
            assert await core.run_once()
            assert len(history_snapshot(core)) == int(kept)
            chat = calls[3]["messages"]
            assert sum(m["role"] == "assistant" for m in chat) == 1 + int(kept)
            assert ("城西旧书店" in encode(chat)) == kept
            assert chat[-3]["content"] == texts[-1]

    run(scenario())


def test_empty_scoped_search_combines_history_with_native_recall_per_input(core, tmp_path):
    async def scenario():
        async with native_combined(
            core,
            tmp_path,
            options=combined_options(),
            core_options={"max_recent_messages": 4},
        ) as (_, _, capture, send, calls, sends, _, memory_id, _):
            core.memory.decide(memory_id, "forget", core.clock())
            for _ in range(2):
                await capture()
                assert await core.run_once()
                assert await send()
            assert len(calls) == 4 and len(sends) == 2
            assert "NO_RECORD" in calls[-1]["messages"][-1]["content"]
            assert [m["role"] for m in calls[-1]["messages"]] == [
                "system",
                "user",
                "assistant",
                "user",
                "assistant",
                "tool",
            ]
            assert [r["scenario"] for r in core.store.rows("SELECT scenario FROM model_usage")] == [
                "chat",
                "chat",
                "chat",
                "chat",
            ]

    run(scenario())


@pytest.mark.parametrize("erased", ["history", "corrected_fact_family"])
def test_latest_ledger_replay_of_old_combined_backup_cannot_revive_sources(core, tmp_path, erased):
    from companion.core import Core

    async def scenario():
        async with native_combined(
            core,
            tmp_path,
            options=combined_options(),
            core_options={"max_recent_messages": 4},
        ) as (window, client, capture, send, calls, sends, _, memory_id, outsider):
            for _ in range(2):
                await capture()
                assert await core.run_once()
                assert await send()
            source = (
                core.store.one("SELECT origin_event_id FROM outbox ORDER BY rowid LIMIT 1")[
                    "origin_event_id"
                ]
                if erased == "history"
                else "evt_test_900"
            )
            key = Fernet.generate_key()
            archive, destination = tmp_path / "before.enc", tmp_path / "restored.db"
            backup(core.settings.database, archive, key)
            await window.pause()
            if erased == "history":
                with core.store.transaction():
                    core.ledger.expire_events(core.store, [source], core.clock())
            else:
                changed = core.memory.decide(
                    memory_id, "correct", core.clock(), "新的临时项目代号。"
                )
                core.memory.decide(changed["memory_id"], "forget", core.clock())
            restore(archive, destination, key, core.settings.erasure_ledger)
            with Core(
                core.settings.model_copy(update={"database": str(destination)}),
                core.model,
                core.clock,
            ) as recovered:
                assert (
                    recovered.store.one("SELECT redacted FROM events WHERE event_id=?", (source,))[
                        "redacted"
                    ]
                    == 1
                )
                assert all(
                    row["content_json"] == "{}"
                    for row in recovered.store.rows("SELECT content_json FROM outbox")
                )
                if erased == "corrected_fact_family":
                    assert recovered.store.one(
                        "SELECT content,status FROM memories WHERE memory_id=?", (memory_id,)
                    ) == {
                        "content": "",
                        "status": "FORGOTTEN",
                    }
                    assert not recovered.store.one(
                        "SELECT 1 FROM memory_vectors WHERE memory_id=?", (memory_id,)
                    )
                    assert not recovered.store.one(
                        "SELECT 1 FROM memory_revisions WHERE memory_id=?", (memory_id,)
                    )
                assert recovered.store.one(
                    "SELECT content,status FROM memories WHERE memory_id=?", (outsider,)
                ) == {
                    "content": "未批准的另一个项目代号是紫云三号。",
                    "status": "CONFIRMED",
                }
                assert recovered.store.one("SELECT COUNT(*) n FROM model_admissions")["n"] == 4
                assert all(
                    r["state"] == "SETTLED"
                    for r in recovered.store.rows("SELECT state FROM model_usage")
                )
                assert recovered.store.one(
                    "SELECT 1 FROM runtime_meta WHERE key='model_budget_restore_required'"
                )
                assert not recovered.status()["live_text_window"]["approved"]
                assert not await recovered.run_once()
            assert len(calls) == 4 and len(sends) == 2

    run(scenario())


def mutate(core, kind):
    event_id = core.store.one("SELECT origin_event_id FROM outbox ORDER BY rowid LIMIT 1")[
        "origin_event_id"
    ]
    if kind == "fact":
        core.store.execute("UPDATE memories SET content='漂移事实' WHERE fact_key IS NOT NULL")
    elif kind == "expired_evidence":
        core.store.execute(
            "UPDATE events SET ingested_at=? WHERE event_id='evt_test_900'",
            (after(core.clock(), -86400 * 3650),),
        )
    elif kind == "history":
        body = json.loads(
            core.store.one("SELECT body_json FROM events WHERE event_id=?", (event_id,))[
                "body_json"
            ]
        )
        body["text"] = "历史原件已漂移"
        core.store.execute(
            "UPDATE events SET body_json=? WHERE event_id=?", (encode(body), event_id)
        )
    elif kind == "PENDING":
        core.store.execute("UPDATE outbox SET state='PENDING' WHERE origin_event_id=?", (event_id,))
    elif kind == "reply":
        core.store.execute(
            "UPDATE outbox SET content_json=? WHERE origin_event_id=?",
            (encode({"text": "漂移回复"}), event_id),
        )
    elif kind == "epoch":
        advance_epoch(core.store)
    elif kind in {"history_approval", "memory_approval"}:
        view = owner_authority.current(core.store, core.settings, core.clock)
        purpose = kind.removesuffix("_approval")
        value = view.policy.model_dump(mode="json")
        value["purposes"].remove(purpose)
        value["history" if purpose == "history" else "memory_scope"] = (
            {"scope": "none"} if purpose == "history" else "none")
        policy = type(view.policy).model_validate(value)
        stopped = owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id=f"stop-before-withdraw-{purpose}", authority=view.authority.as_dict()))
        return policy, stopped["authority"]
    else:
        row = core.store.one(
            "SELECT * FROM runtime_meta WHERE key=?", (owner_input.INPUT + event_id,)
        )
        value = json.loads(row["value"])
        if kind == "signature":
            value["capture"]["signature"] = "0" * 128
        elif kind == "window":
            value["authority"]["policy_sha256"] = "a" * 64
        else:
            raise AssertionError(kind)
        core.store.execute(
            "UPDATE runtime_meta SET value=? WHERE key=?", (encode(value), row["key"])
        )


@pytest.mark.parametrize("stage", ["memory_tool", "answer"])
@pytest.mark.parametrize(
    "kind",
    [
        "history",
        "fact",
        "expired_evidence",
        "signature",
        "PENDING",
        "window",
        "epoch",
        "history_approval",
        "memory_approval",
    ],
)
def test_both_source_scopes_recheck_before_next_model_io(core, tmp_path, monkeypatch, stage, kind):
    async def scenario():
        async with native_combined(
            core,
            tmp_path,
            options=combined_options(),
            core_options={"max_recent_messages": 4},
        ) as (_, _, capture, send, calls, sends, _, _, _):
            await capture()
            assert await core.run_once()
            assert await send()
            await capture()

            original_recall = core.memory_context.recall_async
            mutations = []
            pending_revision = []

            def change():
                revision = mutate(core, kind)
                if revision is not None:
                    pending_revision.append(revision)
                mutations.append(kind)

            async def recalled(*args, **kwargs):
                result = await original_recall(*args, **kwargs)
                if result["rows"] and not mutations:
                    change()
                return result

            async def hook(request):
                if json.loads(request.content)["messages"][-1]["role"] == "tool":
                    change()

            if stage == "memory_tool":
                monkeypatch.setattr(core.memory_context, "recall_async", recalled)
            else:
                core.model.client.event_hooks["request"].append(hook)
            assert await core.run_once()
            assert mutations == [kind]
            if pending_revision:
                # STOP fences the current attempt; revise only after its original usage settles.
                assert all(row["state"] == "SETTLED" for row in core.store.rows(
                    "SELECT state FROM model_usage"))
                policy, stopped = pending_revision[0]
                activate(core, policy, revision=stopped["revision"], operation=f"withdraw-{kind}")
                purpose = kind.removesuffix("_approval")
                assert purpose not in owner_authority.current(
                    core.store, core.settings, core.clock).policy.purposes
                with pytest.raises(CoreError, match="OWNER_AUTHORITY_PURPOSE_REQUIRED"):
                    owner_authority.current(core.store, core.settings, core.clock, purpose=purpose)
            # Epoch-only movement preserves exact source proofs; changed original
            # sources or withdrawn authority block the next answer request.
            assert len(calls) == (4 if kind == "epoch" else 3)
            assert len(sends) == 1
            assert core.store.one("SELECT COUNT(*) n FROM outbox")["n"] == 1
            job = core.store.one("SELECT state FROM jobs ORDER BY rowid DESC LIMIT 1")
            expected = (
                "RETRY_WAIT" if kind == "epoch" or (
                    stage == "memory_tool" and kind in {"fact", "expired_evidence"})
                else "CANCELLED")
            assert job["state"] == expected, job
            assert not await core.run_once()

    run(scenario())


@pytest.mark.parametrize("stage", ["memory_tool", "answer"])
def test_history_drift_after_received_response_keeps_cost_but_never_enqueues(core, tmp_path, stage):
    def respond(request, payload, number):
        reviewing = payload["messages"][-1]["role"] != "tool"
        if number > 2 and reviewing == (stage == "memory_tool"):
            mutate(core, "history")
        return httpx.Response(
            200,
            json=answer(calls=[call(f"recall-{number}", "memory_recall",
                                   {"query": "本次临时项目的验收代号"})])
                if reviewing else answer("合成回复"),
        )

    async def scenario():
        async with native_combined(
            core,
            tmp_path,
            options=combined_options(),
            core_options={"max_recent_messages": 4},
            respond=respond,
        ) as (_, _, capture, send, calls, sends, _, _, _):
            await capture()
            assert await core.run_once()
            assert await send()
            await capture()
            assert await core.run_once()
            assert len(calls) == (3 if stage == "memory_tool" else 4) and len(sends) == 1
            assert core.store.one("SELECT COUNT(*) n FROM outbox")["n"] == 1
            usage = core.store.rows("SELECT state,billed_micro_usd FROM model_usage")
            assert all(r["state"] == "SETTLED" and r["billed_micro_usd"] > 0 for r in usage)

    run(scenario())


@pytest.mark.parametrize("kind", ["history", "fact", "epoch"])
def test_delivery_check_refuses_either_changed_scope_without_sending(core, tmp_path, kind):
    async def scenario():
        async with native_combined(
            core,
            tmp_path,
            options=combined_options(),
            core_options={"max_recent_messages": 4},
        ) as (window, client, capture, send, calls, sends, _, _, _):
            await capture()
            assert await core.run_once()
            assert await send()
            await capture()
            assert await core.run_once()
            leased = await client.post("/internal/v1/deliveries/lease", json={
                "worker_id": "native-combined-check", "authority": window.runtime.authority})
            assert leased.status_code == 200, leased.text
            row = leased.json()["items"][0]
            begun = await client.post(f"/internal/v1/deliveries/{row['message_id']}/begin-send",
                                      json={"lease_token": row["lease_token"]})
            assert begun.status_code == 200, begun.text
            check = {"authority": window.runtime.authority, "event_id": row["origin_event_id"],
                "message_id": row["message_id"], "content_sha256": paid_budget.sha(row["content"]),
                "reply_revision": row["reply_revision"], "lease_token": row["lease_token"]}
            path = "/internal/v1/owner-authority/send-check"
            assert (await client.post(path, json=check)).json()[
                "approved"
            ]
            mutate(core, kind)
            response = await client.post(path, json=check)
            assert response.status_code >= 400
            assert len(calls) == 4 and len(sends) == 1
            assert (
                core.store.one("SELECT state FROM outbox WHERE message_id=?", (row["message_id"],))[
                    "state"
                ]
                == "SENDING"
            )

    run(scenario())


def test_soft_capacity_keeps_current_input_and_native_fact_without_rewriting_fact(
    core, tmp_path
):
    texts = ["本次临时项目的验收代号改说白帆九号。", "那这个临时项目的代号还是原来的？"]

    async def scenario():
        async with native_combined(
            core,
            tmp_path,
            queries=texts,
            options=combined_options(),
            core_options={"max_recent_messages": 4, "context_soft_tokens": 1},
        ) as (_, _, capture, send, calls, _, _, memory_id, _):
            await capture()
            assert await core.run_once()
            assert await send()
            await capture()
            assert await core.run_once()
            assert history_snapshot(core) == []
            snapshots = json.loads(core.store.one(
                "SELECT snapshots_json FROM agent_conversations ORDER BY rowid DESC LIMIT 1"
            )["snapshots_json"])
            plan = next(item["metadata"]["core_context"] for item in snapshots
                        if item["kind"] == "context_plan")
            assert not any(group["kind"] == "history" for group in plan["groups"])
            assert any(item["kind"] == "history" and item["reason"] == "SOFT_LIMIT"
                       for item in plan["assembly_dropped"])
            assert "白帆九号" not in encode(calls[2:])
            messages = calls[-1]["messages"]
            assert "青竹七号" in messages[-1]["content"]
            assert json.loads(messages[-1]["content"])["state"] == "FOUND"
            assert messages[-3] == {"role": "user", "content": texts[-1]}
            assert core.store.one(
                "SELECT status,content FROM memories WHERE memory_id=?", (memory_id,)
            ) == {
                "status": "CONFIRMED",
                "content": "本次临时项目的验收代号是青竹七号。",
            }

    run(scenario())


def test_unknown_review_preserves_budget_and_never_falls_back_to_chat(core, tmp_path):
    def unknown(request, payload, number):
        raise httpx.ReadTimeout("SYNTHETIC_B1_UNKNOWN", request=request)

    async def scenario():
        async with native_combined(
            core,
            tmp_path,
            options=combined_options(),
            core_options={"max_recent_messages": 4},
            respond=unknown,
        ) as (_, _, capture, _, calls, sends, _, _, _):
            await capture()
            assert await core.run_once()
            assert len(calls) == 1 and sends == []
            assert core.store.one("SELECT scenario,state FROM model_usage") == {
                "scenario": "chat",
                "state": "UNKNOWN",
            }
            assert core.store.rows("SELECT * FROM outbox") == []
            assert not await core.run_once()

    run(scenario())


def test_five_total_attempt_cap_survives_combined_stages_and_fresh_core(core, tmp_path):
    from companion.core import Core

    async def scenario():
        async with native_combined(
            core,
            tmp_path,
            options=combined_options(),
            core_options={"max_recent_messages": 4},
            paid={"window_calls": 5},
        ) as (_, _, capture, send, calls, sends, _, _, _):
            for _ in range(2):
                await capture()
                assert await core.run_once()
                assert await send()
            await capture()
            assert await core.run_once()
            assert len(calls) == 5 and len(sends) == 2
            assert core.store.one("SELECT COUNT(*) n FROM model_admissions")["n"] == 5
            restarted = Core(core.settings, core.model, core.clock)
            try:
                assert not await restarted.run_once()
                assert restarted.status()["live_text_window"]["approved"] is False
                assert restarted.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 5
                assert restarted.store.one("SELECT COUNT(*) n FROM outbox")["n"] == 3
                failed = restarted.store.one("SELECT event_id FROM jobs WHERE state='FAILED'")
                assert_failure_notice(restarted, failed["event_id"])
            finally:
                restarted.store.close()
            assert len(calls) == 5

    run(scenario())


def test_combined_budget_too_small_rejects_before_model_http(core, tmp_path):
    async def scenario():
        async with native_combined(
            core,
            tmp_path,
            options=combined_options(),
            core_options={"max_recent_messages": 4},
            paid={"max_input_tokens": 1},
        ) as (_, _, capture, _, calls, sends, _, _, _):
            await capture()
            assert await core.run_once()
            assert calls == sends == []
            assert not core.store.one("SELECT 1 FROM model_admissions")

    run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"history_approval_sha256": None},
        {"memory_approval_sha256": None},
        {"history_scope": "none", "max_history_turns": 0, "max_history_utf8_bytes": 0},
    ],
)
def test_scopes_cannot_inherit_or_reassign_each_others_approval(core, tmp_path, change):
    memory_id = fact(core, 900, "本次临时项目的验收代号是青竹七号。")
    _, historical = live_fixture(core, tmp_path,
        bridge_options={"capture_signing_key": "c" * 64},
        memory_scope="approved_facts", memory_approval_sha256="f" * 64,
        memory_facts=[live_text.memory_fact(core.store, core.settings, core.clock, memory_id)],
        **combined_options())
    value = core.settings.live_text_window.model_dump(mode="json")
    with pytest.raises(ValidationError):
        LiveTextWindowApproval.model_validate({**value, **change})
    plan = deepcopy(historical.value)
    plan["live_text"].update(change)
    with pytest.raises(ValueError, match="HISTORY_APPROVAL|MEMORY_SCOPE"):
        WindowPlan(plan, historical.config, {})
    assert not core.store.one("SELECT 1 FROM model_usage")


def test_old_history_only_plan_without_new_field_remains_byte_exact_readable(core, tmp_path):
    cfg, plan = live_fixture(core, tmp_path,
        bridge_options={"capture_signing_key": "c" * 64}, **combined_options())
    value = deepcopy(plan.value)
    del value["live_text"]["history_approval_sha256"]
    inventory = {key: value[key] for key in (
        "identity_sha256", "protocol_epoch", "config_sha256", "config_version", "image_ids")}
    raw = encode(value)
    historical = WindowPlan(value, cfg, inventory)
    assert historical.sha256 == digest(json.loads(raw))
    assert "history_approval_sha256" not in historical.value["live_text"]
    archived = stored_plan(raw, cfg)
    assert archived.sha256 == historical.sha256 and encode(archived.value) == raw
    assert not core.store.one("SELECT 1 FROM model_usage")


@pytest.mark.parametrize("history", [False, True])
def test_fsynced_forget_with_rolled_back_db_blocks_the_next_model_http(
    core,
    tmp_path,
    monkeypatch,
    history,
):
    async def scenario():
        async with native_combined(
            core,
            tmp_path,
            options=combined_options() if history else {},
            core_options={"max_recent_messages": 4 if history else 0},
        ) as (_, _, capture, send, calls, sends, _, memory_id, _):
            await capture()
            assert await core.run_once()
            assert await send()
            await capture()

            async def forget_then_crash(_request):
                def crash_apply(*_):
                    raise RuntimeError("SYNTHETIC_AFTER_FSYNC_BEFORE_DB_COMMIT")

                with monkeypatch.context() as patch:
                    patch.setattr(core.ledger, "apply", crash_apply)
                    with pytest.raises(RuntimeError):
                        with core.store.transaction():
                            core.ledger.append(core.store, memory_id, core.clock())
                assert (
                    core.store.one("SELECT status FROM memories WHERE memory_id=?", (memory_id,))[
                        "status"
                    ]
                    == "CONFIRMED"
                )
                assert not core.store.one("SELECT 1 FROM privacy_erasure_ledger")

            core.model.client.event_hooks["request"].append(forget_then_crash)
            assert await core.run_once()
            assert len(calls) == 2 and len(sends) == 1
            assert core.store.one("SELECT COUNT(*) n FROM outbox")["n"] == 1

    run(scenario())


def test_foreign_window_is_excluded_before_reading_its_stage_metadata(core, tmp_path):
    async def scenario():
        async with native_combined(
            core,
            tmp_path,
            options=combined_options(),
            core_options={"max_recent_messages": 4},
        ) as (_, _, capture, send, calls, _, _, _, _):
            await capture()
            assert await core.run_once()
            assert await send()
            await capture()
            mutate(core, "window")
            # The rejected history source cannot authorize reading its old snapshots.
            core.store.execute(
                "UPDATE agent_conversations SET snapshots_json='{}'"
            )
            assert await core.run_once()
            assert len(calls) == 4
            assert history_snapshot(core) == []
            assert [m["role"] for m in calls[-1]["messages"]] == [
                "system", "user", "assistant", "tool"]

    run(scenario())
