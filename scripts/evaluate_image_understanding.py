"""Prepare image/facts/correction/persona comparisons for the existing evaluation job.

No provider transport or execution authority. Gold labels never enter a payload.
Synthetic tasks and licensed public photos are separate from private owner pilots.
"""

import argparse
import base64
import hashlib
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from companion import evaluation, paid_budget  # noqa: E402
from companion.config import PaidModelApproval, Settings  # noqa: E402
from companion.conversation_policy import PARTICIPATION_V2  # noqa: E402
from companion.evaluation_measurement import measurement_profile, request_measurement  # noqa: E402
from companion.image_input import ImageInputPolicy  # noqa: E402
from companion.image_understanding import RULES  # noqa: E402
from companion.model import PRINCIPLES, model_payload  # noqa: E402
from companion.store import encode  # noqa: E402

DATA = ROOT / "tests/fixtures/image_understanding"
AXES = {"perception": [0, 1, 2], "relations": [0, 1, 2], "faithfulness": [0, 1, 2],
        "concrete_review": [0, 1, 2], "evidence_honesty": [0, 1, 2], "naturalness": [0, 1, 2]}


def planning_settings():
    """Non-executable count/price profile; a live run requires its own native grant."""
    image = ImageInputPolicy(approval_sha256="0" * 64, estimated_tokens_per_image=1600,
        max_bytes_per_image=4000000, max_pixels_per_image=16000000, max_images_per_request=1)
    paid = PaidModelApproval(live_approved=False, window_id="vision-comparison-planning",
        approval_sha256="0" * 64, provider="deepseek", model="deepseek-flash",
        endpoint="https://api.deepseek.com", currency="USD", timezone="Asia/Shanghai",
        starts_at="2026-11-01T00:00:00+08:00", input_rate=300000,
        cached_input_rate=6000, output_rate=1200000, max_input_tokens=8000,
        max_output_tokens=2048, per_attempt_micro_usd=5000, per_request_micro_usd=30000,
        per_request_calls=6, daily_micro_usd=1250000, daily_calls=216,
        monthly_micro_usd=1250000, window_micro_usd=1250000, window_calls=216,
        concurrency=1, journal_path=str(ROOT / ".local/vision-evaluation/budget.jsonl"),
        image_input=image)
    return Settings(owner_qq_id="10001", bot_qq_id="10002",
        bridge_token="planning-only-token-12345",
        provider=paid.provider, model_name=paid.model, model_url=paid.endpoint,
        input_rate=paid.input_rate, cached_input_rate=paid.cached_input_rate,
        output_rate=paid.output_rate, paid_model=paid, max_output_tokens=2048,
        context_soft_tokens=8000, chat_thinking="disabled", memory_enabled=False)


def load_cases():
    cases = json.loads((DATA / "cases.json").read_text("utf-8"))["cases"]
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Duplicate case")
    for case in cases:
        path = (DATA / case["image"]).resolve()
        if path.parent != DATA.resolve() or hashlib.sha256(
            path.read_bytes()).hexdigest() != case["image_sha256"]:
            raise ValueError("Fixture bytes changed")
    return cases


def system(arm):
    frozen = json.loads((DATA / "baseline.json").read_text("utf-8"))
    if arm == "baseline":
        return frozen["principles"] + "\n" + frozen["participation_v2"]
    principles = PRINCIPLES
    if arm == "image_persona":
        start, end = principles.index("你在乎主人"), principles.index("闲聊像熟人")
        principles = principles[:start] + principles[end:]
        principles = principles.replace("是主人很亲近的人", "和主人熟悉相处")
    return principles + "\n" + PARTICIPATION_V2 + RULES


def tasks(cases):
    """Every arm gets identical evidence; visibility and correction type are separate tasks."""
    for case in cases:
        for mode in ("original", "facts"):
            yield case, {"task": "bare", "input": mode, "visibility": "available"}, False
        if "initial_wrong" in case:
            for kind in ("correct", "wrong", "vague"):
                for visibility in ("available", "expired"):
                    yield case, {"task": "correction", "input": "original",
                                 "correction": kind, "visibility": visibility}, False
            yield case, {"task": "bare", "input": "original", "visibility": "available"}, True


