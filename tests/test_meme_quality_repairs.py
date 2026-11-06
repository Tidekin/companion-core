"""Meme retrieval repairs, exercised from raw owner text and the real offer entry."""

import json
from types import SimpleNamespace

import httpx
import pytest

from companion import meme_policy
from companion.contracts import CoreError
from companion.expression import ExpressionEngine
from companion.meme_retrieval import OFFER, _candidate, query_for
from tests.conftest import run
from tests.meme_authority_fixture import admit
from tests.test_meme_core import worker
from tests.test_meme_core_runtime import runtime_case as runtime_case
from tests.test_meme_native_discovery import native_case, release_foreground
from tests.test_meme_pack_files import Upstream


@pytest.mark.parametrize("text,intent,target,request_kind", [
    ("给我发个表情包", "playful", "recipient", "any"),
    ("来个搞笑表情包", "playful", "recipient", "themed"),
    ("来个开心的表情包", "celebrate", "recipient", "themed"),
    ("哈哈哈哈笑死我了", "playful", "recipient", None),
    ("我好开心", "celebrate", "recipient", None),
    ("太棒了，又成功了", "celebrate", "recipient", None),
    ("我一点也不开心", None, "recipient", None),
    ("我不想睡觉了", None, "recipient", None),
    ("不喜欢这个", None, "recipient", None),
    ("太棒了，又加班到凌晨", None, "recipient", None),
    ("你可真棒，又答错了", None, "recipient", None),
    ("自嘲把工作搞砸了", "playful", "self", None),
    ("他在自嘲", "playful", "third_party", None),
    ("别发表情包了", None, "recipient", None),
    ("认真说，给我发个表情包", None, "recipient", None),
])
def test_production_query_parses_raw_owner_text(text, intent, target, request_kind):
    query = query_for(text)
    assert (query["intent"], query["target"], query["request"]) == (
        intent, target, request_kind)


def caption(**changes):
    value = {
        "description": "我把工作搞砸了，自嘲一下自己", "visible_text": "我是笨蛋",
        "tone": "self_deprecating", "target": "self", "suitable": ("playful",), "avoid": (),
        "use_when": ("自己犯小失误",), "avoid_when": (), "tags": ("自嘲", "工作"),
        "confidence": 0.95, "content_status": "clear", "temporal_status": "static",
    }
    return SimpleNamespace(**{**value, **changes})


def test_self_deprecation_reaches_candidates_through_the_production_query():
    own = query_for("自嘲把工作搞砸了")
    assert meme_policy.context_allows(caption(), own)
    assert meme_policy.lexical_score(caption(), own) > 0
    # Pointing a self-deprecating image at someone else stays refused.
    for text in ("他在自嘲", "你把工作搞砸了"):
        assert not meme_policy.context_allows(caption(), query_for(text))


def test_explicit_request_without_theme_ranks_any_legal_light_sticker():
    warm = caption(description="小猫抱抱", visible_text="", tone="warm", target="recipient",
                   suitable=("comfort",), use_when=(), tags=("猫",))
    query = query_for("给我发个表情包")
    assert meme_policy.context_allows(warm, query)
    assert meme_policy.lexical_score(warm, query) == meme_policy.EXPLICIT_FLOOR
    # Ordinary turns still require overlap or semantic similarity.
    assert meme_policy.lexical_score(warm, query_for("想要温暖的抱抱")) > 0
    assert not meme_policy.context_allows(warm, query_for("哈哈哈哈笑死我了"))
    assert not meme_policy.context_allows(caption(suitable=()), query)


def test_final_candidate_card_carries_use_and_avoid_conditions():
    value = caption(use_when=("下班后吐槽", "自己犯小失误", "第三条"),
                    avoid_when=("对方刚被辞退", "严肃道歉", "第三条"))
    description = _candidate("a" * 64, value, {"category": "playful"})["description"]
    assert "适用：下班后吐槽；自己犯小失误" in description
    assert "避免：对方刚被辞退；严肃道歉" in description and "第三条" not in description
    # Negative conditions never add recall.
    assert "对方刚被辞退" not in meme_policy.caption_text(value)


