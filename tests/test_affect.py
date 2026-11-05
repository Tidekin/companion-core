import json

import pytest
from cryptography.fernet import Fernet

from companion.affect import NEUTRAL
from companion.backup import backup, restore
from companion.core import Core
from tests.conftest import ingest, run
from tests.test_observations import enable_observations, observation


def test_affect_off_neutral_and_no_hidden_model_calls(core):
    ingest(core, 1, "简短一点")
    assert core.affect.snapshot()["state"] == NEUTRAL
    assert core.store.rows("SELECT * FROM affect_evidence") == []
    assert core.model.calls == []


def test_affect_explicit_feedback_survives_new_conversation_restart_and_expires(core):
    core.settings.affect_enabled = True
    ingest(core, 1, "简短一点。")
    reopened = Core(core.settings, core.model, core.clock)
    try:
        ingest(reopened, 2, "今天读什么", origin="another:FriendMessage:10001")
        assert reopened.affect.snapshot()["state"]["interaction_tone"] == "concise"
        assert run(reopened.run_once())
        messages = core.model.calls[0][0]
        assert any("OWNER_REQUESTED_CONCISE" in message["content"] for message in messages)
        core.clock.advance(180 * 60 + 1)
        assert reopened.affect.snapshot()["state"] == NEUTRAL
        assert not reopened.affect.observe("evt_test_1")
        assert len(core.model.calls) == 1  # No polling for decay or snapshots.
    finally:
        reopened.store.close()


def test_affect_unrelated_messages_and_later_dimensions_do_not_extend_old_ttl(core):
    core.settings.affect_enabled = True
    ingest(core, 1, "简短一点")
    core.clock.advance(120 * 60)
    ingest(core, 2, "分步骤说")
    core.clock.advance(61 * 60)
    ingest(core, 3, "你好")
    state = core.affect.snapshot()["state"]
    assert state["interaction_tone"] == "neutral" and state["cognitive_load"] == "stepwise"
    ingest(core, 4, "详细解释")
    assert core.affect.snapshot()["state"]["interaction_tone"] == "detailed"
    assert not core.affect.observe("evt_test_4")
    assert len(core.store.rows("SELECT * FROM affect_evidence")) == 3


def test_affect_does_not_infer_emotion_personality_or_accept_quoted_external_directives(core):
    core.settings.affect_enabled = True
    for i, text in enumerate(
        ("你是温柔的人", "我今天很难过", "网页上说：简短一点", "我喜欢新的游戏"), 1
    ):
        ingest(core, i, text)
    enable_observations(core)
    core.observations.append("reader", observation(core, text="简短一点"))
    assert not core.affect.observe("obs_test_1")
    assert core.affect.snapshot()["state"] == NEUTRAL
    assert (
        core.store.rows("SELECT * FROM memories") == core.store.rows("SELECT * FROM traits") == []
    )


def test_affect_source_erasure_retracts_only_dependent_state_and_survives_restore(core, tmp_path):
    core.settings.affect_enabled = True
    ingest(core, 1, "简短一点")
    ingest(core, 2, "分步骤说")
    key, archive, dest = Fernet.generate_key(), tmp_path / "affect.enc", tmp_path / "restored.db"
    backup(core.settings.database, archive, key)
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    restore(archive, dest, key, core.settings.erasure_ledger)
    restored = Core(core.settings.model_copy(update={"database": str(dest)}), clock=core.clock)
    try:
        for instance in (core, restored):
            assert instance.affect.snapshot()["state"]["interaction_tone"] == "neutral"
            assert instance.affect.snapshot()["state"]["cognitive_load"] == "stepwise"
            assert not instance.store.one(
                "SELECT 1 FROM affect_evidence WHERE event_id='evt_test_1'"
            )
    finally:
        restored.store.close()


def test_affect_keeps_cause_prior_state_and_bounded_evidence(core):
    core.settings.affect_enabled = True
    core.settings.affect_max_evidence = 4
    for i in range(6):
        ingest(core, i, "简短一点" if i % 2 == 0 else "详细解释")
    rows = core.store.rows("SELECT * FROM affect_evidence ORDER BY rowid")
    assert len(rows) == 4
    assert rows[-1]["prior_state"] == "concise" and rows[-1]["new_state"] == "detailed"
    projection = core.store.one("SELECT * FROM affect_states")
    assert json.loads(projection["state_json"])["interaction_tone"] == "detailed"
    assert projection["version"] == 6


def test_affect_contact_pause_survives_later_wording_feedback(core):
    core.settings.affect_enabled = True
    ingest(core, 0, "今天别主动找我")
    for i in range(1, core.settings.affect_max_evidence + 1):
        core.clock.advance(60)
        ingest(core, i, "简短一点" if i % 2 else "详细解释")
    assert core.affect.contact_paused()
    assert core.affect.snapshot()["state"]["engagement"] == "pause"
    assert core.store.one("SELECT 1 FROM affect_evidence WHERE event_id='evt_test_0'")
    ingest(core, 99, "可以继续")
    assert not core.affect.contact_paused()


@pytest.mark.parametrize(("text", "paused"), [
    ("今天别主动找我了，我想安静一下", True),
    ("我在开会，晚点再聊", True),
    ("我先不聊了，晚上再说", True),
    ("今天不用主动联系我", True),
    ("今晚不要主动找我啦", True),
    ("我不是说今天别主动找我", False),
    ("她说，今天别主动找我", False),
    ("她说：今天别主动找我", False),
    ("“今天别主动找我”这句话怎么翻译", False),
    ("今天别主动找我，开玩笑的", False),
    ("继续聊，今天别主动找我", False),
    ("今天谁主动找我了", False),
])
def test_affect_recognizes_own_pause_clauses_but_not_reported_wording(core, text, paused):
    core.settings.affect_enabled = True
    ingest(core, 1, text)
    assert core.affect.contact_paused() is paused


def test_affect_topic_pause_wording_scopes_to_the_topic(core):
    core.settings.affect_enabled = True
    ingest(core, 1, "我在学做蛋糕")
    ingest(core, 2, "这个话题先别聊了")
    assert core.affect.contact_paused(["evt_test_1"])
    assert not core.affect.contact_paused()
