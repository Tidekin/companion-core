"""Native evidence, time continuity, withdrawal and reconstruction of mood hints."""

from contextlib import closing
from datetime import date, datetime

import pytest
from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.life_context import decision_context
from companion.memory import Memory
from companion.mood import RESET_KEY, MoodService, reset_record, validate_reset_record
from companion.store import Store, encode
from companion.transport import enqueue
from tests.conftest import ingest, run
from tests.test_cognition_goal_steps import finish_record, retained_plan
from tests.test_consolidation import enable_nightly
from tests.test_diary import generate
from tests.test_owner_authority import activate, prepare
from tests.test_watch_ledger import JUDGEMENT, Provider, enable, observe, step


def feedback(core, number=1, text="这个回答很有帮助"):
    core.settings.mood_enabled = True
    ingest(core, number * 2, "一起看看这个问题")
    event = core.store.one("SELECT * FROM events WHERE event_id=?", (f"evt_test_{number * 2}",))
    mid = enqueue(
        core.store, event["event_id"], event["conversation_id"], "有依据的回答", core.clock()
    )
    core.store.execute(
        "UPDATE outbox SET state='SENT',sent_at=? WHERE message_id=?", (core.clock(), mid)
    )
    core.clock.advance(1)
    ingest(core, number * 2 + 1, text)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    return f"evt_test_{number * 2 + 1}"


def pleasure(core, layer="expression"):
    return core.mood.snapshot()[layer]["pleasure"]


def erase(core, ids):
    with core.store.transaction():
        core.ledger.expire_events(core.store, ids, core.clock())


def test_continuity_inertia_conflicting_events_and_hourly_decay(core):
    feedback(core)
    first = pleasure(core)
    assert 0 < first <= 0.08
    assert pleasure(core, "undertone") == 0
    ingest(core, 100, "聊点别的吧")
    assert pleasure(core) == first  # ordinary chat does not reset or invent a signal
    core.clock.advance(3 * 3600)
    assert pleasure(core) == pytest.approx(first / 2, abs=1e-6)
    feedback(core, 2)
    second = pleasure(core)
    assert first < second < first + 0.08
    feedback(core, 3, "这个回答让我有点失望")
    assert 0 < pleasure(core) < second
    assert core.mood.snapshot()["undertone"]["dominance"] < 0
    for number in range(4, 15):
        feedback(core, number)
    state = core.mood.snapshot()
    assert 0 < state["undertone"]["pleasure"] < 0.15
    assert 0 < state["immediate"]["pleasure"] < 0.35
    assert max(abs(x) for x in state["expression"].values()) < 0.5
    core.clock.advance(3 * 86400)
    decayed = core.mood.snapshot()
    assert decayed["undertone"]["pleasure"] == pytest.approx(
        state["undertone"]["pleasure"] / 2, abs=1e-6
    )
    assert abs(decayed["immediate"]["pleasure"]) < 1e-5


def test_same_reply_feedback_deduplicated_and_neutral_chat_never_hides_history(core):
    feedback(core)
    before = pleasure(core)
    for number in range(100, 380):
        ingest(core, number, "今天聊了普通话题")
    assert pleasure(core) == before
    ingest(core, 400, "这个回答很有帮助")
    state = core.mood.snapshot()
    assert len(state["signals"]) == 1 and state["undertone"]["pleasure"] == 0


@pytest.mark.parametrize(
    "text",
    [
        "我很开心",
        "你不理我了",
        "主人沉默了",
        "简短一点",
        "假设你刚才的回答很有帮助",
        "他说：这个回答很有帮助",
        "这个回答很有帮助吗",
    ],
)
def test_no_inferred_owner_emotion_silence_fiction_or_preferences(core, text):
    feedback(core, text=text)
    assert core.mood.snapshot()["signals"] == []
    core.clock.advance(2 * 86400)
    assert pleasure(core) == 0


