"""Aggregate a graded round into results.json, summary.json, charts and memory-report.html.

  python report.py --round DIR [--grading grading] [--prepare-review] [--allow-partial-review]

Everything (personas, categories, families, delays, totals, caps) is derived from the bank
and round.json. Review overrides live beside the grades and never edit them:
<grading>/review.jsonl (every reviewed answer) and <grading>/overrides.jsonl (changed ones).
Charts need matplotlib; without it the report is written without figures.
"""

import hashlib
import html
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent / "runner")]
from integrity import check_grading_freeze, grading_identity  # noqa: E402
from roundcfg import Round, ledger_total, read_rows, save, sha  # noqa: E402
from rubrics import rubric  # noqa: E402

R = Round()
ARMS = tuple(R.cfg["arms"]) + R.references
PAIRED = "old" in ARMS and "new" in ARMS
ROOT = R.root
ARGS = sys.argv[1:]
GRADING = ROOT / (ARGS[ARGS.index("--grading") + 1] if "--grading" in ARGS else "grading")
LABELS = {"old": "旧版 Core", "new": "新版 Core", "rag": "千问 RAG", "closed": "闭卷对照"}
COLORS = {"old": "#7D8798", "new": "#2859E8", "rag": "#008896"}
HATCH = {"old": "///", "new": "", "rag": ".."}
for arm in ARMS:
    LABELS.setdefault(arm, "Core" if arm == "core" else arm)
    COLORS.setdefault(arm, "#2859E8")
    HATCH.setdefault(arm, "")


def latency(answer):
    if answer.get("elapsed_seconds") is not None:
        return answer["elapsed_seconds"]
    if answer.get("actual_finished_at") and answer.get("actual_started_at"):
        return (datetime.fromisoformat(answer["actual_finished_at"])
                - datetime.fromisoformat(answer["actual_started_at"])).total_seconds()
    return None


