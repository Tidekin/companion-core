"""Window-scoped continuity over actual loopback capture; no QQ or provider I/O."""

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from cryptography.fernet import Fernet

from companion import owner_authority, owner_input, paid_budget
from companion.backup import backup, restore
from companion.config import LiveTextWindowApproval
from companion.contracts import Candidate
from companion.core import Core
from companion.privacy import advance_epoch
from companion.store import encode, uid
from tests.conftest import run
from tests.native_continuous_support import frame, host
from tests.support.failure_notice import assert_failure_notice
from tests.test_dialogue_context import sent_history
from tests.test_live_text_window import live_fixture


@asynccontextmanager
async def native_continuity(core, tmp_path, *, turns=4, byte_limit=16000, texts=None,
                            soft_limit=3200, core_options=None, paid_options=None,
                            grant_options=None, respond=None, purposes=None):
    texts = texts or ["选了茉莉绿茶", "现在说的这个呢？", "刚才聊到哪了？", "还是那个吗？", "继续"]
    number, captures = 0, 0
    calls = []

    async def source(ws):
        nonlocal number
        number += 1
        # Wire event time and app.receive use the wall clock; the independently
        # advancing Core clock belongs to business deadlines and history tests.
        await ws.send(frame(number, text=texts[number - 1]))
        await ws.wait_closed()

    def provider_reply(request):
        calls.append(json.loads(request.content))
        if respond:
            return respond(request)
        return httpx.Response(200, json={"choices": [{"message": {
            "content": f"合成回答{len(calls)}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    from datetime import UTC, datetime, timedelta

    selected = purposes or (["chat", "history"] if turns else ["chat"])
    history = {"scope": "owner_private", "not_before": datetime.now(UTC) - timedelta(days=1),
               "max_turns": turns, "max_utf8_bytes": byte_limit} if turns else {}
    async with host(core, tmp_path, source, model_reply=provider_reply,
        purposes=selected, core_options={"max_recent_messages": turns * 2,
            "context_soft_tokens": soft_limit, **(core_options or {})},
        paid_options={"daily_calls": 100, "window_calls": 100, **(paid_options or {})},
        grant_options={"history": history, **(grant_options or {})},
    ) as (active, _, sends, _):
        async def capture_input():
            nonlocal captures
            captures += 1
            await active.capture(captures)
            await active.flush()

        async def finish():
            assert await core.run_once("native-continuity")
            return await active.pump()

        yield active, active.client, calls, sends, capture_input, finish


def history_snapshot(core):
    row = core.store.one(
        "SELECT snapshots_json FROM agent_conversations ORDER BY rowid DESC LIMIT 1")
    return next(item["proof"] for item in json.loads(row["snapshots_json"])
                if item["kind"] == "history")


@pytest.mark.parametrize("microsecond", [995999, 999792])
def test_source_timestamp_uses_receive_wall_clock_across_second_boundary(
    core, tmp_path, monkeypatch, microsecond,
):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from companion_onebot_bridge import app
    from tests import native_continuous_support

    received = datetime.now(UTC).replace(microsecond=microsecond)
    wall_clock = SimpleNamespace(now=lambda _: received)
    monkeypatch.setattr(native_continuous_support, "datetime", wall_clock)
    monkeypatch.setattr(app, "datetime", wall_clock)

    async def scenario():
        async with native_continuity(core, tmp_path) as (
            active, _, calls, sends, capture_input, finish,
        ):
            # Reproduce the CI boundary: the independently advanced business clock
            # has crossed the next second while the receiver wall clock has not.
            core.clock.now = received.replace(microsecond=0) + timedelta(seconds=1)
            await capture_input()
            receipt = json.loads(active.db.execute(
                "SELECT capture_json FROM window_inputs").fetchone()[0])
            assert receipt["occurred_at"] == received.replace(microsecond=0).isoformat()
            assert receipt["received_at"] == received.isoformat()
            assert await finish()
            assert active.attempts() == {"ACK": 1}
            assert len(calls) == len(sends) == 1

    run(scenario())


def test_five_authenticated_turns_use_only_approved_window_and_keep_one_attempt(core, tmp_path):
    sent_history(core, 999, "窗口前的旧输入", "窗口前的旧回复")

    async def scenario():
        async with native_continuity(core, tmp_path) as (
            window, client, calls, sends, capture_input, finish,
        ):
            for number in range(1, 6):
                await capture_input()
                assert await finish()
                prompt = calls[-1]["messages"]
                assert len(prompt) == number * 2
                assert [m["role"] for m in prompt] == ["system"] + [
                    role for _ in range(number - 1) for role in ("user", "assistant")
                ] + ["user"]
                assert "窗口前" not in encode(prompt)
                if number > 1:
                    assert prompt[1]["content"] == "选了茉莉绿茶"
                    assert prompt[2]["content"] == "合成回答1"
                assert window.attempts() == {"ACK": number}
                assert core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == number
            assert len(calls) == len(sends) == 5
            assert not await core.run_once()
            assert window.runtime.row()["state"] == "ACTIVE"
            assert core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 5
            assert core.store.one("SELECT COUNT(*) n FROM outbox")["n"] == 6  # plus old history
            assert all(row[0] == "ACK" for row in window.db.execute(
                "SELECT state FROM window_attempts"))
            assert (await client.get("/internal/v1/ops/status")).status_code == 200

    run(scenario())


@pytest.mark.parametrize("turns,byte_limit,kept", [(0, 0, 0), (1, 16000, 1), (4, 1, 0)])
def test_scope_and_utf8_bound_drop_only_complete_turns(core, tmp_path, turns, byte_limit, kept):
    async def scenario():
        async with native_continuity(core, tmp_path, turns=turns, byte_limit=byte_limit) as (
            _, _, calls, _, capture_input, finish,
        ):
            for _ in range(3):
                await capture_input()
                await finish()
            assert len(calls[-1]["messages"]) == 2 + kept * 2
            assert calls[-1]["messages"][-1] == {"role": "user", "content": "刚才聊到哪了？"}

    run(scenario())


def mutate_source(core, kind):
    source = core.store.one("SELECT * FROM events ORDER BY rowid LIMIT 1")
    event_id = source["event_id"]
    if kind == "privacy":
        advance_epoch(core.store)
    elif kind in {"PENDING", "FAILED", "DELIVERY_UNCERTAIN"}:
        core.store.execute("UPDATE outbox SET state=? WHERE origin_event_id=?", (kind, event_id))
    elif kind == "UNKNOWN":
        core.store.execute("UPDATE model_usage SET state='UNKNOWN'")
    elif kind == "redacted":
        core.store.execute("UPDATE events SET redacted=1 WHERE event_id=?", (event_id,))
    elif kind in {"SUPERSEDED", "FORGOTTEN"}:
        text = json.loads(source["body_json"])["text"]
        memory_id = core.memory.add(Candidate(
            subject="user", kind="preference", fact_key="饮料", content=text,
            evidence_event_id=event_id, evidence_quote=text,
        ), core.clock(), confirmed=True)
        assert memory_id is not None
        core.store.execute("UPDATE memories SET status=? WHERE memory_id=?", (kind, memory_id))
    elif kind == "reply_text":
        core.store.execute("UPDATE outbox SET content_json=?", (encode({"text": "被替换的回答"}),))
    elif kind == "input_text":
        body = json.loads(source["body_json"])
        body["text"] = "被替换的输入"
        core.store.execute("UPDATE events SET body_json=? WHERE event_id=?",
                           (encode(body), event_id))
    elif kind == "actor":
        core.store.execute("UPDATE events SET actor_id='companion' WHERE event_id=?", (event_id,))
    elif kind == "source":
        core.store.execute("UPDATE events SET source_event_id='other-identity' WHERE event_id=?",
                           (event_id,))
    elif kind == "conversation":
        conversation = core.store.one("SELECT * FROM conversations LIMIT 1")
        conversation["conversation_id"] = uid("conv")
        conversation["platform_session_key"] = "synthetic-different-conversation"
        core.store.insert("conversations", **conversation)
        core.store.execute("UPDATE events SET conversation_id=? WHERE event_id=?",
                           (conversation["conversation_id"], event_id))
    elif kind == "action":
        core.store.execute("UPDATE outbox SET action_type='qq.private.proactive'")
    else:
        key = owner_input.INPUT + event_id
        receipt = json.loads(core.store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))[
            "value"
        ])
        if kind == "unsigned":
            receipt["capture"]["signature"] = None
        elif kind == "no_capture":
            receipt["capture"] = None
        elif kind == "capture_identity":
            receipt["capture"]["identity_sha256"] = "a" * 64
        elif kind == "window":
            receipt["authority"]["policy_sha256"] = "a" * 64
        elif kind == "receipt_epoch":
            receipt["privacy_epoch"] += 1
        core.store.execute("UPDATE runtime_meta SET value=? WHERE key=?", (encode(receipt), key))


@pytest.mark.parametrize("kind", [
    "PENDING", "FAILED", "DELIVERY_UNCERTAIN", "UNKNOWN", "redacted",
    "SUPERSEDED", "FORGOTTEN", "input_text", "reply_text", "actor", "source",
    "unsigned", "no_capture", "window", "receipt_epoch", "conversation", "action",
    "capture_identity",
])
@pytest.mark.parametrize("repeat", range(12))
def test_selected_history_changed_in_final_preflight_causes_zero_new_io(
    core, tmp_path, kind, repeat, monkeypatch,
):
    from datetime import UTC, datetime

    from companion.contracts import CoreError

    reasons = []
    original_batch = owner_input.history_batch

    def batch(*args, **kwargs):
        view, check, done = original_batch(*args, **kwargs)

        def logged(event_id):
            try:
                return check(event_id)
            except CoreError as error:
                reasons.append((event_id[:12], str(error), core.clock()))
                raise
        return view, logged, done
    monkeypatch.setattr(owner_input, "history_batch", batch)
    margins = []
    original_sources = owner_input._history_sources

    def sources(store, settings, clock, job, **kwargs):
        now = datetime.fromisoformat(clock())
        for row in store.rows("SELECT sent_at FROM outbox WHERE sent_at IS NOT NULL"):
            margins.append(("sent", round((now - datetime.fromisoformat(row["sent_at"]))
                                          .total_seconds(), 4)))
        for row in store.rows("SELECT value FROM runtime_meta WHERE key LIKE ?",
                              (owner_input.INPUT + "%",)):
            capture = json.loads(row["value"]).get("capture") or {}
            if capture.get("received_at"):
                margins.append(("recv", round((now - datetime.fromisoformat(
                    capture["received_at"])).total_seconds(), 4)))
        margins.append(("core-wall", round((now - datetime.now(UTC)).total_seconds(), 4)))
        return original_sources(store, settings, clock, job, **kwargs)
    monkeypatch.setattr(owner_input, "_history_sources", sources)

    def snapshot(tag):
        import time
        return {"tag": tag, "core": core.clock(), "wall": datetime.now(UTC).isoformat(),
                "mono": time.monotonic(), "probe": dict(getattr(core.clock, "probe", {})),
                "outbox": core.store.rows("SELECT state,sent_at,created_at,updated_at,"
                                          "last_error_code,attempt_count FROM outbox"),
                "jobs": core.store.rows("SELECT kind,state,last_error_code,attempts,"
                                        "event_id FROM jobs"),
                "captures": [
                    {k: (json.loads(r["value"]).get("capture") or {}).get(k)
                     for k in ("occurred_at", "received_at")}
                    for r in core.store.rows("SELECT value FROM runtime_meta WHERE key LIKE ?",
                                             (owner_input.INPUT + "%",))],
                "reasons": list(reasons)}

    async def scenario():
        async with native_continuity(core, tmp_path) as (
            _, _, calls, sends, capture_input, finish,
        ):
            diag = []
            await capture_input()
            diag.append(snapshot("cap1"))
            delivered = await finish()
            diag.append(snapshot(f"fin1 pump={delivered}"))
            await capture_input()
            diag.append(snapshot("cap2"))
            changed = []

            async def mutate(_request):
                mutate_source(core, kind)
                changed.append(kind)

            core.model.client.event_hooks["request"].append(mutate)
            assert await core.run_once()
            diag.append(snapshot("run2"))
            diag.append({"prompts": [[m["content"][:20] for m in c["messages"]] for c in calls]})
            import warnings
            probe = getattr(core.clock, "probe", {})
            start = probe.get("tick_start")
            clocks = {
                "gap": round(start[0] - probe["anchor"][0], 4),
                "tick_start_core_minus_wall": round((datetime.fromisoformat(start[2])
                    - datetime.fromisoformat(start[1])).total_seconds(), 4),
                "max_delta": round(probe["max_delta"], 4),
            } if start else {}
            warnings.warn("MARGINS " + json.dumps(margins[:4]) + " CLOCKS "
                          + json.dumps(clocks), stacklevel=1)
            assert len(calls) == len(sends) == 1, "PROBE " + json.dumps(diag, ensure_ascii=False,
                                                                         default=str)
            assert changed == [kind]
            assert core.store.one("SELECT COUNT(*) n FROM outbox")["n"] == 1
            job = core.store.one(
                "SELECT state,last_error_code FROM jobs ORDER BY rowid DESC LIMIT 1",
            )
            assert job["state"] in {"CANCELLED", "FAILED"}, job
            assert len(history_snapshot(core)) == 1

    run(scenario())


def test_unrelated_privacy_epoch_keeps_original_history_source_and_scope(core, tmp_path):
    async def scenario():
        async with native_continuity(core, tmp_path) as (
            _, _, calls, sends, capture_input, finish,
        ):
            await capture_input()
            await finish()
            await capture_input()
            selected = []

            async def advance_unrelated_epoch(_request):
                selected.extend(history_snapshot(core))
                advance_epoch(core.store)

            core.model.client.event_hooks["request"].append(advance_unrelated_epoch)
            assert await core.run_once()
            assert len(calls) == 2 and len(sends) == 1
            assert len(selected) == 1 and history_snapshot(core) == selected
            assert calls[-1]["messages"][1:3] == [
                {"role": "user", "content": "选了茉莉绿茶"},
                {"role": "assistant", "content": "合成回答1"},
            ]
            # Selected-source revalidation permits the request; final output custody
            # still fences the response across a privacy-generation change.
            assert core.store.one("SELECT COUNT(*) n FROM outbox")["n"] == 1
            assert all(row["state"] == "SETTLED" for row in core.store.rows(
                "SELECT state FROM model_usage"))
            job = core.store.one(
                "SELECT state,last_error_code FROM jobs ORDER BY rowid DESC LIMIT 1")
            assert job == {"state": "RETRY_WAIT", "last_error_code": "PRIVACY_CHANGED"}

    run(scenario())


@pytest.mark.parametrize("kind", [
    "PENDING", "FAILED", "redacted", "actor", "source",
    "unsigned", "no_capture", "window", "receipt_epoch", "conversation", "action",
    "capture_identity",
])
def test_ineligible_previous_source_is_absent_from_actual_prompt(core, tmp_path, kind):
    async def scenario():
        async with native_continuity(core, tmp_path) as (
            _, _, calls, sends, capture_input, finish,
        ):
            await capture_input()
            await finish()
            await capture_input()
            mutate_source(core, kind)
            assert await core.run_once()
            assert len(calls) == 2 and len(sends) == 1
            assert len(calls[-1]["messages"]) == 2
            assert calls[-1]["messages"][-1] == {"role": "user", "content": "现在说的这个呢？"}
            assert core.store.one("SELECT state FROM jobs ORDER BY rowid DESC LIMIT 1")[
                "state"
            ] == "SUCCEEDED"

    run(scenario())


@pytest.mark.parametrize("delta,kept", [(-1, False), (0, True)])
def test_history_utf8_byte_boundary_keeps_a_pair_or_neither(core, tmp_path, delta, kept):
    bound = len("选了茉莉绿茶合成回答1".encode()) + delta

    async def scenario():
        async with native_continuity(core, tmp_path, byte_limit=bound) as (
            _, _, calls, _, capture_input, finish,
        ):
            await capture_input()
            await finish()
            await capture_input()
            await finish()
            assert len(calls[-1]["messages"]) == (4 if kept else 2)

    run(scenario())


def test_sources_removed_by_soft_bound_are_not_frozen_or_reintroduced(core, tmp_path):
    current = "当前完整输入" * 80

    async def scenario():
        async with native_continuity(core, tmp_path, soft_limit=1000,
                                     texts=["旧内容" * 150, current]) as (
            _, _, calls, _, capture_input, finish,
        ):
            await capture_input()
            await finish()
            await capture_input()

            async def change_dropped_source(_request):
                mutate_source(core, "input_text")

            core.model.client.event_hooks["request"].append(change_dropped_source)
            assert await core.run_once()
            assert len(calls) == 2
            assert len(calls[-1]["messages"]) == 2
            assert calls[-1]["messages"][-1] == {"role": "user", "content": current}
            assert history_snapshot(core) == []

    run(scenario())


@pytest.mark.parametrize("changes", [
    {"history_scope": "none", "max_history_turns": 1},
    {"history_scope": "approved_window"},
    {"history_scope": "all_history", "max_history_turns": 1, "max_history_utf8_bytes": 500},
    {"history_scope": "approved_window", "max_history_turns": 16, "max_history_utf8_bytes": 500},
    {"history_scope": "approved_window", "max_history_turns": True, "max_history_utf8_bytes": 500},
    {"history_scope": "approved_window", "max_history_turns": 1, "max_history_utf8_bytes": 16001},
])
def test_history_scope_requires_explicit_finite_approval(core, tmp_path, changes):
    live_fixture(core, tmp_path)
    value = core.settings.live_text_window.model_dump()
    with pytest.raises(ValueError):
        LiveTextWindowApproval.model_validate({**value, **changes})


def test_core_restart_keeps_original_window_history_and_attempts(core, tmp_path):
    async def scenario():
        async with native_continuity(core, tmp_path) as (
            _, _, calls, sends, capture_input, finish,
        ):
            await capture_input()
            await finish()
            await capture_input()
            with Core(core.settings, core.model, core.clock) as restarted:
                assert await restarted.run_once()
                assert len(calls) == 2 and len(sends) == 1
                assert calls[-1]["messages"][1:3] == [
                    {"role": "user", "content": "选了茉莉绿茶"},
                    {"role": "assistant", "content": "合成回答1"},
                ]
                assert restarted.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 2
                assert not await restarted.run_once()
                assert len(calls) == 2

    run(scenario())


def test_erasure_and_old_database_replay_clear_history_generated_replies(core, tmp_path):
    async def scenario():
        async with native_continuity(core, tmp_path) as (
            _, _, calls, _, capture_input, finish,
        ):
            await capture_input()
            await finish()
            await capture_input()
            await finish()
            assert len(calls[-1]["messages"]) == 4
            source = core.store.one("SELECT event_id FROM events ORDER BY rowid LIMIT 1")[
                "event_id"
            ]
            key = Fernet.generate_key()
            snapshot, restored = tmp_path / "before-erasure.enc", tmp_path / "restored.db"
            backup(core.settings.database, snapshot, key)
            with core.store.transaction():
                core.ledger.expire_events(core.store, [source], core.clock())
            assert all(row["content_json"] == "{}" for row in core.store.rows(
                "SELECT content_json FROM outbox"
            ))
            assert owner_authority.status(core.store, core.settings,
                                          core.clock)["state"] == "ACTIVE"
            # New metadata stores only references/digests, never another plaintext copy.
            metadata = encode(core.store.rows("SELECT snapshots_json FROM agent_conversations"))
            assert "选了茉莉绿茶" not in metadata and "合成回答" not in metadata
            restore(snapshot, restored, key, core.settings.erasure_ledger)
            with Core(core.settings.model_copy(update={"database": str(restored)}),
                      core.model, core.clock) as reopened:
                assert reopened.store.one("SELECT redacted FROM events WHERE event_id=?",
                                          (source,))["redacted"] == 1
                assert all(row["content_json"] == "{}" for row in reopened.store.rows(
                    "SELECT content_json FROM outbox"
                ))
                assert owner_authority.status(reopened.store, reopened.settings,
                                              reopened.clock)["state"] != "ACTIVE"
                assert reopened.store.one("SELECT COUNT(*) n FROM model_admissions")["n"] == 2
                assert not await reopened.run_once()
                assert len(calls) == 2

    run(scenario())


@pytest.mark.parametrize("change_reply", [False, True])
def test_missing_context_after_model_receive_retains_cost_without_reply(
    core, tmp_path, change_reply,
):
    async def scenario():
        async with native_continuity(core, tmp_path) as (
            _, _, calls, sends, capture_input, finish,
        ):
            await capture_input()
            await finish()
            await capture_input()
            async def receive_then_remove(response):
                core.store.execute("DELETE FROM agent_conversations WHERE job_id=(SELECT job_id "
                                   "FROM jobs ORDER BY rowid DESC LIMIT 1)")
                if change_reply:
                    mutate_source(core, "reply_text")

            core.model.client.event_hooks["response"].append(receive_then_remove)
            assert await core.run_once()
            assert len(calls) == 2 and len(sends) == 1
            expected = 1 if change_reply else 2
            assert core.store.one("SELECT COUNT(*) n FROM outbox")["n"] == expected
            if not change_reply:
                failed = core.store.one("SELECT event_id FROM jobs ORDER BY rowid DESC LIMIT 1")
                assert_failure_notice(core, failed["event_id"])
            result = core.store.one(
                "SELECT state,last_error_code FROM jobs ORDER BY rowid DESC LIMIT 1"
            )
            assert result == ({"state": "CANCELLED", "last_error_code": "REPLY_CANCELLED"}
                              if change_reply else {"state": "FAILED",
                                                    "last_error_code": "MODEL_OUTCOME_UNKNOWN"})
            usage = core.store.rows("SELECT state,billed_micro_usd FROM model_usage")
            assert len(usage) == 2 and all(
                row["state"] == "SETTLED" and row["billed_micro_usd"] > 0 for row in usage
            )

    run(scenario())


def test_send_check_refuses_missing_snapshot_after_completed_model(core, tmp_path):
    async def scenario():
        async with native_continuity(core, tmp_path) as (
            window, client, calls, sends, capture_input, finish,
        ):
            await capture_input()
            await finish()
            await capture_input()
            assert await core.run_once()
            leased = await client.post("/internal/v1/deliveries/lease", json={
                "worker_id": "native-history-check", "authority": window.runtime.authority})
            assert leased.status_code == 200, leased.text
            item = leased.json()["items"][0]
            begun = await client.post(f"/internal/v1/deliveries/{item['message_id']}/begin-send",
                                      json={"lease_token": item["lease_token"]})
            assert begun.status_code == 200, begun.text
            check = {"authority": window.runtime.authority, "event_id": item["origin_event_id"],
                "message_id": item["message_id"],
                "content_sha256": paid_budget.sha(item["content"]),
                "reply_revision": item["reply_revision"], "lease_token": item["lease_token"]}
            path = "/internal/v1/owner-authority/send-check"
            assert (await client.post(path, json=check)).json()["approved"] is True
            core.store.execute("DELETE FROM agent_conversations WHERE job_id=(SELECT job_id "
                               "FROM jobs WHERE event_id=?)", (item["origin_event_id"],))
            response = await client.post(path, json=check)
            assert response.status_code == 409, response.text
            assert response.json()["error"] == "REPLY_CANCELLED"
            assert len(calls) == 2 and len(sends) == 1
            assert core.store.one("SELECT state FROM outbox WHERE message_id=?",
                                  (item["message_id"],))["state"] == "SENDING"

    run(scenario())