@pytest.mark.parametrize("damage", ["actor", "quote", "private", "delivery", "upstream"])
def test_feedback_requires_actual_private_interaction_and_live_ancestry(core, damage):
    key = feedback(core)
    if damage == "actor":
        core.store.execute("UPDATE events SET actor_id='companion' WHERE event_id=?", (key,))
    elif damage == "quote":
        core.store.execute(
            "UPDATE events SET body_json=? WHERE event_id=?",
            (encode({"text": "这个回答很有帮助", "quote": {"text": "引用"}}), key),
        )
    elif damage == "private":
        core.store.execute("UPDATE conversations SET scope='group'")
    elif damage == "delivery":
        core.store.execute("UPDATE outbox SET state='DELIVERY_UNCERTAIN'")
    else:
        erase(core, ["evt_test_2"])
    assert pleasure(core) == 0


def test_snapshot_is_readonly_rebuilds_across_day_and_checks_pinned_sources(core):
    key = feedback(core)
    view = core.mood.snapshot()
    changes = core.store.db.total_changes
    core.clock.advance(29 * 3600)
    core.mood.revalidate(view)  # a frozen prompt remains valid as time advances
    current = core.mood.snapshot()
    assert current["expression"] != view["expression"]
    assert core.store.db.total_changes == changes and core.model.calls == []
    with closing(Store(core.settings.database)) as reopened:
        assert (
            MoodService(Memory(reopened, core.settings, core.ledger), core.clock).snapshot()
            == current
        )
    erase(core, [key])
    assert pleasure(core) == 0
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        core.mood.revalidate(view)


def test_source_forgotten_during_chat_cannot_deliver_old_mood(core):
    key = feedback(core)
    ingest(core, 100, "继续聊聊阅读")
    job = core.store.one("SELECT job_id FROM jobs WHERE event_id='evt_test_100'")
    core.model.callback = lambda: erase(core, [key])
    assert run(core.run_once(job_id=job["job_id"]))
    state = core.store.one("SELECT state,last_error_code FROM jobs WHERE job_id=?",
                           (job["job_id"],))
    assert state == {"state": "CANCELLED", "last_error_code": "REPLY_SOURCE_RECALLED"}
    assert not core.store.one("SELECT 1 FROM outbox WHERE origin_event_id='evt_test_100'")
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"


def test_forgetting_one_foundation_rebuilds_undertone_and_retention_removes_all(core):
    key = feedback(core)
    feedback(core, 2)
    feedback(core, 3)
    assert pleasure(core, "undertone") > 0
    erase(core, [key])
    assert pleasure(core, "undertone") == 0
    assert pleasure(core) > 0
    core.settings.raw_event_retention_days = 1
    core.clock.advance(2 * 86400)
    assert core.mood.snapshot()["signals"] == []


def test_delayed_ingestion_uses_actual_event_time_and_cannot_cross_reset(core):
    key = feedback(core)
    original = core.clock()
    core.clock.advance(6 * 3600)
    core.store.execute("UPDATE events SET ingested_at=? WHERE event_id=?", (core.clock(), key))
    assert pleasure(core) == pytest.approx(.02, abs=1e-6)
    assert core.mood.snapshot()["signals"][0]["at"] == original
    core.mood.control("clear", core.settings.owner_qq_id, request_id="late-feedback")
    core.clock.advance(1)
    core.store.execute("UPDATE events SET ingested_at=? WHERE event_id=?", (core.clock(), key))
    assert core.mood.snapshot()["signals"] == []


def test_uncertain_new_reply_cannot_retarget_feedback_to_an_older_sent_reply(core):
    first = feedback(core)
    core.clock.advance(1)
    ingest(core, 10, "再讨论一下这个问题")
    event = core.store.one("SELECT * FROM events WHERE event_id='evt_test_10'")
    mid = enqueue(core.store, event["event_id"], event["conversation_id"],
                  "这一次的回答", core.clock())
    core.store.execute("UPDATE outbox SET state='DELIVERY_UNCERTAIN' WHERE message_id=?", (mid,))
    core.clock.advance(1)
    ingest(core, 11, "这个回答很有帮助")
    assert [item["event_id"] for item in core.mood.snapshot()["signals"]] == [first]


