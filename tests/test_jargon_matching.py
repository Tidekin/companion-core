"""Orthographic matching never loosens exact-quote extraction or forgotten keys."""

import pytest

from companion import jargon
from companion.store import encode
from tests.support.jargon import say


@pytest.mark.parametrize("text", ["我先afk一下", "我先ＡＦＫ一下", "我先AFK一下"])
def test_variants_share_one_source_count_and_owner_forget(core, text):
    say(core, 1, "我先AFK一下")
    jargon._upsert(core.store, "AFK", ["evt_test_1"], core.clock())
    say(core, 2, text)
    jargon.scan(core.store, core.settings, core.clock())
    jargon._upsert(core.store, "afk", ["evt_test_2"], core.clock())
    assert core.store.rows("SELECT term,uses FROM jargon_terms") == [{"term": "AFK", "uses": 2}]
    core.store.execute("UPDATE jargon_terms SET state='INFERRED',meaning='离开片刻'")
    result, refs = jargon.context(core.store, text, 3)
    assert result == [{"term": "AFK", "meaning": "离开片刻"}]
    assert refs == {"evt_test_1", "evt_test_2"}
    jargon.forget(core.store, "ａｆｋ")
    jargon._upsert(core.store, "afk", ["evt_test_2"], core.clock())
    assert not jargon.context(core.store, text, 3)[0]
    assert core.store.rows("SELECT term,state FROM jargon_terms") == [
        {"term": "AFK", "state": "FORGOTTEN"}]


def test_normalization_does_not_fabricate_original_evidence(core):
    say(core, 1, "我先afk一下")
    jargon._apply_extract(core.store, core.settings,
        {"terms": [{"term": "AFK", "event_ids": ["evt_test_1"]}]},
        {"evt_test_1": "我先afk一下"}, core.clock())
    assert not core.store.rows("SELECT * FROM jargon_terms")
    assert not jargon.matches("AFK", "CAFKX")


def test_legacy_same_key_meanings_stay_separate_and_ambiguous_variant_is_omitted(core):
    say(core, 1, "我先AFK一下")
    for term, meaning in [("AFK", "离开"), ("afk", "主人自造的另一个意思")]:
        core.store.execute("INSERT INTO jargon_terms "
            "(term,refs_json,uses,state,meaning,created_at,updated_at) "
            "VALUES (%s,%s,4,'INFERRED',%s,%s,%s)",
            (term, encode(["evt_test_1"]), meaning, core.clock(), core.clock()))
    assert jargon.context(core.store, "我先AFK一下", 3)[0] == [{"term": "AFK", "meaning": "离开"}]
    assert not jargon.context(core.store, "我先ＡＦＫ一下", 3)[0]
    assert len(core.store.rows("SELECT * FROM jargon_terms")) == 2