def load(*, allow_failed_gate=False):
    freeze_path = GRADING / "grading-freeze.json"
    frozen = json.loads(freeze_path.read_text("utf-8"))
    rules, _, _ = rubric(frozen["rubric"])
    assert "effective_votes" in frozen, "Grading freeze lacks resolved votes; use a fresh --out directory"
    check_grading_freeze(frozen, grading_identity(R, frozen["rubric"], rules, frozen["effective_votes"]))
    manifest = json.loads((GRADING / "manifest.json").read_text("utf-8"))
    assert manifest["grading_freeze_sha256"] == sha(freeze_path), "Grading manifest freeze changed"
    assert manifest["raw_grades_sha256"] == sha(GRADING / "grades.jsonl"), "Raw grades changed"
    assert manifest["status"] in ({"AUTO_COMPLETE_REVIEW_PENDING", "ANCHOR_GATE_FAILED"}
                                  if allow_failed_gate else {"AUTO_COMPLETE_REVIEW_PENDING"}), "Anchor gate failed"
    if not allow_failed_gate:
        gates = manifest["anchor_gates"]
        if frozen["anchors"]:
            assert len(gates) == frozen["effective_votes"], "Anchor gates incomplete"
            assert all(g and g["passed"] is True for g in gates.values()), "Anchor gate failed"
    answers = {}
    for a in R.graded_arms:
        raw = R.arm_answers(a)
        answers[a] = {r["id"]: r for r in raw}
        assert len(raw) == len(answers[a]) == R.total, "Answers incomplete or duplicated: " + a
        assert set(answers[a]) == {q["id"] for q in R.questions}, "Answer IDs changed: " + a
    mapping = json.loads((GRADING / "blind-mapping.json").read_text("utf-8"))
    raw_grades = read_rows(GRADING / "grades.jsonl")
    grades = {r["key"]: r for r in raw_grades}
    assert len(raw_grades) == len(grades) == len(mapping) == R.answers_total, "grading incomplete or duplicated"
    assert set(grades) == {m["key"] for m in mapping}, "Grading key mismatch"
    assert all(type(g["pass"]) is bool for g in raw_grades), "Non-boolean grade"
    final = {k: dict(v) for k, v in grades.items()}
    overridden = set()
    for row in read_rows(GRADING / "overrides.jsonl"):
        assert row["original_grade"] == grades[row["key"]] and type(row["pass"]) is bool
        overridden.add(row["key"])
        final[row["key"]] = {**grades[row["key"]], "pass": row["pass"],
                             "review_reason": row["reason"]}
    unresolved = {k for k, g in grades.items() if any(g.get(flag) for flag in (
        "judge_unresolved", "anchor_failed", "interrupted_review_required"))}
    assert allow_failed_gate or not unresolved - overridden, "Unresolved judgments need explicit review overrides"
    reverse = {(m["arm"], m["id"]): m for m in mapping}
    assert set(reverse) == {(a, q["id"]) for a in R.graded_arms for q in R.questions}, "Mapping mismatch"
    inputs = {r["id"]: r for p in (R.suite / "inputs").glob("*.jsonl") for r in read_rows(p)}
    rows = []
    for q in sorted(R.questions, key=lambda r: r["id"]):
        arms = {}
        for a in R.graded_arms:
            ans, m = answers[a][q["id"]], reverse[a, q["id"]]
            assert m["key"] == hashlib.sha256((frozen["answers_freeze_sha256"] + a + q["id"])
                                              .encode()).hexdigest()[:24], "Blind key mismatch"
            assert m["answer_sha256"] == hashlib.sha256(ans.get("answer", "").encode()).hexdigest()
            arms[a] = dict(key=m["key"], answer=ans.get("answer", ""), status=ans["status"],
                           error=ans.get("error"), auto_grade=grades[m["key"]],
                           passed=final[m["key"]]["pass"], elapsed_seconds=latency(ans),
                           answer_sha256=m["answer_sha256"],
                           exact_pass=exact(R.criteria[q["id"]], ans.get("answer", "")))
            assert has_answer(arms[a]) or not arms[a]["passed"], "Unanswered instance cannot pass"
        change = "pass" if arms[ARMS[0]]["passed"] else "fail"
        if PAIRED:
            op, npass = arms["old"]["passed"], arms["new"]["passed"]
            change = ("gain" if npass else "loss") if op != npass else (
                "both_pass" if op else "both_fail")
        rows.append({**q, "criteria": R.criteria[q["id"]], "arms": arms, "change": change,
                     "sources": [inputs[s] for s in q["source_ids"]]})
    return rows, grades


def exact(criteria, answer):
    """Mechanical check: every exact group has one alternative verbatim in the answer
    (spaces and markdown emphasis ignored). None when the criteria define no groups."""
    groups = criteria.get("exact")
    if not groups:
        return None
    text = "".join(answer.replace("*", "").split())
    return all(any("".join(alt.split()) in text for alt in group) for group in groups)


def z_drop(low, high):
    """One-sided two-proportion z that `high` (e.g. the longest chains) scores below `low`."""
    n1, n2 = low["total"], high["total"]
    if not n1 or not n2:
        return None
    pooled = (low["passed"] + high["passed"]) / (n1 + n2)
    spread = (pooled * (1 - pooled) * (1 / n1 + 1 / n2)) ** 0.5
    return round((low["rate"] - high["rate"]) / spread, 3) if spread else None


def score(rows, arm):
    n = len(rows)
    passed = sum(r["arms"][arm]["passed"] for r in rows)
    answered = sum(has_answer(r["arms"][arm]) for r in rows)
    return dict(passed=passed, total=n, rate=passed / n if n else None,
                answered=answered, unanswered=n - answered, semantic_failures=answered - passed,
                answered_rate=passed / answered if answered else None,
                empty_answers=sum(r["arms"][arm]["status"] == "SUCCEEDED"
                                  and not r["arms"][arm]["answer"].strip() for r in rows),
                execution_failures=sum(r["arms"][arm]["status"] != "SUCCEEDED" for r in rows))


def has_answer(answer):
    return answer["status"] == "SUCCEEDED" and bool(answer["answer"].strip())


