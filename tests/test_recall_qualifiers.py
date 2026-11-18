from companion import turn_recall


def test_recalled_qualification_follows_its_own_visible_context(core, monkeypatch):
    texts = [
        "我报名参加秋季接力赛。",
        "明天要给水杯换个盖子。",
        "咱们在给小说里的选手编领奖情节。",
        "回家路上买了瓶水。",
        "那不过是小说里的设定。",
    ]
    rows = [{"event_id": str(i), "record_sequence": i, "user_text": text, "reply_text": "好",
             "occurred_at": "2026-05-01T01:00:00+00:00"} for i, text in enumerate(texts)]
    missing = set()

    def candidates(store, settings, job, ids):
        kept = set(ids) - missing
        selected = [row for row in rows if row["event_id"] in kept]
        return selected, [[] for _ in selected]

    monkeypatch.setattr(turn_recall, "_candidates", candidates)
    nearby = turn_recall._nearby_turns(rows)
    assert nearby == {"4": ["2", "3"]}

    def recall(order, *, denied=(), byte_limit=32000, query="我真的得奖了吗？"):
        ranking = {"first": order, "hop": [], "nearby": nearby}
        with core.store.read_snapshot():
            result = turn_recall.reference(core.store, core.settings, {}, query, set(),
                                          lambda ids: not set(denied).intersection(ids),
                                          max_turns=3, max_bytes=byte_limit, ranked_ids=ranking)
        return result

    assert recall(["0", "4"])["event_ids"] == ["0"]
    pair = recall(["4", "2"])
    assert pair["event_ids"] == ["2", "4"]
    assert recall(["4", "2"], denied=["2"]) is None
    assert recall(["4", "2"], byte_limit=pair["cost_bytes"] - 1) is None
    missing.add("2")
    assert recall(["4", "2"]) is None
    assert recall(["4"], query="你还记得我说的“那不过是小说里的设定”吗？")["event_ids"] == ["4"]


def test_qualification_filter_keeps_specific_facts_and_independent_evidence():
    texts = ["这是我新的收货地址。", "那只是草案。正式活动在下周一。",
             "这只是说明。", "这只是补充。", "那不过是比喻。"]
    rows = [{"event_id": str(i), "user_text": t} for i, t in enumerate(texts)]
    rows[3]["quoted_text"] = "周五活动已经取消。"
    notes = [[], [], [{"kind": "附件", "text": "原始记录"}], [], []]
    ranked = [0, 1, 2, 3, 4]
    assert turn_recall._grounded(rows, notes, ranked, {}, "周五有何安排？") == [0, 1, 2, 3]
