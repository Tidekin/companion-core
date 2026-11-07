"""Owner bursts wait briefly, are answered once, and commands are never delayed."""

from datetime import datetime

from companion import dialogue_history, input_debounce
from companion.config import Settings
from companion.input_debounce import InputDebounceConfig
from companion.store import after
from tests.support.input_debounce import accept
from tests.support.runtime_configuration import hot as hot
from tests.support.runtime_configuration import save


def enable(core, **values):
    core.settings.input_debounce = InputDebounceConfig(**values)


def seconds(core, value):
    return (datetime.fromisoformat(value) - datetime.fromisoformat(core.clock())).total_seconds()


def test_defaults_are_on_and_stay_out_of_the_settings_dump():
    settings = Settings(owner_qq_id="10001", bot_qq_id="10002", bridge_token="x" * 32)
    assert settings.input_debounce.enabled is True
    assert "input_debounce" not in settings.model_dump()


def test_a_quiet_message_waits_two_seconds_before_it_can_be_claimed(core):
    enable(core)
    first, due = accept(core, 1)
    assert seconds(core, due) == 2
    assert core.claim_job("worker") is None
    core.clock.advance(2)
    assert core.claim_job("worker")["job_id"] == first["job_id"]


def test_an_active_chat_waits_longer_to_open_a_batch(core):
    enable(core)
    earlier, _ = accept(core, 1)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (earlier["job_id"],))
    core.clock.advance(10)
    _, due = accept(core, 2)
    assert seconds(core, due) == 5


def test_a_burst_is_answered_once_with_every_input_in_context(core):
    enable(core)
    first, _ = accept(core, 1, "我跟你说")
    core.clock.advance(1)
    second, due = accept(core, 2, "今天老板又改需求了")
    assert seconds(core, due) == 2  # Continuation, not a fresh active opening.
    assert core.store.one('SELECT state FROM jobs WHERE job_id=%s',
                          (first["job_id"],))["state"] == "CANCELLED"
    core.clock.advance(2)
    job = core.claim_job("worker")
    assert job["job_id"] == second["job_id"]
    pending = dialogue_history.pending_owner_sources(
        core.store, job["conversation_id"], second["event_id"], 10)
    assert [row["event_id"] for row in pending] == [first["event_id"]]


def test_the_whole_batch_is_bounded(core):
    enable(core)
    start = core.clock()
    for number in range(1, 6):  # 0 s to 6 s: one batch that keeps being extended.
        _, due = accept(core, number, f"第{number}条")
        core.clock.advance(1.5)
    assert due == after(start, 8)
    assert core.claim_job("worker") is None  # 7.5 s: still inside the batch.
    core.clock.advance(0.5)  # Claimed at the 8 s bound, not 2 s after the last input.
    assert core.claim_job("worker")["event_id"] == "evt_test_5"


def test_hot_edit_between_messages_preserves_open_batch_wait_and_cap(hot):
    enable(hot, continue_seconds=3, batch_seconds=4)
    start = hot.clock()
    _, due = accept(hot, 1)
    assert due == after(start, 2)
    save(hot, input_debounce={"active_seconds": 6, "continue_seconds": 1, "batch_seconds": 10})
    hot.clock.advance(1)
    _, due = accept(hot, 2)
    assert due == after(start, 4)
    hot.clock.advance(1)
    _, due = accept(hot, 3)
    assert due == after(start, 4)  # Original cap, even after another extension.
    hot.clock.advance(3)
    _, due = accept(hot, 4)
    assert seconds(hot, due) == 6
    hot.clock.advance(1)
    _, due = accept(hot, 5)
    assert seconds(hot, due) == 1


def test_legacy_timestamp_batch_finishes_with_startup_timing(hot):
    enable(hot)
    first, _ = accept(hot, 1)
    row = hot.store.one((
        'SELECT conversation_id FROM events WHERE event_id=%s'
    ), (first["event_id"],))
    hot.store.execute('UPDATE runtime_meta SET value=%s WHERE key=%s',
                      (hot.clock(), input_debounce.BATCH_KEY + row["conversation_id"]))
    save(hot, input_debounce={"continue_seconds": 7})
    hot.clock.advance(1)
    _, due = accept(hot, 2)
    assert seconds(hot, due) == 2


def test_bare_media_waits_longer_and_commands_are_not_delayed(core):
    enable(core)
    conversation = "conv_synthetic"
    now = core.clock()
    media = input_debounce.due(core.store, core.settings, conversation, "evt_x",
                               {"text": "", "media": [{"kind": "image"}]}, now)
    assert seconds(core, media) == 4
    for body in ({"text": "/记住这个"}, {"text": "日记", "diary_command": True},
                 {"text": "目标列表", "control_command": True}):
        assert input_debounce.due(core.store, core.settings, conversation, "evt_y",
                                  body, now) is None


def test_disabled_debounce_claims_immediately(core):
    enable(core, enabled=False)
    first, due = accept(core, 1)
    assert due is None
    assert core.claim_job("worker")["job_id"] == first["job_id"]
