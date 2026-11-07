from tests.support.next_memes_animation import canvas_gif
from tests.support.next_memes_catalog import check, png
from tests.support.next_memes_semantic import add, ready, step

CASES = [
    ("comfort", "难过了，想要抱抱", "comfort", "recipient", ["warm"]),
    ("no_mockery", "安慰我，别嘲笑我搞砸工作", "comfort", "recipient", ["warm"]),
    ("self_deprecation", "自嘲把工作搞砸了", "playful", "self", ["self"]),
    ("no_target_swap", "自嘲把工作搞砸了", "playful", "neutral", []),
    ("text_conflict", "文字写着你真笨活该", "comfort", "recipient", []),
    ("gif_reversal", "从抱抱突然转为嘲笑", "like", "recipient", []),
    ("no_match", "明天天气预报温度", "comfort", "recipient", []),
]


def check_caption_fixture_quality(semantic, name, text, intent, target, expected):
    store, _ = semantic
    labels = {}
    labels["warm"] = add(semantic)
    labels["self"] = add(
        semantic,
        png("blue"),
        description="我把工作搞砸了，自嘲一下自己",
        visible_text="我是笨蛋",
        tone="self_deprecating",
        target="self",
        suitable=["playful"],
        tags=["自嘲", "工作"],
        use_when=["自己犯小失误"],
    )
    labels["mock"] = add(
        semantic,
        png("red"),
        description="嘲笑接收者把工作搞砸了",
        visible_text="你真笨，活该",
        tone="mocking",
        target="recipient",
        suitable=["comfort", "playful"],
        tags=["工作", "嘲笑", "安慰"],
    )
    labels["conflict"] = add(
        semantic,
        png("yellow"),
        visible_text="你真笨活该",
        description="和善地陪伴与拥抱",
        tone="warm",
    )
    labels["reversal"] = add(
        semantic,
        canvas_gif([(24, 24), (24, 24)]),
        description="从抱抱突然转为嘲笑",
        suitable=["like"],
        temporal_status="changes",
    )
    for label, sha in labels.items():
        if label in {"conflict", "reversal"}:
            step(semantic, sha)
            assert step(semantic, sha)["state"] == "QUARANTINED"
        else:
            ready(semantic, sha)
    result = store.candidates(name, {"text": text, "intent": intent, "target": target}, check=check)
    assert [row["candidate_id"] for row in result] == [labels[label] for label in expected]