@pytest.mark.parametrize("action", ["forget", "clear"])
def test_latest_ledger_blocks_old_backup_and_recompute_cannot_undo_clear(core, tmp_path, action):
    key = feedback(core)
    archive, restored, secret = (
        tmp_path / "before.enc",
        tmp_path / "restored.db",
        Fernet.generate_key(),
    )
    backup(core.settings.database, archive, secret)
    if action == "forget":
        erase(core, [key])
    else:
        core.mood.control("clear", core.settings.owner_qq_id, request_id="clear-once")
    restore(archive, restored, secret, core.settings.erasure_ledger)
    with closing(Store(str(restored))) as store:
        service = MoodService(Memory(store, core.settings, core.ledger), core.clock)
        assert service.snapshot()["signals"] == []
        assert service.control("recompute", core.settings.owner_qq_id)["mood"]["signals"] == []
        if action == "clear":
            assert store.one("SELECT value FROM runtime_meta WHERE key=?", (RESET_KEY,))


def test_pause_clear_new_sources_and_affect_are_independent(core):
    feedback(core)
    core.settings.affect_enabled = True
    ingest(core, 50, "简短一点")
    assert core.affect.snapshot()["evidence"]
    owner = core.settings.owner_qq_id
    proof = core.mood.snapshot()
    assert core.mood.control("pause", owner)["mood"]["paused"]
    assert core.mood.reference() is None
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        core.mood.revalidate(proof)
    core.clock.advance(3600)
    assert core.mood.control("resume", owner)["mood"]["expression"]["pleasure"] > 0
    core.mood.control("clear", owner, request_id="clear-once")
    assert core.mood.control("recompute", owner)["mood"]["signals"] == []
    assert core.affect.snapshot()["evidence"]
    feedback(core, 2)
    assert pleasure(core) > 0
    assert core.mood.control("clear", owner, request_id="clear-once")["state"] == "RESET"
    assert pleasure(core) > 0  # idempotent retry does not advance the barrier
    with pytest.raises(CoreError, match="OWNER_REQUIRED"):
        core.mood.control("pause", "other")


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_ids", ["some-event"]),
        ("memory_ids", ["memory"]),
        ("memory_id", "affect_reset:one"),
        ("at", "2026-10-31"),
    ],
)
def test_mood_reset_record_cannot_erase_sources_or_other_namespaces(core, field, value):
    record = reset_record(1, "clear", core.clock())
    record[field] = value
    with pytest.raises(ValueError):
        validate_reset_record(record)


def test_goal_success_needs_real_step_receipts_and_simulation_never_counts(core):
    core.settings.mood_enabled = True
    goal, steps = retained_plan(core)
    results = [finish_record(core, step, i) for i, step in enumerate(steps, 1)]
    core.store.execute(
        "UPDATE goals SET state='DONE',result_event_id=? WHERE goal_id=?", (results[-1], goal)
    )
    assert core.mood.snapshot()["signals"][0]["reason"] == "VERIFIED_GOAL_COMPLETION"
    assert core.mood.snapshot()["expression"]["dominance"] > 0
    core.store.execute(
        "UPDATE goal_steps SET state='EXECUTION_UNCERTAIN' WHERE step_id=?", (steps[0],)
    )
    assert core.mood.snapshot()["signals"] == []
    core.store.execute("UPDATE goal_steps SET state='DONE' WHERE step_id=?", (steps[0],))
    core.store.execute(
        "UPDATE events SET source='observation.simulation' WHERE event_id=?", (results[0],)
    )
    assert core.mood.snapshot()["signals"] == []


