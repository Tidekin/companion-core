"""Quotes of erased, forgotten or recalled local messages never reach the model."""

import json

import pytest

from companion import dialogue_history, qq_quotes
from companion.contracts import InboundEvent
from companion.input_recall import marker_key
from companion.store import encode
from tests.support.onebot_events import normalized, raw_event
from tests.test_quote_attribution import QUOTE, delivered, quoted_job

OWNER_TEXT = "我上周说的那个私事其实是这样的"


def owner_message(core, number=7, text=OWNER_TEXT):
    event = InboundEvent.model_validate(normalized(raw_event(number, text=text)))
    saved = core.ingest(event, event.event_id)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?", (saved["job_id"],))
    return core.store.one("SELECT * FROM events WHERE event_id=?", (event.event_id,))


def erase(core, event_id):
    with core.store.transaction():
        core.ledger.expire_events(core.store, [event_id], core.clock())


def recall(core, source_event_id):
    core.store.execute("INSERT INTO runtime_meta(key,value) VALUES(?,?)",
                       (marker_key(source_event_id), encode({"state": "RECALLED"})))


def prompt(core, job):
    return json.dumps(core.context(job), ensure_ascii=False)


@pytest.mark.parametrize("fault", ["erased", "recalled"])
def test_quoted_owner_message_is_withdrawn(core, fault):
    row = owner_message(core)
    if fault == "erased":
        erase(core, row["event_id"])
    else:
        recall(core, row["source_event_id"])
    job, _ = quoted_job(core, quote=OWNER_TEXT, reference="7")
    text = prompt(core, job)
    assert OWNER_TEXT not in text
    assert "原文已被主人删除或撤回" in text
    # The signed capture stays as the Bridge recorded it, so replay still verifies.
    stored = core.store.one("SELECT body_json FROM events WHERE event_id=?", (job["event_id"],))
    assert OWNER_TEXT in stored["body_json"]


def test_quote_of_an_erased_own_reply_is_withdrawn_and_not_attributed(core):
    message_id = delivered(core)
    origin = core.store.one("SELECT origin_event_id FROM outbox WHERE message_id=?",
                            (message_id,))["origin_event_id"]
    erase(core, origin)
    job, _ = quoted_job(core)
    text = prompt(core, job)
    assert QUOTE not in text and "原文已被主人删除或撤回" in text
    assert "本地作者核验：" not in text


@pytest.mark.parametrize("reference", ["7", "999"])
def test_active_or_unknown_quotes_are_left_as_read(core, reference):
    owner_message(core)
    job, _ = quoted_job(core, quote=OWNER_TEXT, reference=reference)
    text = prompt(core, job)
    assert OWNER_TEXT in text and "原文已被主人删除或撤回" not in text


def test_a_later_erasure_also_withdraws_the_quote_from_history(core):
    row = owner_message(core)
    job, _ = quoted_job(core, quote=OWNER_TEXT, reference="7")
    source = core.store.one(
        "SELECT event_id,body_json,json_extract(body_json,'$.text') AS user_text,"
        "'' AS reply_text FROM events WHERE event_id=?", (job["event_id"],))
    assert OWNER_TEXT in json.dumps(dialogue_history.turn_messages(source, core.store),
                                    ensure_ascii=False)
    erase(core, row["event_id"])
    replay = json.dumps(dialogue_history.turn_messages(source, core.store), ensure_ascii=False)
    assert OWNER_TEXT not in replay and "原文已被主人删除或撤回" in replay


def test_other_conversations_and_epochs_never_match():
    assert qq_quotes.namespace("10002:qq:FriendMessage:10001:epoch:abcdefgh:7") is None
    assert qq_quotes.namespace("10002:onebot-direct:FriendMessage:10001:epoch:short:7") is None
    assert qq_quotes.namespace(
        "10002:onebot-direct:FriendMessage:10001:epoch:abcdefgh:7"
    ) == "10002:onebot-direct:FriendMessage:10001:epoch:abcdefgh"
    assert qq_quotes.media_slots({"rich": {"segments": [
        {"index": 0, "type": "reply", "body": {"media": [{"slot": 2}]}},
        {"index": 1, "type": "reply", "body": {"media": [{"slot": 3}]}}]}}, {0}) == {2}
