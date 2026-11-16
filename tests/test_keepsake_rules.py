"""Excerpts are deterministic pieces of the owner's own words."""

from companion import keepsakes


def test_excerpt_keeps_the_sentences_about_the_memory_in_original_order():
    text = "今天好累啊。对了我的生日是三月五号，别忘了！晚上吃什么呢？"
    memory = "主人的生日是三月五号"
    assert keepsakes.excerpt(text, memory, 120) == "对了我的生日是三月五号，别忘了！"
    assert keepsakes.excerpt(text, "主人的生日是三月五号", 120) == keepsakes.excerpt(
        text, "主人的生日是三月五号", 120)


def test_excerpt_takes_at_most_two_sentences_and_respects_the_limit():
    text = "我在杭州上班。杭州的冬天很冷。我养了一只猫。杭州西湖很美。"
    cut = keepsakes.excerpt(text, "主人在杭州上班，觉得杭州冬天冷", 120)
    assert cut == "我在杭州上班。杭州的冬天很冷。"
    assert len(keepsakes.excerpt(text * 20, "主人在杭州上班", 16)) <= 16


def test_unrelated_text_keeps_nothing():
    assert keepsakes.excerpt("今天天气不错。", "主人的生日是三月五号", 120) == ""
    assert keepsakes.excerpt("", "主人的生日是三月五号", 120) == ""


def test_trimmed_body_names_the_original_input_only_when_its_excerpt_matches():
    body = {"text": "我的生日是三月五号", "keepsake": {
        "excerpt_sha256": keepsakes.digest("我的生日是三月五号"), "input_sha256": "a" * 64}}
    assert keepsakes.kept_input(body) == "a" * 64
    assert keepsakes.kept_input({**body, "text": "改过的内容"}) is None
    assert keepsakes.kept_input({"text": "普通消息"}) is None