def test_watch_uses_settled_judgement_not_self_report_or_title(core, tmp_path):
    import httpx

    from companion.model import ModelClient
    from tests.test_exploration import configure

    configure(core)
    enable(core)
    core.settings.mood_enabled = True
    activate(
        core, prepare(core, tmp_path).model_copy(update={"purposes": ["exploration", "expression"]})
    )
    provider = Provider(encode(JUDGEMENT))
    core.model = ModelClient(
        core.settings, httpx.AsyncClient(transport=httpx.MockTransport(provider))
    )
    key = observe(core, 1)
    assert core.mood.snapshot()["signals"] == []
    assert step(core)
    assert pleasure(core) == 0.03
    assert pleasure(core, "undertone") == 0
    calls = len(provider.calls)
    core.mood.snapshot()
    assert len(provider.calls) == calls == 1
    core.store.execute("UPDATE watch_ledger SET depth='title_only'")
    assert core.mood.snapshot()["signals"] == []
    core.store.execute("UPDATE watch_ledger SET depth='text'")
    erase(core, [key])
    assert core.mood.snapshot()["signals"] == []


def test_life_reference_is_scoped_and_pinned_across_elapsed_time(core):
    key = feedback(core)
    now = core.clock()
    scope = [{"event_id": key}, {"event_id": "evt_test_2"}]
    context = decision_context(core.store, core.settings, scope, "synthetic", now)
    assert context["mood"]["state"]["pleasure"] > 0
    assert "mood" not in decision_context(core.store, core.settings, scope[:1], "synthetic", now)
    core.clock.advance(3600)
    assert (
        decision_context(
            core.store,
            core.settings,
            scope,
            "synthetic",
            core.clock(),
            mood_at=context["mood"]["evaluated_at"],
        )
        == context
    )


def test_serious_context_uses_existing_expression_guard(core):
    feedback(core)
    assert core.mood.reference(text="今天的星星很好看")
    assert core.mood.reference(text="认真说，家人今天住院了") is None
    assert core.mood.snapshot()["signals"]  # suppression is not erasure


def test_life_job_revalidates_frozen_mood_during_model_and_readback(core):
    import json

    from tests.test_life_topics import configure, due, proposal

    configure(core)
    feedback_id = feedback(core, 2)
    core.life_topics.synchronize()
    proposal(core, source_event_ids=["evt_test_4", feedback_id])

    def elapsed():
        material = json.loads(core.model.calls[-1][0][-1]["content"])
        assert material["decision_context"]["mood"]["proof"]["source_event_ids"]
        core.clock.advance(1)

    core.model.callback = elapsed
    job = due(core)
    assert job and run(core.run_once(job_id=job))
    row = core.store.one("SELECT state,last_error_code FROM jobs WHERE job_id=?", (job,))
    assert row == {"state": "SUCCEEDED", "last_error_code": None}
    result = core.store.one("SELECT state FROM life_activity_results WHERE job_id=?", (job,))
    assert result["state"] == "SHADOW"
    assert core.life_topics.preview(core.settings.owner_qq_id)["items"]


def test_diary_reflection_reuse_sources_without_feedback_loop(core):
    core.clock.now = datetime.fromisoformat("2026-10-18T04:00:00+00:00")
    feedback(core)
    core.clock.advance(1)
    feedback(core, 2)
    core.clock.now = datetime.fromisoformat("2026-10-19T04:00:00+00:00")
    model = enable_nightly(core, seed=False)
    core.settings.diary_enabled = True
    before = core.mood.snapshot()
    generate(core)
    assert run(core.reflections.run_day(date(2026, 10, 18)))["state"] == "COMMITTED"
    assert [scenario for _, scenario in model.calls] == ["diary", "reflection"]
    for messages, _ in model.calls:
        assert '"mood"' in messages[-1]["content"]
    assert core.mood.snapshot() == before  # generated feelings are never new mood evidence
