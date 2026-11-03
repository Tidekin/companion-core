"""Toolkit checks without paid calls: authoring -> validation, rubric normalization."""

import json
import sys
from datetime import datetime
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import authoring  # noqa: E402
from authoring import Source, Story  # noqa: E402


def stories(tag):
    return [Story("A01", "偏好", "基本", [Source(f"{tag}喜欢绿茶。")], "我喜欢什么茶？", ["绿茶"]),
            Story("A02", "引用更正", "更正",
                  [Source(f"{tag}活动带3箱水。"),
                   Source("这里写错了，是5箱。", "quote",
                          quoted={"text": f"{tag}活动带3箱水。", "name": "我", "uin": "10001"})],
                  "带几箱水？", ["5箱"], ["3箱"], distractors=["隔壁活动带两箱。"])]


def make_round(tmp_path, personas=("P1", "P2")):
    root = tmp_path / "round"
    root.mkdir()
    (root / "round.json").write_text(json.dumps({"name": "t", "arms": {}}), "utf-8")
    summary = authoring.build(root / "bank", {p: stories(p) for p in personas},
                              start=datetime(2026, 1, 5, tzinfo=authoring.TZ), slots=160,
                              fillers=["今天没什么事。"])
    return root, summary


def test_authored_bank_validates_and_counts_come_from_the_bank(tmp_path, monkeypatch):
    root, summary = make_round(tmp_path)
    monkeypatch.setenv("MEMORY_ROUND", str(root))
    monkeypatch.setattr(sys, "argv", ["bank.py"])
    import bank
    from roundcfg import Round

    r = Round()
    problems, info = bank.validate(r)
    assert not problems, problems
    assert info["semantic"] == summary["semantic"] == 2 * 2 * 4 and info["personas"] == 2
    quoted = [row for row in (r.suite / "inputs/P1.jsonl").read_text("utf-8").splitlines()
              if '"quoted"' in row]
    row = json.loads(quoted[0])
    assert row["text"] == "这里写错了，是5箱。" and row["quoted"]["uin"] == "10001"
    assert any("-X1" in line for line in (r.suite / "inputs/P1.jsonl").read_text("utf-8")
               .splitlines())


def test_validation_reports_a_future_source(tmp_path, monkeypatch):
    root, _ = make_round(tmp_path, personas=("P1",))
    path = root / "bank/suite-v1/questions.jsonl"
    rows = [json.loads(s) for s in path.read_text("utf-8").splitlines()]
    rows[0]["now"] = "2026-01-01T00:00:00+08:00"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    monkeypatch.setenv("MEMORY_ROUND", str(root))
    import bank
    from roundcfg import Round

    problems, _ = bank.validate(Round())
    assert any("cutoff" in p or "source after question" in p for p in problems)


def test_rubric_v2_fails_on_unsupported_claims_only_when_present():
    from rubrics import normalize_v2

    case = {"required": ["5箱"], "forbidden": ["3箱"]}
    ok = normalize_v2({"key": "k", "pass": True, "missing": [], "violations": [],
                       "unsupported": []}, case)
    bad = normalize_v2({"key": "k", "pass": True, "missing": [], "violations": [],
                        "unsupported": ["说成了周六"]}, case)
    assert ok["pass"] and not bad["pass"] and bad["unsupported"] == ["说成了周六"]


def test_default_spacing_is_unchanged_and_story_windows_do_not_overlap(tmp_path):
    _, _ = make_round(tmp_path, personas=("P1",))
    rows = [json.loads(s) for s in (tmp_path / "round/bank/suite-v1/inputs/P1.jsonl")
            .read_text("utf-8").splitlines()]
    turns = {r["id"]: r["turn"] for r in rows}
    # Original layout: story k starts at slot 2 * (5 + 5k), i.e. turn slot + 1.
    assert turns["P1-A01-S1"] == 21 and turns["P1-A02-S1"] == 31