def finance(path, cap):
    total, latest = ledger_total(path)
    phases = defaultdict(lambda: dict(calls=0, charged_or_held_cny=0.0, input_tokens=0,
                                      cached_tokens=0, output_tokens=0))
    for v in latest.values():
        phase = v.get("phase", "unknown")
        label = phase.split(":")[1] if phase.startswith("inference:") else phase
        s, u = phases[label], v.get("usage") or {}
        s["calls"] += 1
        s["charged_or_held_cny"] += v.get("charged_or_held_cny", 0)
        s["input_tokens"] += u.get("prompt_tokens", 0)
        s["cached_tokens"] += u.get("prompt_cache_hit_tokens", 0)
        s["output_tokens"] += u.get("completion_tokens", 0)
    return dict(charged_or_held_cny=total, cap_cny=cap,
                states=dict(Counter(v.get("state") for v in latest.values())),
                phases=dict(phases), invoice_verified=False)


def modality_groups(rows):
    return {"纯文字源": [r for r in rows if set(r["input_modalities"]) == {"text"}],
            "含语音": [r for r in rows if "voice" in r["input_modalities"]],
            "含图像": [r for r in rows if "image" in r["input_modalities"]],
            "含文件/表格": [r for r in rows if "file" in r["input_modalities"]],
            "含引用/转发": [r for r in rows if set(r["input_modalities"]) & {"quote", "forward"}],
            "含助手历史": [r for r in rows if "assistant" in r["input_modalities"]]}


def summarize(rows):
    excluded = set(R.cfg["sensitivity_exclude_categories"])
    answered = [r for r in rows if all(has_answer(r["arms"][a]) for a in ARMS)]
    summary = dict(
        round=R.name, at=datetime.now(UTC).isoformat(), grading=GRADING.name,
        overall={a: score(rows, a) for a in ARMS},
        all_arms_answered={a: score(answered, a) for a in ARMS},
        all_arms_answered_ids=sorted(r["id"] for r in answered),
        sample_counts=dict(question_instances=len(rows), personas=len({r["persona"] for r in rows}),
                           fact_groups=len({(r["persona"], r["group_id"]) for r in rows})),
        by_persona={p: {a: score([r for r in rows if r["persona"] == p], a) for a in ARMS}
                    for p in R.personas},
        by_delay={str(d): {a: score([r for r in rows if r["delay_days"] == d], a) for a in ARMS}
                  for d in R.delays},
        by_category={c: {a: score([r for r in rows if r["category_id"] == c], a) for a in ARMS}
                     for c in R.categories},
        category_delay={c: {str(d): {a: score([r for r in rows if r["category_id"] == c
                                               and r["delay_days"] == d], a) for a in ARMS}
                            for d in R.delays} for c in R.categories},
        modality={m: {a: score(rs, a) for a in ARMS} for m, rs in modality_groups(rows).items()
                  if rs},
        finance={"deepseek": finance(R.deepseek_ledger, R.cfg["budget"]["deepseek_total_cny"]),
                 "qwen": finance(R.qwen_ledger, R.cfg["budget"]["qwen_total_cny"])},
        review_count=len(read_rows(GRADING / "review.jsonl")),
        override_count=len(read_rows(GRADING / "overrides.jsonl")), not_independent_human=True,
        limitations=R.cfg["limitations"])
    if excluded:
        kept = [r for r in rows if r["category_id"] not in excluded]
        summary["sensitivity"] = {"excluded_categories": sorted(excluded),
                                  **{a: score(kept, a) for a in ARMS}}
    if R.controls:
        guessable = sorted(r["id"] for r in rows if any(r["arms"][c]["passed"] for c in R.controls))
        kept = [r for r in rows if r["id"] not in set(guessable)]
        summary["controls"] = {c: score(rows, c) for c in R.controls}
        summary["guessable_without_memory"] = guessable
        summary["memory_dependent"] = {a: score(kept, a) for a in ARMS}
    checked = [r for r in rows if r["arms"][ARMS[0]]["exact_pass"] is not None]
    if checked:
        summary["exact"] = {a: dict(
            checked=len(checked), exact_pass=sum(r["arms"][a]["exact_pass"] for r in checked),
            disagree_with_final=sum(r["arms"][a]["exact_pass"] != r["arms"][a]["passed"]
                                    for r in checked)) for a in ARMS}
    names = sorted({k for r in rows for k in r["criteria"].get("strata", {})})
    if names:
        summary["strata"] = {}
        for name in names:
            values = sorted({r["criteria"]["strata"][name] for r in rows
                             if name in r["criteria"].get("strata", {})}, key=str)
            cells = {str(v): {a: score([r for r in rows if r["criteria"].get("strata", {})
                                        .get(name) == v], a) for a in ARMS} for v in values}
            entry = {"values": cells}
            numeric = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
            if len(numeric) >= 2:
                low, high = str(min(numeric)), str(max(numeric))
                entry["drop_z_shortest_vs_longest"] = {
                    a: z_drop(cells[low][a], cells[high][a]) for a in ARMS}
            summary["strata"][name] = entry
    manifest = GRADING / "manifest.json"
    if manifest.exists():
        m = json.loads(manifest.read_text("utf-8"))
        summary["judging"] = {k: m.get(k) for k in ("status", "judges", "votes", "anchor_gates",
                                                    "anchor_failed_keys", "split_decisions")}
    if PAIRED:
        summary["paired_changes"] = dict(Counter(r["change"] for r in rows))
        summary["new_minus_old_points"] = (summary["overall"]["new"]["rate"]
                                           - summary["overall"]["old"]["rate"]) * 100
    summary["latency"] = {a: dict(median_seconds=statistics.median(v),
                                  p95_seconds=statistics.quantiles(v, n=100, method="inclusive")[94]
                                  if len(v) > 1 else v[0],
                                  n=len(v))
                          for a in ARMS if (v := [r["arms"][a]["elapsed_seconds"] for r in rows
                                                  if r["arms"][a]["elapsed_seconds"] is not None])}
    return summary


