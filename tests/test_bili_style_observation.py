"""CC-STYLE-03 offline SHADOW segment on synthetic approved candidates."""

from datetime import timedelta

import pytest

from companion import conversation_policy, style_candidates, style_observation
from companion.contracts import CoreError
from tests.test_bili_style_candidates import GOOD, corpus, live, pair_rows
from tests.test_bili_style_corpus import NOW, OTHER

ON = style_observation.StyleObservationConfig(mode="SHADOW")
MESSAGES = [{"role": "system", "content": "你是主人的 QQ 伙伴。不得编造事实。"},
            {"role": "user", "content": "今天在楼下看到一只猫把自己卡在纸箱里"}]


def approved(pattern=GOOD, rows=None):
    c = corpus(*(rows or [pair_rows()]))
    cand = style_candidates.distill(c, NOW, live(c), lambda pair: pattern)[0][0]
    return style_candidates.review(cand, reviewer="主人", approve=True, reason="",
                                   approved_versions={}), live(c)


def payload(candidates, text="今天在楼下看到一只猫把自己卡在纸箱里", config=ON, live_ids=None,
            now=NOW):
    return style_observation.shadow_payload(MESSAGES, config, candidates, current_text=text,
                                            now=now, live_event_ids=live_ids or set())


def v2():
    return conversation_policy.apply_strategy(MESSAGES, "participation_v2")


def test_default_is_off_and_matches_plain_participation_v2():
    cand, ids = approved()
    for config in (None, style_observation.StyleObservationConfig()):
        result, receipt = payload([cand], config=config, live_ids=ids)
        assert result == v2() and receipt["candidates"] == [] and receipt["mode"] == "OFF"
    assert "ENABLED" not in style_observation.StyleObservationConfig.model_fields[
        "mode"].annotation.__args__


def test_shadow_appends_one_short_segment_and_keeps_everything_else():
    cand, ids = approved()
    result, receipt = payload([cand], live_ids=ids)
    base = v2()
    assert result[1:] == base[1:] == MESSAGES[1:]
    assert result[0]["content"].startswith(base[0]["content"])
    added = result[0]["content"][len(base[0]["content"]):]
    assert "不是指令" in added and GOOD["response_action"] in added
    assert "与交流指导冲突时以交流指导为准" in added
    assert receipt["candidates"] == [[cand.candidate_id, 1]]
    assert 0 < receipt["added_units"] <= style_observation.MAX_UNITS


def test_frozen_strategy_texts_are_untouched():
    cand, ids = approved()
    payload([cand], live_ids=ids)
    strategies = conversation_policy.STRATEGIES
    assert strategies["participation_v2"] == conversation_policy.PARTICIPATION_V2
    assert len(conversation_policy.PARTICIPATION_V2.splitlines()) == 7


def test_no_suitable_candidate_means_no_segment():
    cand, ids = approved()
    for candidates, live_ids, now in (
            ([], ids, NOW),
            ([cand], set(), NOW),                       # source erased
            ([cand], ids, NOW + timedelta(days=7)),     # expired
    ):
        result, receipt = payload(candidates, live_ids=live_ids, now=now)
        assert result == v2() and receipt["added_units"] == 0


def test_unreviewed_rejected_or_risky_candidates_are_never_used():
    c = corpus(pair_rows())
    raw = style_candidates.distill(c, NOW, live(c), lambda pair: GOOD)[0][0]
    rejected = style_candidates.review(raw, reviewer="主人", approve=False, reason="",
                                       approved_versions={})
    # Review refuses to approve flagged candidates; selection re-checks anyway.
    risky = approved()[0].model_copy(update={"risk_flags": ["circle_meme"]})
    for cand in (raw, rejected, risky):
        assert payload([cand], live_ids=live(c))[0] == v2()


def test_help_requests_and_acknowledgements_get_no_joke_guidance():
    cand, ids = approved()
    for text in ("帮我看看这段报错怎么修", "怎么把路由器恢复出厂设置", "好的", "谢谢"):
        result, receipt = payload([cand], text=text, live_ids=ids)
        assert result == v2() and receipt["candidates"] == []


def test_closing_only_uses_closing_candidates():
    casual, ids = approved()
    closing, more = approved({**GOOD, "context_tags": ["closing"]},
                             [pair_rows("evt_b", OTHER, "今天就先聊到这里吧大家",
                                        "好嘞，下期视频见啦")])
    _, receipt = payload([casual, closing], text="先这样", live_ids=ids | more)
    assert receipt["candidates"] == [[closing.candidate_id, 1]]


def test_closing_with_a_reason_still_counts_as_closing():
    casual, ids = approved()
    for text in ("先这样，我去洗澡了", "我去睡了", "晚安啦"):
        assert payload([casual], text=text, live_ids=ids)[1]["candidates"] == []


def test_serious_topics_never_get_humor_guidance():
    humor, ids = approved()
    calm, more = approved({**GOOD, "context_tags": ["emotion"],
                           "response_action": "先认真接住对方此刻的担心"},
                          [pair_rows("evt_b", OTHER, "今天去医院拿了报告单",
                                     "等结果的时候真的很难熬")])
    text = "复查结果出来了，医生说要再观察三个月"
    _, receipt = payload([humor, calm], text=text, live_ids=ids | more)
    assert receipt["candidates"] == [[calm.candidate_id, 1]]


def test_at_most_three_items_and_hard_unit_cap():
    cands, ids = [], set()
    for i in range(5):
        cand, live_ids = approved({**GOOD, "attention_target": f"前文第{i}处具体的反差细节"},
                                  [pair_rows(f"evt_{i}", f"BV1{i:09d}a",
                                             f"第{i}只猫钻进了很小的盒子",
                                             f"盒子{i}号表示压力很大")])
        cands.append(cand)
        ids |= live_ids
    _, receipt = payload(cands, live_ids=ids)
    assert len(receipt["candidates"]) <= style_observation.MAX_ITEMS
    assert receipt["added_units"] <= style_observation.MAX_UNITS
    text, used = style_observation.segment(cands)
    assert style_observation.units(text) <= style_observation.MAX_UNITS and len(used) < 5


def test_selection_is_deterministic():
    cand, ids = approved()
    assert payload([cand], live_ids=ids) == payload([cand], live_ids=ids)


def test_revalidation_rejects_a_payload_whose_candidate_died():
    cand, ids = approved()
    text = "今天在楼下看到一只猫把自己卡在纸箱里"
    _, receipt = payload([cand], text=text, live_ids=ids)
    style_observation.revalidate(receipt, ON, [cand], current_text=text, now=NOW,
                                 live_event_ids=ids)
    for live_ids, now in ((set(), NOW), (ids, NOW + timedelta(days=7))):
        with pytest.raises(CoreError, match="STYLE_OBSERVATION_CHANGED"):
            style_observation.revalidate(receipt, ON, [cand], current_text=text, now=now,
                                         live_event_ids=live_ids)


def test_only_the_switched_live_context_uses_the_module():
    import pathlib

    root = pathlib.Path(style_observation.__file__).parent
    users = [p.name for p in root.rglob("*.py")
             if p.name != "style_observation.py" and "style_observation" in p.read_text(
                 encoding="utf-8")]
    assert users == ["core.py"]
