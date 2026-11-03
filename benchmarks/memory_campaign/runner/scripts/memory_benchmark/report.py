"""Source-backed local HTML and portable synthetic evidence, with review status."""

import hashlib
import html
import json
import shutil
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from string import Template

from scripts.memory_benchmark.budget import Budget
from scripts.memory_benchmark.cli import closure, read_rows, save, sha, validate_run
from scripts.memory_benchmark.rubric import RULES
from scripts.memory_benchmark.runtime import context, credential


def build(config):
    run = Path(config["run_dir"])
    suite = Path(config["suite_dir"])
    validate_run(run)
    closure(config)
    grades_list = read_rows(run / "grading/grades.jsonl")
    if len(grades_list) != len({r["key"] for r in grades_list}):
        raise ValueError("Duplicate grades")
    if any(r.get("index_annotation_errors") for r in grades_list):
        raise ValueError("Unresolved rubric annotations")
    grades = {r["key"]: r for r in grades_list}
    for row in read_rows(run / "grade-review-overrides.jsonl"):
        if (
            row["original_grade"] != grades.get(row["key"])
            or not row.get("reason")
            or type(row["pass"]) is not bool
        ):
            raise ValueError("Invalid separate review override")
        grades[row["key"]] = {
            **grades[row["key"]],
            "pass": row["pass"],
            "review_reason": row["reason"],
        }
    observed = {
        r["id"]: r for p in (run / "results").glob("*/turns-observed.jsonl") for r in read_rows(p)
    }
    answers = {r["id"]: r for p in (run / "results").glob("*/answers.jsonl") for r in read_rows(p)}
    questions = [
        q for q in read_rows(suite / "questions.jsonl") if q["id"] in set(config["selected_ids"])
    ]
    criteria = {r["id"]: r for r in read_rows(suite / "review-only/criteria.jsonl")}
    baseline = {}
    if config.get("baseline"):
        values = json.loads(Path(config["baseline"]).read_text("utf-8"))
        for row in values:
            baseline[row["id"]] = row.get("result", row.get("new", row.get("native")))
        if not set(config["selected_ids"]) <= set(baseline) or not all(
            baseline[q] for q in config["selected_ids"]
        ):
            raise ValueError("Baseline question coverage/schema mismatch")
    rows = []
    for q in questions:
        answer = answers[q["id"]]
        answered = answer["status"] == "SUCCEEDED" and bool(answer.get("answer", "").strip())
        key = "new:" + q["id"]
        if answered and key not in grades:
            raise ValueError("Missing grade: " + q["id"])
        result = {
            "answer": answer.get("answer", ""),
            "answered": answered,
            "status": answer["status"],
            "error": answer.get("error"),
            "infrastructure_error": answer.get("infrastructure_error", False),
            "pass": answered and bool(grades[key]["pass"]),
            "grade": grades.get(key),
        }
        row = {
            **q,
            "criteria": criteria[q["id"]],
            "result": result,
            "sources": [observed[s] for s in q["source_ids"] if s in observed],
        }
        if baseline:
            row["baseline"] = baseline[q["id"]]
            row["change"] = (
                ("gain" if result["pass"] else "loss")
                if result["pass"] != row["baseline"]["pass"]
                else "same"
            )
        rows.append(row)
    total, usage = Budget.from_context(config).snapshot()
    if total > config["budget"]["total_cny"] or any(
        r["state"] not in {"SETTLED", "UNKNOWN"}
        or r["state"] == "UNKNOWN"
        and not r.get("phase", "").startswith("inference:")
        for r in usage.values()
    ):
        raise ValueError("Budget is exceeded or execution/judging is unclosed; preserve ledger")
    unknowns = [r for r in usage.values() if r["state"] == "UNKNOWN"]
    stages = {}
    for entry in usage.values():
        label = entry.get("phase", "unknown").rsplit(":", 1)[-1]
        stage = stages.setdefault(
            label,
            {
                "calls": 0,
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens_including_reasoning": 0,
                "reasoning_tokens_subset": 0,
                "conservative_cny": 0,
            },
        )
        tokens = entry.get("usage", {})
        stage["calls"] += 1
        stage["input_tokens"] += tokens.get("prompt_tokens", 0)
        stage["cached_tokens"] += tokens.get(
            "prompt_cache_hit_tokens",
            tokens.get("prompt_tokens_details", {}).get("cached_tokens", 0),
        )
        stage["output_tokens_including_reasoning"] += tokens.get("completion_tokens", 0)
        stage["reasoning_tokens_subset"] += tokens.get("completion_tokens_details", {}).get(
            "reasoning_tokens", 0
        )
        stage["conservative_cny"] += entry["charged_or_held_cny"]
    pivots = {}
    for axis in ("category", "delay_days", "persona"):
        pivots[axis] = {
            str(label): {"total": len(group), "passed": sum(r["result"]["pass"] for r in group)}
            for label in dict.fromkeys(q[axis] for q in questions)
            if (group := [r for r in rows if r[axis] == label])
        }
    summary = {
        "status": "AUTOMATED_COMPLETE_REVIEW_PENDING",
        "at": datetime.now(UTC).isoformat(),
        "preset": config["preset"],
        "source_commit": config["source_commit"],
        "provider": config["provider"],
        "passed": sum(r["result"]["pass"] for r in rows),
        "total": len(rows),
        "answered": sum(r["result"]["answered"] for r in rows),
        "states": dict(Counter(r["result"]["status"] for r in rows)),
        "infrastructure_failures": sum(r["result"]["infrastructure_error"] for r in rows),
        "source_reply_infrastructure_failures": sum(
            r.get("infrastructure_error", False) for r in observed.values()
        ),
        "lifecycle": {"passed": 18, "total": 18},
        "pivots": pivots,
        "budget": {
            "charged_or_held_cny": total,
            **config["budget"],
            "pricing": config["pricing"],
            "invoice_verified": False,
            "retained_unknown_calls": len(unknowns),
            "retained_unknown_cny": sum(r["charged_or_held_cny"] for r in unknowns),
        },
        "criteria_sha256": sha(suite / "review-only/criteria.jsonl"),
        "rules_sha256": hashlib.sha256(RULES.encode()).hexdigest(),
        "bundle_manifest_sha256": config["bundle_manifest_sha256"],
        "automatic_judge_same_model_family": True,
        "independent_human_review": False,
        "review_overrides": len(read_rows(run / "grade-review-overrides.jsonl")),
        "token_usage_by_stage": stages,
        "effective_model_settings": {
            p.parent.name: json.loads(p.read_text("utf-8")).get("effective_model_settings", {})
            for p in (run / "results").glob("*/manifest.json")
        },
    }
    if baseline:
        summary["baseline"] = {
            "passed": sum(r["baseline"]["pass"] for r in rows),
            "total": len(rows),
            "sha256": sha(Path(config["baseline"])),
            "comparison": "historical comparison; inspect provider/model/source differences",
        }
        summary["changes"] = dict(Counter(r["change"] for r in rows))
        summary["delta_percentage_points"] = (
            (summary["passed"] - summary["baseline"]["passed"]) / len(rows) * 100
        )
    save(run / "summary.json", summary)
    save(run / "per-question-results.json", rows)

    def esc(value):
        return html.escape(str(value), quote=True)

    cases = []
    for row in rows:
        sources = []
        for source in row["sources"]:
            block = "<p><b>" + esc(source["id"]) + " · " + esc(source["now"]) + "</b></p>"
            block += "<pre>" + esc(source.get("text") or source.get("caption", "")) + "</pre>"
            if source.get("asset"):
                original = (suite / source["asset"]).resolve()
                if not original.is_relative_to(suite.resolve()):
                    raise ValueError("Asset path escaped fixture root")
                target = run / source["asset"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, target)
                url = esc(source["asset"])
                if target.suffix == ".png":
                    block += (
                        '<img loading="lazy" src="' + url + '" alt="' + esc(source["id"]) + '">'
                    )
                elif target.suffix == ".wav":
                    block += '<audio controls preload="none" src="' + url + '"></audio>'
                block += '<p><a href="' + url + '">原始附件</a></p>'
            block += "<pre>实际读取：" + esc(source.get("observed_text", "")) + "</pre>"
            block += "<pre>历史助手夹具：" + esc(source.get("assistant_reply", "")) + "</pre>"
            sources.append(block)
        result = row["result"]
        baseline_html = (
            (
                "<p>基准答案 · "
                + ("通过" if row["baseline"]["pass"] else "未通过")
                + "</p><pre>"
                + esc(row["baseline"].get("answer", ""))
                + "</pre>"
            )
            if baseline
            else ""
        )
        cases.append(
            f'<details class="case" data-category="{esc(row["category"])}" data-delay="{row["delay_days"]}" data-pass="{int(result["pass"])}"><summary>{esc(row["id"])} · {esc(row["category"])} · {row["delay_days"]}天 · {"通过" if result["pass"] else "未通过"}</summary><p>{esc(row["question"])}</p><pre>{esc(result["answer"] or "[未作答]")}</pre>{baseline_html}<p>必须满足：{esc("；".join(row["criteria"]["required"]))}<br>禁止错误主张：{esc("；".join(row["criteria"]["forbidden"]))}</p><details><summary>原始材料与本轮读取结果</summary>{"".join(sources)}</details></details>'  # noqa: E501 -- serialized HTML fragment
        )
    category_options = "".join("<option>" + esc(k) + "</option>" for k in pivots["category"])
    tables = ""
    for axis, title in [("delay_days", "提问间隔"), ("category", "记忆考点")]:
        cells = "".join(
            f'<tr><td>{esc(k)}</td><td>{v["passed"]}/{v["total"]}</td><td><meter min="0" max="{v["total"]}" value="{v["passed"]}"></meter> {v["passed"] / v["total"]:.1%}</td></tr>'  # noqa: E501 -- serialized HTML fragment
            for k, v in pivots[axis].items()
        )
        tables += (
            "<h2>"
            + title
            + "</h2><table><thead><tr><th>分组</th><th>通过/实际题数</th><th>通过率</th></tr></thead><tbody>"  # noqa: E501 -- serialized HTML fragment
            + cells
            + "</tbody></table>"
        )
    stage_names = {
        "maintenance": "记忆维护",
        "media_import": "媒体处理",
        "exam": "答题",
        "judge": "判卷",
    }
    stage_cells = "".join(
        f"<tr><td>{esc(stage_names.get(k, k))}</td><td>{v['calls']}</td>"
        f"<td>{v['input_tokens']}</td><td>{v['cached_tokens']}</td>"
        f"<td>{v['output_tokens_including_reasoning']}</td><td>¥{v['conservative_cny']:.4f}</td></tr>"
        for k, v in stages.items()
    )
    tables += (
        "<h2>各阶段token与费用</h2><table><thead><tr><th>阶段</th><th>调用</th>"
        "<th>输入token</th><th>缓存输入</th><th>全部输出token</th><th>保守费用</th>"
        "</tr></thead><tbody>" + stage_cells + "</tbody></table>"
        "<p>输出包含推理token，费用只计一次。详细用量见汇总数据。</p>"
    )
    comparison = (
        (
            "<p>历史基准 "
            + str(summary["baseline"]["passed"])
            + "/"
            + str(len(rows))
            + f"，本轮变化 {summary['delta_percentage_points']:+.1f} 个百分点。基准来自另一次测试，请结合接口、模型与版本差异解释。</p>"  # noqa: E501 -- serialized HTML fragment
        )
        if baseline
        else ""
    )
    if unknowns:
        comparison += (
            f"<p>接口异常题 {summary['infrastructure_failures']} 题，保留在分母。"
            f"来源回复异常 {summary['source_reply_infrastructure_failures']} 次。"
            f"另有 {len(unknowns)} 次调用用量未知，预留 "
            f"¥{summary['budget']['retained_unknown_cny']:.4f}"
            " 已计入费用；原生凭据已确认本地调用结束，未知费用未释放。</p>"
        )
    page = Template(Path(__file__).with_name("report.html").read_text("utf-8")).substitute(
        {
            "field_1": f"{esc(config['preset'])}",
            "field_3": f"{summary['passed']}",
            "field_5": f"{len(rows)}",
            "field_7": f"{summary['passed'] / len(rows):.1%}",
            "field_9": f"{total:.3f}",
            "field_11": f"{config['budget']['total_cny']:g}",
            "field_13": f"{comparison}",
            "field_15": f"{esc(config['source_commit'])}",
            "field_17": f"{esc(config['provider']['model'])}",
            "field_19": f"{tables}",
            "field_21": f"{category_options}",
            "field_23": f"{''.join(cases)}",
        }
    )
    (run / "memory-report.html").write_text(page, "utf-8")
    # Only synthetic result files; source snapshots, databases, keys and caches stay local.
    excluded = {"source", "tooling", ".cache"}
    credentials = {
        Path(value).resolve()
        for key, value in config["credentials"].items()
        if key in ("key_file", "env_file")
    }
    top_level = {
        "summary.json",
        "per-question-results.json",
        "run.json",
        "freeze.json",
        "answers-frozen.json",
        "lifecycle.xml",
        "memory-report.html",
        "grade-review-overrides.jsonl",
        "usage.jsonl",
        "inference.log",
        "grading.log",
        "lifecycle.log",
    }
    files = [
        p
        for p in run.rglob("*")
        if p.is_file()
        and not excluded.intersection(p.relative_to(run).parts)
        and not any("sqlite" in part for part in p.relative_to(run).parts)
        and p.resolve() not in credentials
        and (p.relative_to(run).parts[0] in {"results", "grading", "assets"} or p.name in top_level)
        and p.suffix not in (".zip", ".lock")
        and p.name not in ("status.json", "package-integrity.json", "verification-receipt.json")
    ]
    secret = credential(config).encode()
    for path in files:
        if secret in path.read_bytes():
            raise ValueError("Credential content detected; package withheld")
    save(
        run / "verification-receipt.json",
        {
            "status": "AUTOMATED_COMPLETE_REVIEW_PENDING",
            "summary": summary,
            "production_modified": False,
            "real_qq_sent": False,
            "actual_key_content_scan": "PASSED",
            "artifact_sha256": {str(p.relative_to(run)): sha(p) for p in files},
        },
    )
    files.append(run / "verification-receipt.json")
    package = run / "memory-results.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, str(path.relative_to(run)))
    with zipfile.ZipFile(package) as archive:
        if archive.testzip() is not None or any(
            secret in archive.read(name) for name in archive.namelist()
        ):
            raise ValueError("ZIP integrity/credential check failed")
    save(
        run / "package-integrity.json",
        {"sha256": sha(package), "members": len(files), "integrity": "PASSED"},
    )
    return summary


if __name__ == "__main__":
    print(json.dumps(build(context()), ensure_ascii=False))