def prepare_review(rows, grades):
    persona, delay = R.review_persona(), R.cfg["review"]["delay_days"]
    selected = [(r, a, f"preregistered_{persona}_d{delay}") for r in rows
                if r["persona"] == persona and r["delay_days"] == delay for a in ARMS]
    for family in sorted({r["family"] for r in rows}) if PAIRED else ():
        pool = [r for r in rows if r["family"] == family and r["arms"]["old"]["auto_grade"]["pass"]
                != r["arms"]["new"]["auto_grade"]["pass"]]
        for r in pool[:R.cfg["review"]["changed_per_family"]]:
            selected.extend((r, a, "changed_" + family) for a in ARMS)
    for r in rows:
        for c in R.controls:
            if r["arms"][c]["auto_grade"]["pass"]:
                selected.append((r, c, "closed_book_pass"))
        for a in ARMS:
            if r["arms"][a]["exact_pass"] not in (None, r["arms"][a]["auto_grade"]["pass"]):
                selected.append((r, a, "exact_check_disagrees"))
        for a in R.graded_arms:
            g = r["arms"][a]["auto_grade"]
            if any(g.get(flag) for flag in (
                "judge_unresolved", "anchor_failed", "interrupted_review_required")):
                selected.append((r, a, "unresolved_judgment"))
            if g.get("agreement", 1) < 1:
                selected.append((r, a, "split_judge_vote"))
            if g.get("index_annotation_errors") or (not g["pass"] and not g.get("missing")
                                                    and not g.get("violations")
                                                    and not g.get("unsupported")
                                                    and not g.get("execution_failure")):
                selected.append((r, a, "inconsistent_auto_grade"))
    out, seen = [], set()
    for r, arm, reason in selected:
        v = r["arms"][arm]
        if v["key"] in seen:
            continue
        seen.add(v["key"])
        out.append(dict(key=v["key"], arm=arm, id=r["id"], reason=reason,
                        answer_sha256=v["answer_sha256"], question=r["question"],
                        category=r["category"], required=r["criteria"]["required"],
                        forbidden=r["criteria"].get("forbidden", []), answer=v["answer"],
                        auto_grade=grades[v["key"]]))
    path = GRADING / "review-selection.json"
    if path.exists():
        assert json.loads(path.read_text("utf-8")) == out, "review selection changed"
    else:
        save(path, out)
    return out


