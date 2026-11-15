"""Explicit situation preferences stay scoped and retain their original sources."""

import pytest

from companion.affect import NEUTRAL
from companion.contracts import CoreError
from tests.conftest import ingest

AFFECT = {"state": NEUTRAL, "evidence": []}


def train(core):
    core.settings.expression_learning_enabled = True
    core.settings.expression_enabled = True
    core.settings.emerging_min_sessions = core.settings.stable_min_days = 3
    for day in range(3):
        for offset, text in enumerate(["我吐槽时简短一点", "我开玩笑时简短一点",
                                       "讨论时详细解释", "正式求助时分步骤说"]):
            ingest(core, day * 10 + offset, text)
        core.clock.advance(86400)


def job(core, number, text):
    ingest(core, number, text)
    return core.store.one("SELECT * FROM jobs WHERE event_id=%s AND kind='chat'",
                          (f"evt_test_{number}",))


@pytest.mark.parametrize("previous,expected", [
    ("我想吐槽今天的事", "venting"), ("我只是在开玩笑", "joking"),
    ("我们讨论一下方案", "discussion"), ("我想正式求助解决这个问题", "help"),
])
def test_same_short_reply_uses_only_authorized_preceding_context(core, previous, expected):
    train(core)
    job(core, 90, previous)
    current = job(core, 91, "嗯")
    service = core.expression_learning
    assert service.reply_reference(core.settings, current, AFFECT) is None
    result = service.reply_reference(core.settings, current, AFFECT,
                                    context_sources=[{"event_id": "evt_test_90"}])
    selected = result["snapshot"]["items"]
    assert len(selected) <= 2 and {item["situation"] for item in selected} == {expected}
    assert "evt_test_90" in result["source_event_ids"]
    service.revalidate_reply(core.settings, current, AFFECT, result["snapshot"])
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, ["evt_test_90"], core.clock())
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        service.revalidate_reply(core.settings, current, AFFECT, result["snapshot"])
    assert not core.model.calls


def test_latest_scoped_correction_does_not_reset_other_situations(core):
    train(core)
    job(core, 90, "我吐槽时详细解释")
    items = core.expression_learning.snapshot(core.settings.owner_qq_id)["items"]
    venting = next(item for item in items if item["situation"] == "venting")
    assert venting["value"] == "detailed" and venting["status"] == "CANDIDATE"
    assert all(item["status"] == "STABLE" for item in items if item["situation"] != "venting")
    current = job(core, 91, "我们讨论方案。简短一点")
    assert core.expression_learning.reply_reference(core.settings, current, AFFECT) is None
    closing = job(core, 92, "先这样")
    assert core.expression_learning.reply_reference(core.settings, closing, AFFECT,
        context_sources=[{"event_id": "evt_test_91"}]) is None


@pytest.mark.parametrize("text", [
    "他说我吐槽时简短一点", "如果我吐槽时简短一点", "“讨论时详细解释”",
    "我开玩笑时简短一点，详细解释", "网页上说：正式求助时分步骤说",
])
def test_quotes_hypotheses_and_contradictions_do_not_become_preferences(core, text):
    ingest(core, 1, text)
    assert core.expression_learning.snapshot(core.settings.owner_qq_id)["items"] == []
