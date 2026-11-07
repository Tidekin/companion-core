"""Interest-driven discovery: topics, owner terms, choice, wire query and dedup."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from companion import topic_selection as ts
from companion.store import after
from source_reader.bilibili import BilibiliReader
from source_reader.network import ReadFailure
from source_reader.protocol import decode_lease
from tests.support.exploration import configure
from tests.test_watch_ledger import observe


class Rng:
    def __init__(self, *values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0)


def judged(core, number, tags, rating, *, days_ago=0, url=None):
    event_id = observe(core, number, url=url)
    at = after(core.clock(), -days_ago * 86400)
    core.store.execute(
        "INSERT INTO watch_ledger(entry_id,source_event_id,platform,item_ref,depth,state,rating,"
        "take,topic_tags_json,created_at,updated_at) VALUES(?,?,'bilibili',?,'text','JUDGED',"
        "?,'看过',?,?,?)", (f"watch_{number}", event_id, url or event_id, rating,
                           json.dumps(tags, ensure_ascii=False), at, at))
    return event_id


def test_clean_keeps_short_plain_terms_only():
    assert ts.clean("  独立 游戏！ ") == "独立 游戏"
    for bad in ("猫", "https example", "一二三四五六七八九十一二三", "联系13812345678", None, 3):
        assert ts.clean(bad) is None


def test_interest_topics_decay_and_dislikes_drop_out(core):
    configure(core)
    judged(core, 1, ["猫", "猫咪"], 2)
    judged(core, 2, ["猫咪", "游戏"], 1, days_ago=14)
    judged(core, 3, ["恐怖片"], -2)
    judged(core, 4, ["旧话题"], 2, days_ago=40)
    topics = dict(ts.interest_topics(core.store, core.clock()))
    assert set(topics) == {"猫咪", "游戏"}  # One-character tags fail clean; -2 scores 0.
    assert topics["猫咪"] == pytest.approx(3 + 2 * 0.25)
    assert topics["游戏"] == pytest.approx(0.5)


def test_erased_sources_leave_the_interest_projection(core):
    configure(core)
    event_id = judged(core, 5, ["独立游戏"], 2)
    with core.store.transaction():
        core.ledger.expire_events(core.store, [event_id], core.clock())
    assert ts.interest_topics(core.store, core.clock()) == []


def test_owner_terms_strip_preference_phrasing(core, monkeypatch):
    rows = [{"content": "主人很喜欢看独立游戏"}, {"content": "喜欢猫"}, {"content": "爱看科幻电影"}]
    monkeypatch.setattr("companion.interest.profile",
                        lambda *a: {"owner.preference": rows, "self.interest": []})
    assert ts.owner_terms(core.store, core.settings, core.clock()) == ["独立游戏", "科幻电影"]


def test_owner_terms_need_the_memory_purpose_under_owner_authority(core, monkeypatch):
    from companion.contracts import CoreError

    def refuse(*args, **kwargs):
        raise CoreError("OWNER_AUTHORITY_PURPOSE_REQUIRED", 403)

    monkeypatch.setattr(ts.owner_authority, "required", lambda *a: True)
    monkeypatch.setattr(ts.owner_authority, "current", refuse)
    assert ts.owner_terms(core.store, core.settings, core.clock()) == []


def test_choose_splits_shares_rotates_and_falls_back(core, monkeypatch):
    configure(core)
    monkeypatch.setattr(ts, "owner_terms", lambda *a: ["科幻电影"])
    judged(core, 6, ["独立游戏"], 2)
    adapters = ["bili", "heybox"]
    assert ts.choose(core.store, core.settings, core.clock(), adapters, Rng(0.6)) is None
    assert ts.choose(core.store, core.settings, core.clock(), adapters, Rng(0.1, 0.0)) == (
        "bili", "独立游戏")
    with core.store.transaction():
        core.exploration.create("bili", "discover", "earlier", query="独立游戏")
    assert ts.choose(core.store, core.settings, core.clock(), adapters, Rng(0.1, 0.0)) == (
        "bili", "科幻电影")
    assert ts.choose(core.store, core.settings, core.clock(), ["heybox"], Rng(0.1)) is None


def test_wake_creates_a_queried_bili_discover_that_only_reading_leases_carry(core, monkeypatch):
    configure(core)
    judged(core, 7, ["独立游戏"], 2)
    monkeypatch.setattr(ts, "owner_terms", lambda *a: [])
    monkeypatch.setattr(ts, "random", Rng(0.1, 0.0))
    core.exploration.schedule_wake([{"decision": "OBSERVE", "decision_id": "wake-q"}])
    row = core.store.one("SELECT adapter_id,operation,url,query FROM exploration_requests "
                         "WHERE request_key='wake-q'")
    assert dict(row) == {"adapter_id": "bili", "operation": "discover", "url": "",
                         "query": "独立游戏"}
    assert core.exploration.lease("bili", reading=True)["query"] == "独立游戏"


def test_queries_are_only_for_discover_requests(core):
    from companion.contracts import CoreError

    configure(core)
    with pytest.raises(CoreError, match="EXPLORATION_QUERY_INVALID"), core.store.transaction():
        core.exploration.create("bili", "read", "bad", url="https://www.bilibili.com/video/"
                                "BV1xx411c7mD", query="独立游戏")


NOW = datetime(2026, 10, 29, tzinfo=UTC)


def lease(**changes):
    return {"item": {
        "schema_version": 1, "request_id": "req_query", "lease_token": "lease_query_1",
        "lease_until": (NOW + timedelta(seconds=60)).isoformat(), "operation": "discover",
        "url": "", "max_bytes": 1024, "max_requests": 1, "max_items": 5, "allowed_hosts": [],
        "context": {"root_task_id": "root", "source": "bilibili", "source_event_ids": [],
                    "privacy_epoch": 1}, **changes}}


def test_reader_lease_accepts_a_bounded_discover_query_only():
    assert decode_lease(lease(query="独立游戏"), "bilibili", NOW)[0]["query"] == "独立游戏"
    assert "query" not in decode_lease(lease(), "bilibili", NOW)[0]
    for bad in ({"query": "x"}, {"query": "a\x00b"}, {"query": "q" * 101}, {"query": 3},
                {"query": "独立游戏", "operation": "read",
                 "url": "https://www.bilibili.com/video/BV1xx411c7mD"}):
        with pytest.raises(ReadFailure, match="READER_LEASE_INVALID"):
            decode_lease(lease(**bad), "bilibili", NOW)


class Public:
    def __init__(self, body):
        self.body, self.urls = body, []

    async def get(self, url, budget, purpose):
        self.urls.append((url, purpose))
        return json.dumps(self.body).encode()


def test_native_reader_searches_with_the_query_and_keeps_popular_without_it():
    found = {"code": 0, "data": {"result": [
        {"type": "video", "bvid": "BV1xx411c7mD", "title": "<em class=\"keyword\">独立</em>游戏"},
        {"type": "video", "bvid": "bad", "title": "skip"}]}}
    public = Public(found)
    items = asyncio.run(BilibiliReader(public).read(
        {"operation": "discover", "url": "", "max_items": 5, "query": "独立游戏"}, None))
    assert items == [{"url": "https://www.bilibili.com/video/BV1xx411c7mD",
                      "title": "独立游戏", "text": ""}]
    assert "search/type" in public.urls[0][0] and "keyword=" in public.urls[0][0]
    popular = Public({"code": 0, "data": {"list": []}})
    asyncio.run(BilibiliReader(popular).read(
        {"operation": "discover", "url": "", "max_items": 5}, None))
    assert "web-interface/popular" in popular.urls[0][0]


def test_watched_items_are_not_proposed_as_new_reads(core):
    from companion.exploration import ReadItem, ReadResult

    configure(core)
    url = "https://www.bilibili.com/video/BV1xx411c7mD"
    judged(core, 8, ["独立游戏"], 1, url=url)
    with core.store.transaction():
        request = core.exploration.create("bili", "discover", "wake-dedup")
    token = core.exploration.lease("bili")["lease_token"]
    core.exploration.complete("bili", request, ReadResult(
        lease_token=token, status="SUCCESS", used_bytes=100,
        items=[ReadItem(title="已经看过的视频", url=url)]))
    assert core.store.one("SELECT 1 FROM goals WHERE json_extract(scope_json,'$.url')=?",
                          (url,)) is None