def charts(rows, summary):
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        from matplotlib.font_manager import FontProperties
        from matplotlib.patches import Patch
    except ImportError:
        return []
    font = FontProperties(fname="C:/Windows/Fonts/msyh.ttc")
    plt.rcParams.update({"font.family": font.get_name(), "font.size": 11,
                         "axes.unicode_minus": False, "svg.fonttype": "path",
                         "figure.facecolor": "#F5F8FD", "axes.facecolor": "#F5F8FD"})

    def header(fig, title, subtitle):
        h = fig.get_figheight()
        fig.text(.06, 1 - .35 / h, title, fontsize=22, weight="bold", color="#102F51", va="top")
        fig.text(.06, 1 - .85 / h, subtitle, fontsize=11, color="#53677E", va="top")
        fig.legend(handles=[Patch(facecolor=COLORS[a], hatch=HATCH[a], label=LABELS[a])
                            for a in ARMS], loc="upper left", bbox_to_anchor=(.055, 1 - 1.2 / h),
                   ncol=3, frameon=False)

    def store(fig, stem):
        fig.savefig(ROOT / f"{stem}.png", dpi=150, facecolor=fig.get_facecolor())
        fig.savefig(ROOT / f"{stem}.svg", facecolor=fig.get_facecolor())
        plt.close(fig)

    fig, ax = plt.subplots(figsize=(11.6, 6.2))
    fig.subplots_adjust(left=.22, right=.88, bottom=.14, top=.72)
    header(fig, f"{R.name} · 记忆测试", f"{R.total}题 · {len(R.personas)}个虚构人物 · 整题通过率")
    for i, a in enumerate(ARMS):
        s = summary["overall"][a]
        ax.barh(len(ARMS) - i - 1, s["rate"] * 100,
                color=COLORS[a], hatch=HATCH[a], height=.55)
        ax.text(s["rate"] * 100 + 1, len(ARMS) - i - 1,
                f'{s["passed"]}/{s["total"]}  {s["rate"]:.1%}',
                va="center", weight="bold", color="#102F51")
    ax.set(yticks=list(reversed(range(len(ARMS)))),
            yticklabels=[LABELS[a] for a in ARMS], xlim=(0, 100))
    for spine in ax.spines.values():
        spine.set_visible(False)
    store(fig, "memory-summary")
    figures = []
    for page, family in enumerate(dict.fromkeys(r["family"] for r in rows), 1):
        cats = list(dict.fromkeys(r["category_id"] for r in rows if r["family"] == family))
        fig, axes = plt.subplots(len(cats), 1, figsize=(12, 3 + 2 * len(cats)), squeeze=False)
        fig.subplots_adjust(left=.28, right=.95, top=1 - 1.9 / fig.get_figheight(),
                            bottom=.9 / fig.get_figheight(), hspace=.62)
        header(fig, family, "柱高=整题通过率 · 柱顶=通过数/题数 · 统一0–100%轴")
        delays = R.delays
        for ax, cid in zip(axes[:, 0], cats, strict=True):
            subset = [r for r in rows if r["category_id"] == cid]
            ax.text(-.32, .5, subset[0]["category"], transform=ax.transAxes, fontsize=12,
                    weight="bold", color="#102F51", va="center")
            for j, a in enumerate(ARMS):
                s = [score([r for r in subset if r["delay_days"] == d], a) for d in delays]
                xs = np.arange(len(delays)) + (j - 1) * .21
                bars = ax.bar(xs, [v["rate"] * 100 for v in s], width=.2, color=COLORS[a],
                              hatch=HATCH[a])
                for b, v in zip(bars, s, strict=True):
                    ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 3,
                            f'{v["passed"]}/{v["total"]}', ha="center", fontsize=9,
                            color=COLORS[a], weight="bold", clip_on=False)
            ax.set(ylim=(0, 100), yticks=[0, 100], xticks=range(len(delays)),
                   xticklabels=["当天" if d == 0 else f"{d}天" for d in delays])
            for spine in ax.spines.values():
                spine.set_visible(False)
        stem = f"category-{page:02d}"
        store(fig, stem)
        figures.append(dict(stem=stem, title=family))
    return figures


