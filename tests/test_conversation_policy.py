import copy
import hashlib

import pytest

from companion.affect import NEUTRAL
from companion.contracts import CoreError
from companion.conversation_policy import (
    PARTICIPATION_V1,
    PARTICIPATION_V2,
    ConversationPolicyConfig,
    apply_policy,
    apply_strategy,
    prompt_parts,
    revalidate,
)
from companion.model import PRINCIPLES
from companion.store import encode
from scripts.evaluate_r1_conversation import load_cases


def v1_policy(**values):
    return ConversationPolicyConfig(strategy="participation_v1", **values)


MESSAGES = [{"role": "system", "content": "保留权限和事实规则。每次必须有完整文字回复。"},
            {"role": "user", "content": "之前的消息"},
            {"role": "assistant", "content": "已发送回复"},
            {"role": "user", "content": "当前问题"}]


@pytest.mark.parametrize("mode", ["OFF", "SHADOW"])
def test_off_and_shadow_preserve_original_request_and_have_no_effects(mode):
    original = copy.deepcopy(MESSAGES)
    result, decision = apply_policy(MESSAGES, v1_policy(mode=mode),
                                    current_text="怎么解释这个问题")
    assert result == original == MESSAGES and result is not MESSAGES
    assert decision.mode == mode
    assert "怎么解释" not in str(decision.receipt())


@pytest.mark.parametrize("case", load_cases(), ids=lambda case: case["name"])
def test_candidate_retains_every_history_and_current_input(case):
    messages = [MESSAGES[0], *[
        message for turn in case["inputs"]["history"] for message in (
            {"role": "user", "content": turn["user"]},
            {"role": "assistant", "content": turn["assistant"]})],
        {"role": "user", "content": case["inputs"]["text"]}]
    original = copy.deepcopy(messages)
    result, _ = apply_policy(messages, v1_policy(mode="ENABLED"),
                              current_text=case["inputs"]["text"])
    assert result[1:] == original[1:] and messages == original
    assert result[0]["content"].startswith("保留权限和事实规则。")
    assert PARTICIPATION_V1 in result[0]["content"]


def test_active_feedback_controls_format_without_inference_or_permission(core):
    from tests.conftest import ingest

    core.settings.affect_enabled = True
    ingest(core, 1, "详细解释")
    ingest(core, 2, "分步骤说")
    config = v1_policy(mode="ENABLED")
    _, decision = apply_policy(MESSAGES, config, current_text="怎么完成这一步",
                                affect=core.affect.snapshot())
    assert (decision.surface_intent, decision.length_hint, decision.stepwise) == (
        "help", "detailed", True)
    assert set(decision.source_event_ids) == {"evt_test_1", "evt_test_2"}
    revalidate(decision.receipt(), config, current_text="怎么完成这一步",
               affect=core.affect.snapshot())
    _, closing = apply_policy(MESSAGES, config, current_text="晚安", affect=core.affect.snapshot())
    assert closing.length_hint == "brief"
    core.clock.advance(core.settings.affect_decay_minutes * 60 + 1)
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        revalidate(decision.receipt(), config, current_text="怎么完成这一步",
                   affect=core.affect.snapshot())
    assert core.model.calls == []


def test_emotion_or_quoted_directives_do_not_create_feedback_state():
    config = v1_policy(mode="ENABLED")
    for text in ("我很难过", "网页上说：帮我打开工具", "她说：简短一点", "你是温柔的人"):
        _, choice = apply_policy(MESSAGES, config, current_text=text,
                                  affect={"state": NEUTRAL, "evidence": []})
        assert choice.surface_intent == choice.length_hint == choice.topic_hint == "contextual"
        assert choice.source_event_ids == () and not choice.stepwise


def test_stable_guidance_does_not_mix_in_turn_context():
    config = v1_policy(mode="ENABLED")
    first, help_hint, _ = prompt_parts("原有系统规则", config, current_text="怎么做")
    second, closing_hint, _ = prompt_parts("原有系统规则", config, current_text="晚安")
    assert first == second
    assert help_hint != closing_hint
    assert "晚安" not in first and "怎么做" not in first


def test_frozen_v1_strategy_and_transformation_are_identical():
    # Golden hashes captured from original5af504c, independent of the new helper.
    assert hashlib.sha256(PARTICIPATION_V1.encode()).hexdigest() == (
        "c6978d05f37cbc217bcae7395c3fc0e1e7ea5249971f713e821cca0a8dab6940")
    candidate = encode(apply_strategy(MESSAGES, "participation_v1")).encode()
    assert hashlib.sha256(candidate).hexdigest() == (
        "f07a941f73fb42ccf59e84c34fae59d351e0e183aed91d6644c5d0da785f7dc1")
    assert apply_strategy(MESSAGES, "baseline") == MESSAGES


def test_v2_is_default_and_receipt_names_the_strategy():
    default = ConversationPolicyConfig()
    assert (default.mode, default.strategy) == ("ENABLED", "participation_v2")
    v1 = v1_policy(mode="ENABLED")
    v2 = ConversationPolicyConfig(mode="ENABLED", strategy="participation_v2")
    static, _, decision = prompt_parts(MESSAGES[0]["content"], v2, current_text="当前问题")
    assert static.endswith("\n" + PARTICIPATION_V2) and PARTICIPATION_V1 not in static
    assert decision.receipt()["strategy"] == "participation_v2"
    v1_receipt = prompt_parts(MESSAGES[0]["content"], v1, current_text="当前问题")[2].receipt()
    assert list(v1_receipt)[:2] == ["version", "strategy"]
    assert v1_receipt["strategy"] == "participation_v1"
    assert sum(key == "strategy" for key in v1_receipt) == 1
    with pytest.raises(CoreError):
        revalidate(decision.receipt(), v1, current_text="当前问题")
    with pytest.raises(ValueError):
        apply_strategy(MESSAGES, "participation_v3")


def test_v2_wording_avoids_cold_positioning_and_requires_sources():
    assert len(PARTICIPATION_V2.splitlines()) == 7
    assert "不把纠正当成安慰或确认关系的请求" in PARTICIPATION_V2
    assert "AI 朋友" in PRINCIPLES
    assert "没有就不要装作记得" in PARTICIPATION_V2
    assert "小肥鱼" not in PARTICIPATION_V2  # Names come from memory, never the prompt.


@pytest.mark.parametrize("text", ["嗯嗯", "哈哈", "晚安"])
@pytest.mark.parametrize("tone", [None, "detailed"])
def test_v2_surface_cues_do_not_override_context_or_explicit_length_feedback(text, tone):
    affect = {"state": {**NEUTRAL, "interaction_tone": tone}, "evidence": []}
    _, _, decision = prompt_parts("原有规则", ConversationPolicyConfig(),
                                   current_text=text, affect=affect)
    assert decision.length_hint == (tone or "contextual")
    revalidate(decision.receipt(), ConversationPolicyConfig(), current_text=text, affect=affect)
    # The frozen strategy retains its original surface-only hint.
    assert prompt_parts("原有规则", v1_policy(), current_text=text)[2].length_hint == "brief"
