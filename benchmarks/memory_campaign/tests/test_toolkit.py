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