def extras(summary, e):
    """Controls, anchor gates, exact checks and strata as short paragraphs."""
    counts = summary["sample_counts"]
    common = summary["all_arms_answered"]
    out = [f'<p>{counts["question_instances"]} 个题目实例，{counts["personas"]} 个人物，'
           f'{counts["fact_groups"]} 个事实组。同一事实组的多次提问相关，不能当作独立样本；'
           '分层 z 值仅供探索，未校正组内相关性。</p>',
           '<p>三路共同已作答：' + e("；".join(
               f'{LABELS[a]} {v["passed"]}/{v["total"]}' for a, v in common.items()))
           + '。主通过率仍以全部题目实例为分母，保留执行失败和空回答。</p>']
    out.append('<table><caption>按人物统计：通过 / 全部（已作答）</caption><thead><tr><th>人物</th>'
               + "".join(f'<th>{LABELS[a]}</th>' for a in ARMS) + '</tr></thead><tbody>'
               + "".join('<tr><th>' + e(p) + '</th>' + "".join(
                   f'<td>{v[a]["passed"]}/{v[a]["total"]}（{v[a]["answered"]}）</td>' for a in ARMS)
                   + '</tr>' for p, v in summary["by_persona"].items()) + '</tbody></table>')
    if "controls" in summary:
        c = ", ".join(f'{LABELS.get(k, k)} {v["passed"]}/{v["total"]}'
                      for k, v in summary["controls"].items())
        md = "；".join(f'{LABELS[a]} {v["passed"]}/{v["total"]}'
                      for a, v in summary["memory_dependent"].items())
        out.append(f"<p>无资料对照：{e(c)}。去掉对照组能答对的题后：{e(md)}。</p>")
    for name, gate in ((summary.get("judging") or {}).get("anchor_gates") or {}).items():
        if gate:
            out.append(f'<p>锚点 {e(name)}：准确率 {gate["accuracy"]:.1%}，多数类基线 '
                       f'{gate["majority_baseline"]:.1%}，作废批次 {gate["discarded_batches"]}/'
                       f'{gate["batches"]}，{"通过" if gate["passed"] else "未通过"}。</p>')
    if "exact" in summary:
        out.append("<p>逐字核对：" + e("；".join(
            f'{LABELS[a]} {v["exact_pass"]}/{v["checked"]}（与终判不一致 {v["disagree_with_final"]}）'
            for a, v in summary["exact"].items())) + "</p>")
    for name, entry in summary.get("strata", {}).items():
        cells = "；".join(f'{value}: ' + " / ".join(f'{v[a]["passed"]}/{v[a]["total"]}' for a in ARMS)
                         for value, v in entry["values"].items())
        out.append(f"<p>分层 {e(name)}（旧/新/RAG）：{e(cells)}</p>")
    return "".join(out)