def test_version_chain_dates_asks_strata_and_relayed_update(tmp_path):
    story = authoring.chain(
        "V01", "版本链", ["三楼", "五楼", "七楼"], first=lambda v: f"排练室在{v}。",
        change=lambda v: f"排练室改到{v}了。", current_q="排练室现在在哪？",
        first_q="排练室最早在哪？", on_q="{K}那天排练室定在哪？", all_q="排练室换过哪些地方？",
        relayed=("老周", "九楼", "排练室改到九楼。"), gap=4, interference="none")
    root = tmp_path / "round"
    root.mkdir()
    (root / "round.json").write_text(json.dumps({"name": "t", "arms": {}}), "utf-8")
    authoring.build(root / "bank", {"P1": [story, *stories("P1")]},
                    start=datetime(2026, 1, 5, tzinfo=authoring.TZ), slots=200,
                    fillers=["今天没什么事。"])
    suite = root / "bank/suite-v1"
    qs = {q["id"]: q for q in map(json.loads, (suite / "questions.jsonl").read_text("utf-8")
                                  .splitlines())}
    gold = {c["id"]: c for c in map(json.loads, (suite / "review-only/criteria.jsonl")
                                    .read_text("utf-8").splitlines())}
    inputs = {r["id"]: r for r in map(json.loads, (suite / "inputs/P1.jsonl").read_text("utf-8")
                                      .splitlines())}
    assert {"P1-V01/d00", "P1-V01H/d00", "P1-V01T/d00", "P1-V01C/d00"} <= set(qs)
    second = datetime.fromisoformat(inputs["P1-V01-S2"]["now"])
    assert qs["P1-V01T/d03"]["question"] == f"{second.month}月{second.day}日那天排练室定在哪？"
    assert gold["P1-V01T/d03"]["exact"] == [["五楼"]]
    assert gold["P1-V01/d00"]["exact"] == [["九楼"], ["七楼"]]
    assert gold["P1-V01/d00"]["strata"] == {"chain_len": 3, "interference": "none",
                                             "relayed": True}
    assert inputs["P1-V01-S4"]["quoted"]["name"] == "老周"
    # The next story starts after the chain's own, longer window.
    assert inputs["P1-A01-S1"]["turn"] > inputs["P1-V01-S4"]["turn"]


def test_anchor_misjudged_batch_is_discarded(tmp_path, monkeypatch):
    import importlib

    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text("".join(json.dumps(a, ensure_ascii=False) + "\n" for a in (
        {"question": "q", "answer": "绿茶", "required": ["绿茶"], "pass": True},
        {"question": "q", "answer": "红茶", "required": ["绿茶"], "pass": False})), "utf-8")
    root = tmp_path / "round"
    root.mkdir()
    (root / "round.json").write_text(json.dumps({"name": "t", "arms": {}, "grading": {
        "anchors": str(anchors)}}), "utf-8")
    monkeypatch.setenv("MEMORY_ROUND", str(root))
    monkeypatch.setattr(sys, "argv", ["grade.py"])
    grade = importlib.import_module("grade")

    def response(verdicts):
        grades = [{"key": k, "pass": v, "missing": [] if v else [0], "violations": []}
                  for k, v in verdicts.items()]
        return {"status": 200, "keys": list(verdicts), "anchors": {"a1": 0, "a2": 1},
                "response": {"choices": [{"message": {"content": json.dumps({"grades": grades})}}]}}

    lookup = {"c1": {"required": ["x"], "forbidden": []}}
    rows, ok, outcomes = grade.settle(response({"c1": True, "a1": True, "a2": False}), lookup)
    assert ok and outcomes == [True, True] and [r["key"] for r in rows] == ["c1"]
    _, ok, outcomes = grade.settle(response({"c1": True, "a1": True, "a2": True}), lookup)
    assert not ok and outcomes == [True, False]


def test_exact_check_ignores_spacing_and_emphasis(monkeypatch, tmp_path):
    import importlib

    root = tmp_path / "round"
    root.mkdir()
    (root / "round.json").write_text(json.dumps({"name": "t", "arms": {}}), "utf-8")
    monkeypatch.setenv("MEMORY_ROUND", str(root))
    monkeypatch.setattr(sys, "argv", ["report.py"])
    report = importlib.import_module("report")
    criteria = {"exact": [["九楼", "9楼"], ["七楼"]]}
    assert report.exact(criteria, "较新的说法是 **9楼**，你自己说的是七 楼")
    assert not report.exact(criteria, "在九楼")
    assert report.exact({}, "任何") is None
    assert report.z_drop({"passed": 18, "total": 20, "rate": .9},
                         {"passed": 10, "total": 20, "rate": .5}) > 1.645