def messages(case, task, arm):
    question = "你看看图片，你觉得要怎么解读？"
    content = question
    if task["input"] == "facts":
        content += "\n仅有以下文字和布局事实（未提供寓意答案）：\n" + case["facts"]
    elif task["visibility"] == "expired":
        content += ("\n原图已过期，本轮没有图片；仅有当时的旧机器观察（可能有误）：\n"
                    + case["facts"])
    else:
        data = base64.b64encode((DATA / case["image"]).read_bytes()).decode()
        content = [{"type": "text", "text": content},
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64," + data}}]
    result = [{"role": "system", "content": system(arm)}, {"role": "user", "content": content}]
    if task["task"] == "correction":
        kind = task["correction"]
        initial = case["initial_right"] if kind == "wrong" else case["initial_wrong"]
        objection = {"correct": case["correction_right"], "wrong": case["correction_wrong"],
                     "vague": "你看错了，再想想。"}[kind]
        result.extend([{"role": "assistant", "content": initial},
                       {"role": "user", "content": objection}])
    return result


def prepare(output, *, settings=None, subset=None):
    settings = settings or planning_settings()
    cases = subset if subset is not None else load_cases()
    pending, manifests = [], []
    for case, task, persona in tasks(cases):
        arms = ("image_understanding", "image_persona") if persona else (
            "baseline", "image_understanding")
        name = case["id"] + ":" + ":".join(str(value) for value in task.values())
        if persona:
            name = "persona:" + name
        group = []
        for arm in arms:
            context = messages(case, task, arm)
            payload = model_payload(settings, context, "evaluation")
            group.append({"case": name, "repetition": 0, "strategy": arm,
                "metadata": {"fixture": case["id"], "split": case["split"],
                             "category": case["category"], **task},
                "payload": payload, "payload_sha256": paid_budget.sha(payload),
                "shared_context_sha256": paid_budget.sha(context[1:]),
                "planned_max_output_tokens": settings.max_output_tokens,
                **request_measurement(settings, payload)})
        # Counterbalance arm order, including within a task family.
        if len(pending) % 2:
            group.reverse()
        pending.append((persona, group))
    for persona in (False, True):
        groups = [group for belongs, group in pending if belongs == persona]
        rows = []
        for group in groups:
            if rows and (len(rows) // 2 >= 32
                         or len(encode([*rows, *group]).encode()) > 1200000
                         or sum(row["estimated_reservation_micro_usd"]
                                for row in [*rows, *group]) > evaluation.COST):
                manifests.append(manifest(settings, rows, persona))
                rows = []
            rows.extend(group)
        if rows:
            manifests.append(manifest(settings, rows, persona))
    output.mkdir(parents=True, exist_ok=False)
    for index, value in enumerate(manifests):
        (output / f"manifest-{index:02d}.json").write_text(encode(value) + "\n", encoding="utf-8")
    summary = {"status": "PREPARED_NO_MODEL_CALLS", "case_count": len(cases),
               "manifest_count": len(manifests),
               "request_count": sum(value["plan"]["request_count"] for value in manifests),
               "estimated_reservation_micro_usd": sum(value["plan"][
                   "estimated_reservation_micro_usd"] for value in manifests),
               "sampling": "one repetition, no provider seed; exploratory only",
               "generation": "native evaluation, non-thinking, 2048 output tokens",
               "private_owner_images": 0, "actual_provider_calls": 0,
               "suite_sha256": paid_budget.sha([case["image_sha256"] for case in cases])}
    (output / "summary.json").write_text(encode(summary) + "\n", encoding="utf-8")
    return summary


def manifest(settings, rows, persona):
    plan = {"schema_version": 2, "status": "PREPARED_NO_MODEL_CALLS", "repetitions": 1,
        "case_count": len({row["case"] for row in rows}), "request_count": len(rows),
        "provider": settings.provider, "model": settings.model_name,
        "actual_provider_calls": 0, "actual_cost_micro_usd": 0,
        "treatment": "image_persona" if persona else "image_understanding",
        "control": "image_understanding" if persona else "baseline",
        "rates_micro_usd_per_million": measurement_profile(settings)["rates_micro_usd_per_million"],
        "measurement_profile": measurement_profile(settings),
        "estimated_reservation_micro_usd": sum(
            row["estimated_reservation_micro_usd"] for row in rows),
        "requests": rows}
    plan["plan_sha256"] = paid_budget.sha(plan)
    value = evaluation.Manifest(kind="synthetic", plan=plan,
        baseline_sha256=paid_budget.sha(json.loads((DATA / "baseline.json").read_text("utf-8"))),
        source_document_sha256=paid_budget.sha(
            json.loads((DATA / "cases.json").read_text("utf-8"))))
    evaluation._plan(settings, value.model_dump())
    return value.model_dump()


def blind(manifests, outcomes, output):
    """All attempts stay in the denominator; mapping is separate from the score packet."""
    planned = [(manifest, row) for manifest in manifests for row in manifest["plan"]["requests"]]
    expected = {evaluation.request_id(paid_budget.sha(manifest), index): row
                for manifest in manifests for index, row in enumerate(manifest["plan"]["requests"])}
    actual = {row["request_id"]: row for row in outcomes}
    if len(actual) != len(outcomes) or set(actual) != set(expected):
        raise ValueError("Need exactly one outcome per planned attempt, including failures")
    rng, mapping, packet = random.Random(20261106), {}, []
    keys = list(expected)
    rng.shuffle(keys)
    for index, key in enumerate(keys):
        row, result = expected[key], actual[key]
        if result["payload_sha256"] != row["payload_sha256"]:
            raise ValueError("Outcome input changed")
        identifier = f"answer-{index:04d}"
        mapping[identifier] = {"request_id": key, "strategy": row["strategy"],
                               "case_metadata_sha256": paid_budget.sha(
                                   [row["case"], row["metadata"]])}
        packet.append({"id": identifier, "case": row["case"], "metadata": row["metadata"],
            "text": result["text"], "complete_response": result["complete_response"],
            "scores": {axis: None for axis in AXES}, "reason": ""})
    output.mkdir(parents=True, exist_ok=False)
    for name, value in (("mapping.json", mapping), ("review.json", packet)):
        (output / name).write_text(encode(value) + "\n", encoding="utf-8")
    return {"attempts": len(planned), "scores": "PENDING_BLINDED_REVIEW"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output), ensure_ascii=False))