def render(rows, summary, figures):
    e = lambda s: html.escape(str(s), quote=True)  # noqa: E731
    cards = "".join(f'<div class="kpi"><span>{LABELS[a]}</span><strong>{summary["overall"][a]["rate"]:.1%}'
                    f'</strong><small>{summary["overall"][a]["passed"]}/{R.total} · 已作答 '
                    f'{summary["overall"][a]["answered"]} · 答错 {summary["overall"][a]["semantic_failures"]}'
                    f' · 未作答 {summary["overall"][a]["unanswered"]}（执行失败 '
                    f'{summary["overall"][a]["execution_failures"]}，空回答 '
                    f'{summary["overall"][a]["empty_answers"]}）</small></div>' for a in ARMS)
    figs = "".join(f'<figure><h3>{e(f["title"])}</h3><img src="{f["stem"]}.png"></figure>'
                   for f in figures)
    review = {r["key"]: r["reason"] for r in read_rows(GRADING / "review.jsonl")}
    cards_q = []
    for r in rows:
        parts = "".join(
            f'<section class="answer"><h4>{LABELS[a]} <b class="{"pass" if v["passed"] else "fail"}">'
            f'{"通过" if v["passed"] else ("未通过" if has_answer(v) else "未作答")}</b>'
            f'</h4><pre>{e(v["answer"] or v["error"] or "无可见回答")}</pre>'
            + (f'<p class="note">复核：{e(review[v["key"]])}</p>' if v["key"] in review else "")
            + "</section>" for a, v in r["arms"].items())
        cards_q.append(f'<details class="q" data-cat="{e(r["category_id"])}"><summary><code>'
                       f'{e(r["id"])}</code> {e(r["category"])} · {r["delay_days"]}天 '
                       f'{" / ".join("✓" if r["arms"][a]["passed"] else "×" for a in ARMS)}'
                       f'</summary><h3>{e(r["question"])}</h3><div class="answers">{parts}</div>'
                       f'<p>必须满足：{e("；".join(r["criteria"]["required"]))}</p>'
                       f'<p>禁止：{e("；".join(r["criteria"].get("forbidden", [])))}</p></details>')
    ds, qw = summary["finance"]["deepseek"], summary["finance"]["qwen"]
    comparison = (f'新版较旧版 {summary["new_minus_old_points"]:+.2f} 个百分点；配对：'
                  f'{e(summary["paired_changes"])}。') if PAIRED else ""
    doc = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>' + e(R.name)
           + '</title><style>body{font-family:"Microsoft YaHei",sans-serif;background:#f5f8fd;'
           'color:#17334f;margin:0}main{max-width:1200px;margin:auto;padding:32px}.kpis{display:grid;'
           'grid-template-columns:repeat(3,1fr);gap:16px}.kpi{background:#fff;padding:18px}.kpi strong'
           '{display:block;font-size:38px}figure{background:#fff;margin:20px 0;padding:12px}figure img'
           '{width:100%}.answers{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.answer'
           '{background:#fff;padding:10px}pre{white-space:pre-wrap;font:inherit}.pass{color:#087869}'
           'table{border-collapse:collapse;margin:16px 0}th,td{padding:4px 12px;text-align:left}'
           '.fail{color:#a43d40}.q{border-bottom:1px solid #d1dce9;padding:10px 0}.note{font-size:12px;'
           'color:#586c81}</style><main><h1>' + e(R.name) + '</h1>'
           f'<p>{R.total}题 × {e(" / ".join(LABELS[a] for a in ARMS))} · '
           f'判卷 {e(GRADING.name)}</p><div class="kpis">'
           f'{cards}</div><p>{comparison}DeepSeek ¥{ds["charged_or_held_cny"]:.2f}/'
           f'¥{ds["cap_cny"]}，千问 ¥{qw["charged_or_held_cny"]:.2f}/¥{qw["cap_cny"]}（保守估算）。'
           f'复核 {summary["review_count"]} 份，改判 {summary["override_count"]} 份，非独立人工。</p>'
           + extras(summary, e)
           + figs + "<h2>逐题</h2>" + "".join(cards_q) + "<h2>局限</h2>"
           + "".join(f"<p>{e(x)}</p>" for x in summary["limitations"]) + "</main></html>")
    (ROOT / "memory-report.html").write_text(doc, "utf-8")


def main():
    rows, grades = load(allow_failed_gate="--prepare-review" in ARGS)
    selection = prepare_review(rows, grades)
    if "--prepare-review" in ARGS:
        print("review_selection", len(selection))
        return
    reviewed = {r["key"]: r for r in read_rows(GRADING / "review.jsonl")}
    missing = {r["key"] for r in selection} - set(reviewed)
    if missing and "--allow-partial-review" not in ARGS:
        raise SystemExit(f"Review incomplete: {len(missing)} selected answers unreviewed")
    summary = summarize(rows)
    save(ROOT / "results.json", rows)
    save(ROOT / "summary.json", summary)
    render(rows, summary, charts(rows, summary))
    print(json.dumps(summary["overall"], ensure_ascii=False))


if __name__ == "__main__":
    main()