def erase_marker(store, event_id):
    store.execute("INSERT INTO runtime_meta(key,value) VALUES (?,?)",
                  ("erased_event:" + event_id, "{}"))


@pytest.mark.parametrize("change", ["receipt", "erased"])
def test_unavailable_inventory_source_leaves_the_current_chat_running(runtime_case, change):
    c = runtime_case
    s = c.core.store
    if change == "receipt":
        # The original receipt was cleaned while the staged asset still waits for cleanup.
        s.execute("DELETE FROM runtime_meta WHERE key=?", ("owner_input:" + c.source["event_id"],))
    else:
        erase_marker(s, c.source["event_id"])
    run(c.service.prepare_offer(c.job))
    saved = c.retrieval.check(c.job)
    assert saved["offer"]["candidates"] == [] and saved["catalog_bindings"] == {}
    audit = s.one("SELECT safe_detail_json FROM audit_log WHERE action='meme.offer_empty'")
    assert json.loads(audit["safe_detail_json"])["reason"] == "NO_ASSETS"
    assert json.loads(audit["safe_detail_json"])["unavailable"] == 1
    offered, offer = ExpressionEngine(s, c.core.settings, c.core.clock)._offer(c.job["event_id"])
    assert offered and offer.candidates == []


def test_frozen_candidate_withdrawn_later_still_stops_its_use(runtime_case):
    c = runtime_case
    run(c.service.prepare_offer(c.job))
    assert c.retrieval.check(c.job)["catalog_bindings"]
    erase_marker(c.core.store, c.source["event_id"])
    with pytest.raises(CoreError):
        c.retrieval.check(c.job)
    with pytest.raises(CoreError):
        ExpressionEngine(c.core.store, c.core.settings, c.core.clock).instruction(
            c.job["event_id"])
    # A later prepare of the same turn keeps its frozen contract instead of re-ranking.
    with pytest.raises(CoreError):
        run(c.service.prepare_offer(c.job))
    assert c.core.store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                            (OFFER + c.job["event_id"],))


