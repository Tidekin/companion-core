"""Appraisals reach the model, act from the owner's words and bind hurt to one reply."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from companion import mood_appraisals
from companion.contracts import InteractionSignal
from companion.model import EXTRACT_RULES
from companion.store import encode
from tests.conftest import ingest, run
from tests.support.mood import erase, feedback
from tests.support.mood_appraisals import appraise
from tests.support.mood_appraisals_integration import prepare_extraction


def settle(core, request_id):
    core.store.execute(
        "INSERT INTO model_usage (request_id,scenario,provider,model,state,input_tokens,"
        "output_tokens,occurred_at) VALUES (?, 'memory_extract','synthetic','synthetic',"
        "'SETTLED',100,20,?)", (request_id, core.clock()),
    )


def judge(core, key, quote, reason="OWNER_AFFECTION", request_id=None):
    """Judge and record an already ingested message at the current clock."""
    request_id = request_id or "judge_" + key
    settle(core, request_id)
    signals = mood_appraisals.validate(
        core.store, [InteractionSignal(event_id=key, reason=reason, quote=quote)], {key}
    )
    with core.store.transaction():
        mood_appraisals.record(core.store, core.settings, signals, request_id, core.clock())
    return signals


def test_extraction_request_asks_for_every_signal_reason(core):
    assert '"interaction_signals":[...]' in EXTRACT_RULES
    for reason in mood_appraisals.REASONS:
        assert reason in EXTRACT_RULES
    prepare_extraction(core)
    assert run(core.extract_once())
    system = core.model.calls[0][0][0]["content"]
    assert system == EXTRACT_RULES


def test_extraction_request_explains_expectations_expressed_as_requests(core):
    prepare_extraction(core)
    assert run(core.extract_once())
    system = core.model.calls[0][0][0]["content"]
    # The actual request must distinguish relational wishes from ordinary tool tasks.
    assert "未来的互动尚未发生，不等于假设" in system
    assert "明晚回来陪我聊聊吧" in system
    assert "明早提醒我开会" in system
    assert "同一事件可以同时写入记忆和互动信号" in system


def test_relationship_memory_does_not_consume_its_expectation_appraisal(core):
    core.settings.memory_enabled = core.settings.mood_enabled = True
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    text = "明天你要第一个跟我说早安哦"
    key = ingest(core, 1, text)["event_id"]
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.model.extraction = encode({
        "memories": [{"subject": "relationship", "kind": "preference",
                      "fact_key": "morning_greeting", "content": "主人希望你明早先来问候。",
                      "evidence_event_id": key, "evidence_quote": text}],
        "interaction_signals": [{"event_id": key, "reason": "OWNER_EXPECTATION",
                                 "quote": text}],
    })
    assert run(core.extract_once())
    assert core.store.one("SELECT event_id,reason FROM mood_appraisals") == {
        "event_id": key, "reason": "OWNER_EXPECTATION"}
    assert core.store.one("SELECT event_id FROM memory_evidence WHERE polarity=1") == {
        "event_id": key}
    assert [s["reason"] for s in core.mood.snapshot()["signals"]] == ["OWNER_EXPECTATION"]


def test_delayed_extraction_decays_from_when_the_owner_spoke(core):
    core.settings.mood_enabled = True
    said = core.clock()
    key = ingest(core, 1, "想你了")["event_id"]
    core.clock.advance(6 * 3600)
    judge(core, key, "想你了")
    view = core.mood.snapshot()
    assert view["signals"][0]["at"] == datetime.fromisoformat(said).astimezone(
        ZoneInfo("UTC")).isoformat(timespec="microseconds")
    # Two 3-hour half-lives since the words, not a fresh +0.05 at processing time.
    assert view["immediate"]["pleasure"] == pytest.approx(0.05 / 4, abs=1e-6)
    # Before it was judged, history does not know about it.
    assert core.mood.snapshot(at=said)["signals"] == []


def test_daily_cap_counts_the_day_the_owner_spoke(core):
    zone = ZoneInfo(core.settings.timezone)
    late = datetime(2026, 11, 1, 23, 30, tzinfo=zone)
    core.clock.now = late.astimezone(ZoneInfo("UTC")) - timedelta(hours=2)
    appraise(core, 1, "想你一")
    appraise(core, 2, "想你二")
    core.clock.now = late.astimezone(ZoneInfo("UTC"))
    key = ingest(core, 3, "想你三")["event_id"]
    core.clock.advance(3600)  # judged after local midnight
    judge(core, key, "想你三")
    assert [s["event_id"] for s in core.mood.snapshot()["signals"]] == [
        "evt_test_1", "evt_test_2"]


def test_signals_outside_the_consumer_scope_use_no_daily_quota(core):
    keys = [appraise(core, number, f"想你{number}") for number in range(1, 4)]
    assert [s["event_id"] for s in core.mood.snapshot()["signals"]] == keys[:2]
    scoped = core.mood.snapshot(allowed=[keys[2]])["signals"]
    assert [s["event_id"] for s in scoped] == [keys[2]]


def hurt(core):
    key = feedback(core, 1, text="你刚才那样说让我有点难受")
    signals = judge(core, key, "让我有点难受", "OWNER_HURT_BY_REPLY")
    assert signals and signals[0]["target_message_id"]
    return key, signals[0]["target_message_id"]


def test_hurt_rests_on_the_reply_and_its_input(core):
    key, _ = hurt(core)
    signal = core.mood.snapshot()["signals"][0]
    assert signal["reason"] == "OWNER_HURT_BY_REPLY"
    assert signal["source_event_ids"] == ["evt_test_2", key]
    erase(core, ["evt_test_2"])  # the input the reply answered
    assert core.mood.snapshot()["signals"] == []


@pytest.mark.parametrize("damage", ["deleted", "changed", "not_sent"])
def test_hurt_drops_when_its_reply_no_longer_matches(core, damage):
    _, target = hurt(core)
    assert core.mood.snapshot()["signals"]
    if damage == "deleted":
        core.store.execute("DELETE FROM outbox WHERE message_id=?", (target,))
    elif damage == "changed":
        core.store.execute(
            "UPDATE outbox SET content_sha256='other' WHERE message_id=?", (target,))
    else:
        core.store.execute("UPDATE outbox SET state='FAILED' WHERE message_id=?", (target,))
    assert core.mood.snapshot()["signals"] == []


def test_hurt_needs_an_unambiguous_reply(core):
    key = feedback(core, 1, text="你刚才那样说让我有点难受")
    reply = core.store.one("SELECT * FROM outbox")
    core.store.execute(
        "INSERT INTO outbox(message_id,origin_event_id,conversation_id,action_type,"
        "content_json,content_sha256,state,delivery_key,attempt_count,created_at,updated_at) "
        "VALUES ('msg_newer',?,?,'qq.private.proactive','{}','x','DELIVERY_UNCERTAIN',"
        "'newer',1,?,?)",
        (reply["origin_event_id"], reply["conversation_id"], reply["created_at"],
         reply["created_at"]),
    )
    assert judge(core, key, "让我有点难受", "OWNER_HURT_BY_REPLY") == []


def test_hurt_rows_without_a_bound_reply_are_ignored(core):
    hurt(core)
    core.store.execute("UPDATE mood_appraisals SET target_message_id=NULL,target_sha256=NULL")
    assert core.mood.snapshot()["signals"] == []
