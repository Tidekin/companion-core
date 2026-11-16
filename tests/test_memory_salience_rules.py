"""Recall salience is pure ordering math; no store, model or clock."""

import pytest

from companion import memory_salience as salience

NOW = "2026-11-16T00:00:00.000000+00:00"


def days_ago(days, hours=0):
    from datetime import datetime, timedelta

    return (datetime.fromisoformat(NOW) - timedelta(days=days, hours=hours)).isoformat()


def test_one_fresh_mention_counts_fully_and_fades_over_thirty_days():
    assert salience.vividness([NOW], NOW) == pytest.approx(1)
    assert salience.vividness([days_ago(30)], NOW) == pytest.approx(.5)
    assert salience.vividness([days_ago(181)], NOW) == 0
    assert salience.vividness([], NOW) == 0


def test_repeats_within_a_day_are_dulled_and_strength_is_capped():
    same_day = [days_ago(0, h) for h in (3, 2, 1)]
    assert salience.vividness(same_day, NOW) == pytest.approx(
        sum(.5 ** k * 2 ** (-h * 3600 / salience.MENTION_HALF_LIFE)
            for k, h in enumerate((3, 2, 1))))
    spread = [days_ago(d) for d in (3, 2, 1)]
    assert salience.vividness(spread, NOW) > salience.vividness(same_day, NOW) - .3
    many = [days_ago(d) for d in range(0, 40, 2)]
    assert salience.vividness(many, NOW) == salience.VIVID_CAP


def test_mood_congruence_is_positive_cosine_scaled_by_mood_strength():
    wronged = {"pleasure": -.1, "arousal": .05, "dominance": -.07}
    assert salience.congruence(wronged, [-.04, .02, -.03]) > .9
    assert salience.congruence(wronged, [.05, .02, .01]) == 0
    faint = {key: value / 10 for key, value in wronged.items()}
    assert salience.congruence(faint, [-.04, .02, -.03]) < .2
    assert salience.congruence(None, [-.04, .02, -.03]) == 0
    assert salience.congruence(wronged, None) == 0


def test_bonuses_stay_below_a_few_reciprocal_rank_steps():
    step = 1 / 61 - 1 / 62
    assert salience.VIVID_WEIGHT + salience.MOOD_WEIGHT < 6 * step


@pytest.mark.parametrize(("text", "content", "expected"), [
    ("我下周要去杭州出差", "主人下周要去杭州出差", True),
    ("杭州好玩吗", "主人下周要去杭州出差", True),
    ("我生日快到了", "主人的生日是3月5日", True),
    ("I booked the GPU", "主人在用 gpu 训练模型", True),
    ("今天天气不错", "主人下周要去杭州出差", False),
    ("我们今天聊点什么", "主人今天加班到很晚", False),
    ("", "主人下周要去杭州出差", False),
])
def test_only_owner_words_that_share_terms_count_as_a_mention(text, content, expected):
    assert salience.mentioned(text, content) is expected