def test_second_discovery_reaches_new_files_without_downloading_held_ones(
    core, tmp_path, monkeypatch
):
    async def scenario():
        # Two attempts each reserve the per-attempt ceilings against the daily quotas.
        async with native_case(core, tmp_path, monkeypatch,
                               meme_discovery_daily_download_bytes=32 * 1024 * 1024,
                               meme_discovery_daily_storage_bytes=8 * 1024 * 1024) as c:
            c.upstream = Upstream(count=5)
            c.service.discovery.transport = httpx.MockTransport(c.upstream.handle)
            await c.retrieval.prepare(c.job)
            release_foreground(c)
            assert await c.service.run_once("first-discovery")
            first = set(c.service.discovery._held(c.event["event_id"]))
            assert len(first) == 3
            first_calls = list(c.upstream.calls)
            calls = []
            for _ in range(3):  # The real worker captions staged files before more discovery.
                assert await worker(core, calls)

            event, response = await admit(c.client, c.config, c.grant, c.now, 2,
                                          text="太棒了，又成功了")
            assert response.status_code == 202, response.text
            job = core.claim_job("second-offer", job_id=response.json()["job_id"])
            second = core.store.one("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],))
            # Held captions do not fit this intent: the library lacks relevant material.
            await c.retrieval.prepare(second)
            assert core.store.one("SELECT 1 FROM jobs WHERE kind='meme_discovery' "
                                  "AND event_id=?", (event["event_id"],))
            core.store.execute("UPDATE jobs SET state='SUCCEEDED',lease_token=NULL "
                               "WHERE job_id=?", (second["job_id"],))
            assert await c.service.run_once("second-discovery")
            held = set(c.service.discovery._held(event["event_id"]))
            assert len(held) == 5 and first < held
            fetched = c.upstream.calls[len(first_calls):]
            assert not set(fetched) & first

    run(scenario())


def test_one_queued_discovery_at_a_time(core, tmp_path, monkeypatch):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch) as c:
            await c.retrieval.prepare(c.job)
            event, response = await admit(c.client, c.config, c.grant, c.now, 2,
                                          text="恭喜，搞定了")
            assert response.status_code == 202, response.text
            release_foreground(c)
            job = core.claim_job("second-offer", job_id=response.json()["job_id"])
            second = core.store.one("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],))
            await c.retrieval.prepare(second)
            jobs = core.store.rows("SELECT event_id FROM jobs WHERE kind='meme_discovery'")
            assert [row["event_id"] for row in jobs] == [c.event["event_id"]]
            reasons = [json.loads(row["safe_detail_json"])["reason"] for row in core.store.rows(
                "SELECT safe_detail_json FROM audit_log WHERE action='meme.offer_empty'")]
            assert reasons == ["NO_ASSETS", "NO_ASSETS"]

    run(scenario())


@pytest.mark.parametrize("text,intent,mood", [
    ("周末去公园走走", "ambient", None),
    ("今天好累", "ambient", "low"),
    ("嗯", None, None),
    ("/help", None, None),
    ("https://example.com/a", None, None),
    ("我一点也不开心", None, None),
    ("太棒了，又加班到凌晨", None, None),
    ("我妈住院了", None, None),
])
def test_cue_less_light_turns_become_ambient(text, intent, mood):
    query = query_for(text)
    assert (query["intent"], query["mood"]) == (intent, mood)


def test_ambient_menu_keeps_light_tones_and_reads_low_mood():
    warm_comfort = caption(description="小猫抱抱", visible_text="", tone="warm",
                           target="recipient", suitable=("comfort", "like"), tags=("猫",))
    cheer = caption(description="欢呼", visible_text="", tone="celebratory", target="neutral",
                    suitable=("celebrate",), tags=("欢呼",))
    light, low = query_for("周末去公园走走"), query_for("今天好累")
    assert not meme_policy.context_allows(warm_comfort, light)
    assert meme_policy.context_allows(cheer, light)
    assert meme_policy.context_allows(warm_comfort, low)
    assert not meme_policy.context_allows(cheer, low)
    for query in (light, low):
        assert not meme_policy.context_allows(caption(), query)  # self-deprecating
    assert meme_policy.lexical_score(cheer, light) == meme_policy.AMBIENT_FLOOR


@pytest.mark.parametrize("runtime_case", ["ambient_low"], indirect=True)
def test_low_mood_ambient_turn_offers_the_gentle_sticker_as_optional(runtime_case):
    c = runtime_case
    run(c.service.prepare_offer(c.job))
    saved = c.retrieval.check(c.job)
    assert saved["offer"]["intent"] == "ambient"
    assert [x["candidate_id"] for x in saved["offer"]["candidates"]] == [c.item["asset_sha256"]]
    _, data = ExpressionEngine(c.core.store, c.core.settings, c.core.clock).instruction_parts(
        c.job["event_id"])
    assert "可选点缀" in data


@pytest.mark.parametrize("runtime_case", ["ambient"], indirect=True)
def test_ambient_miss_is_routine_and_lost_draw_stays_text_only(runtime_case, monkeypatch):
    c = runtime_case
    run(c.service.prepare_offer(c.job))
    saved = c.retrieval.check(c.job)
    # The only asset is comfort-first, which a plain light turn does not get.
    assert saved["offer"]["intent"] == "ambient" and saved["offer"]["candidates"] == []
    assert not c.core.store.one("SELECT 1 FROM audit_log WHERE action='meme.offer_empty'")
    assert not c.core.store.one("SELECT 1 FROM jobs WHERE kind='meme_discovery' AND event_id=?",
                                (c.job["event_id"],))
    from companion import expression

    monkeypatch.setattr(expression, "selection_draw", lambda *args: False)
    with pytest.raises(CoreError):
        c.retrieval.check(c.job)  # The frozen ambient offer no longer matches a lost draw.


@pytest.mark.parametrize("runtime_case", ["ambient"], indirect=True)
def test_lost_ambient_draw_freezes_a_null_offer_without_scanning(runtime_case, monkeypatch):
    from companion import expression

    c = runtime_case
    monkeypatch.setattr(expression, "selection_draw", lambda *args: False)
    run(c.service.prepare_offer(c.job))
    saved = c.retrieval.check(c.job)
    assert saved["offer"] is None and saved["ranking"]["header"]["scanned"] == 0
