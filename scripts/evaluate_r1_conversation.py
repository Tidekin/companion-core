"""Prepare fixed-context R1 comparisons and export blinded observed replies, offline.

This module has no provider transport and never sends QQ messages. Prepared inputs
are not model outputs. Real generation needs a separately authorized executor.
"""

import argparse
import hashlib
import itertools
import json
import random
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from companion import style_observation  # noqa: E402
from companion.config import Settings  # noqa: E402
from companion.contracts import Candidate, InboundEvent  # noqa: E402
from companion.conversation_policy import (  # noqa: E402
    PARTICIPATION_V1,
    ConversationPolicyConfig,
    apply_strategy,
)
from companion.core import Core  # noqa: E402
from companion.evaluation_measurement import (  # noqa: E402
    measurement_profile,
    request_measurement,
)
from companion.model import model_payload  # noqa: E402
from companion.reply_segments import SegmentedReplyConfig  # noqa: E402
from companion.store import after, encode, migrate  # noqa: E402
from companion.style_observation import StyleObservationConfig, shadow_payload  # noqa: E402
from companion.transport import enqueue  # noqa: E402
from tests.postgres_fixture import isolated_database  # noqa: E402

DATASET = ROOT / "tests/fixtures/r1_conversation_cases.json"
NOW = "2026-10-24T06:30:00.000000+00:00"
# The frozen R1 comparison keeps its pre-humanlike-defaults baseline explicit.
FROZEN_BASELINE = {
    "memory_enabled": False, "chat_thinking": "disabled",
    "conversation_policy": ConversationPolicyConfig(mode="OFF", strategy="participation_v1"),
}
STRATEGY = PARTICIPATION_V1


def sha(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def load_cases(path=DATASET):
    data = json.loads(Path(path).read_text("utf-8"))
    cases = data["cases"]
    names = [case["name"] for case in cases]
    if len(names) != len(set(names)) or not cases:
        raise ValueError("Cases must have unique names")
    for case in cases:
        if case["metadata"]["evidence_type"] != "SYNTHETIC":
            raise ValueError("The tracked dataset accepts synthetic cases only")
        if not isinstance(case["inputs"]["text"], str):
            raise ValueError("A case needs a current user text")
    return cases


def private_case(path):
    """Use only actual SENT text; never import the target answer into its input."""
    original = json.loads(Path(path).read_text("utf-8-sig"))
    turns = original["turns"]
    if not 2 <= len(turns) <= 12:
        raise ValueError("Expected a bounded original dialogue")
    history = []
    for turn in turns[:-1]:
        sent = sorted(turn["sent_segments"], key=lambda item: item["segment_index"])
        if any(item["state"] != "SENT" for item in sent):
            raise ValueError("Unsent text cannot become observed history")
        if [item["segment_index"] for item in sent] != list(range(len(sent))):
            raise ValueError("Observed reply must be a contiguous sent prefix")
        history.append({"user": turn["owner_text"],
                        "assistant": "".join(item["text"] for item in sent)})
    return {"name": "private_identity_baseline", "inputs": {
        "text": turns[-1]["owner_text"], "history": history, "memories": [],
    }, "metadata": {
        "evidence_type": "ACTUAL_VISIBLE_DIALOGUE_RECONSTRUCTION",
        "category": "private_baseline",
        "source_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        "historical_full_model_payload": False,
        "rubric": "Observe explanation, redundant questions, grounded views and truthfulness.",
    }}



def _ingest(core, name, text, *, old_session=False):
    origin = ("r1-memory" if old_session else "r1-dialogue") + ":FriendMessage:10001"
    event = InboundEvent.model_validate({
        "schema_version": 1, "event_id": "evt_r1_" + name,
        "source": "qq.onebot", "source_event_id": f"10002:{origin}:{name}",
        "event_type": "message.received", "occurred_at": core.clock(),
        "conversation": {"platform_session_key": origin, "scope": "private"},
        "actor": {"platform_user_id": "10001", "role": "owner"},
        "payload": {"text": text, "media": []}, "trace_id": "r1_" + name,
    })
    accepted = core.ingest(event, "r1_" + name)
    source = core.store.one("SELECT conversation_id FROM events WHERE event_id=%s",
                            (accepted["event_id"],))
    return {**accepted, "conversation_id": source["conversation_id"]}


def build_context(case, empty_database, directory):
    """Native source/erasure/context behavior, with fixed supplied recall candidates.

    This isolates conversation strategy from retrieval/model randomness. It does
    not measure embedding quality or recreate missing historical request fields.
    """
    with isolated_database(template=empty_database) as database:
        return _build_context(case, database, directory)


def _build_context(case, database, directory):
    settings = Settings(owner_qq_id="10001", bot_qq_id="10002", bridge_token="r1-offline-" * 4,
                        database=database, state_directory=str(directory / "state"),
                        erasure_ledger=str(directory / "erasures.jsonl"),
                        max_output_tokens=2048, context_soft_tokens=16000,
                        max_recent_messages=30, proactive_enabled=False,
                        expression_enabled=True, expression_selection_probability=0,
                        segmented_reply=SegmentedReplyConfig(mode="semantic"),
                        embedding_cache_dir=str(directory / "unused-embedding-cache"),
                        input_rate=300000, cached_input_rate=6000, output_rate=1200000,
                        **FROZEN_BASELINE)
    ticks = itertools.count()
    clock_value = [NOW]
    def clock():
        return clock_value[0]
    def stable_id():
        return uuid.uuid5(uuid.NAMESPACE_URL, f"r1:{case['name']}:{next(ticks)}")
    with patch("companion.store.uuid.uuid4", stable_id), Core(settings, clock=clock) as core:
        settings.memory_enabled = True  # Native evidence, with explicit recall rows below.
        for index, memory in enumerate(case["inputs"]["memories"]):
            accepted = _ingest(core, f"memory_{index}", memory["content"],
                               old_session=memory.get("cross_session", False))
            core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s",
                               (accepted["job_id"],))
            with core.store.transaction(control_write=True):
                mid = core.memory.add(Candidate(
                    subject="user", kind="preference", fact_key=memory.get("fact_key", str(index)),
                    content=memory["content"], evidence_event_id=accepted["event_id"],
                    evidence_quote=memory["content"],
                ), clock(), confirmed=memory["confirmed"])
            if memory.get("erase"):
                core.memory.decide(mid, "forget", clock())
            clock_value[0] = after(clock(), 60)
        for index, turn in enumerate(case["inputs"]["history"]):
            accepted = _ingest(core, f"history_{index}", turn["user"])
            core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s",
                               (accepted["job_id"],))
            if turn["assistant"]:
                message = enqueue(core.store, accepted["event_id"], accepted["conversation_id"],
                                  turn["assistant"], clock())
                core.store.execute("UPDATE outbox SET state='SENT',sent_at=%s WHERE message_id=%s",
                                   (clock(), message))
            clock_value[0] = after(clock(), 60)
        accepted = _ingest(core, "current", case["inputs"]["text"])
        job = core.claim_job("r1-offline")
        if not job or job["job_id"] != accepted["job_id"]:
            raise ValueError("Fixture failed to select its current chat")
        memories = core.memory.search("*", now=clock())
        messages = core.context(job, memories=memories)
        # The private excerpt contains valid unanswered/partial input. The lab
        # reconstructs its visible sequence directly, never asserting SENT for
        # unseen text or pretending this is the historical full provider payload.
        if case["metadata"]["evidence_type"] != "SYNTHETIC":
            messages = [messages[0]]
            for turn in case["inputs"]["history"]:
                messages.append({"role": "user", "content": turn["user"]})
                if turn["assistant"]:
                    messages.append({"role": "assistant", "content": turn["assistant"]})
            messages.append({"role": "user", "content": case["inputs"]["text"]})
        return settings, messages


STYLE = "participation_v2_bili_style"
TREATMENTS = ("participation_v1", "participation_v2", STYLE)
# CC-STYLE-04: the observation segment is compared against v2 itself, not baseline.
CONTROLS = {STYLE: "participation_v2"}
AXES = ("naturalness", "desire_to_continue", "deliberateness", "factual_accuracy")
STYLE_AXES = ("naturalness", "desire_to_continue", "deliberateness", "context_fit",
              "forced_meme", "factual_accuracy")
# Raters see only A/B; WIN/LOSE for the treatment exist only after unblinding.
CHOICES = ("A", "B", "TIE", "BOTH_BAD", "UNSCORABLE")
VERDICTS = ("WIN", "TIE", "LOSE", "BOTH_BAD", "UNSCORABLE")


def verdict(mapping_row, overall, treatment):
    """Unblind one A/B choice into the treatment's WIN/TIE/LOSE/BOTH_BAD/UNSCORABLE."""
    if overall not in CHOICES:
        raise ValueError("Unknown overall choice")
    if overall in {"A", "B"}:
        return "WIN" if mapping_row[overall] == treatment else "LOSE"
    return overall


def plan_treatment(plan):
    treatment = plan.get("treatment", "participation_v1")
    if treatment not in TREATMENTS or plan.get("treatment") == "participation_v1":
        raise ValueError("Unknown treatment strategy")
    if plan.get("control", "baseline") != CONTROLS.get(treatment, "baseline"):
        raise ValueError("Unknown treatment control")
    return treatment


def plan_control(plan):
    return CONTROLS.get(plan_treatment(plan), "baseline")


def arm(base, strategy, case, style):
    """Return (messages, style receipt). Only the system message differs between arms."""
    if strategy != STYLE:
        return apply_strategy(base, strategy), None
    return shadow_payload(base, StyleObservationConfig(mode="SHADOW"), style["candidates"],
                          current_text=case["inputs"]["text"], now=style["now"],
                          live_event_ids=style["live_event_ids"])


def measurement_defaults():
    return Settings(owner_qq_id="10001", bot_qq_id="10002", bridge_token="r1-offline-" * 4,
                    database="dbname=synthetic",
                    max_output_tokens=2048, context_soft_tokens=16000,
                    input_rate=300000, cached_input_rate=6000, output_rate=1200000,
                    segmented_reply=None, **FROZEN_BASELINE)


def prepare(cases, output, *, repetitions=1, measurement_settings=None,
            treatment="participation_v1", style=None):
    """`style` = {"candidates", "now", "live_event_ids"} only for the STYLE treatment."""
    output = Path(output).resolve()
    if not output.is_relative_to(ROOT / ".local"):
        raise ValueError("Experiment artifacts must stay in this worktree's .local directory")
    if repetitions < 1 or repetitions > 3:
        raise ValueError("Use one to three predeclared repetitions")
    if treatment not in TREATMENTS:
        raise ValueError("Unknown treatment strategy")
    if (treatment == STYLE) != (style is not None):
        raise ValueError("The bili-style treatment needs its reviewed candidates, others none")
    control = CONTROLS.get(treatment, "baseline")
    if output.exists():
        raise FileExistsError(output)
    counting = measurement_settings or measurement_defaults()
    if counting.max_output_tokens != 2048 or not counting.pricing_known:
        raise ValueError("Comparison requires fixed 2048 output allowance and configured prices")
    temporary = ROOT / ".local/test-temp"
    temporary.mkdir(parents=True, exist_ok=True)
    records = []
    with (tempfile.TemporaryDirectory(prefix="r1-prepare-", dir=temporary) as temp,
          isolated_database() as empty):
        temp = Path(temp)
        migrate(empty)
        for index, case in enumerate(cases):
            directory = temp / str(index)
            directory.mkdir()
            settings, base = build_context(case, empty, directory)
            for repeat in range(repetitions):
                order = (control, treatment) if (index + repeat) % 2 == 0 else (
                    treatment, control)
                for strategy in order:
                    messages, receipt = arm(base, strategy, case, style)
                    payload = model_payload(counting, messages, "evaluation")
                    extra = {"style_receipt": receipt} if receipt is not None else {}
                    records.append({"case": case["name"], "repetition": repeat,
                                    "strategy": strategy, "metadata": case["metadata"], **extra,
                                    "payload": payload, "payload_sha256": sha(payload),
                                    "shared_context_sha256": sha(messages[1:]),
                                    "planned_max_output_tokens": settings.max_output_tokens,
                                    **request_measurement(counting, payload)})
    profile = measurement_profile(counting)
    costs = [r["estimated_reservation_micro_usd"] for r in records]
    plan = {"schema_version": 2, "status": "PREPARED_NO_MODEL_CALLS",
            "case_count": len(cases), "repetitions": repetitions, "request_count": len(records),
            "provider": counting.provider, "model": counting.model_name, "seed_sent": False,
            "temperature_sent": False, "provider_sampling_reproducible": False,
            "measurement_profile": profile,
            "rates_micro_usd_per_million": profile["rates_micro_usd_per_million"],
            "cost_basis": "Native Measurement reservation; exact_local_render is local only. "
                          "Hosted preprocessing and billing equality are unverified.",
            "estimated_reservation_micro_usd": (None if None in costs else sum(costs)),
            "actual_provider_calls": 0, "actual_cost_micro_usd": 0, "requests": records}
    if treatment != "participation_v1":
        # Absent for v1 so earlier plans and their digests stay valid.
        plan["treatment"] = treatment
    if treatment == STYLE:
        applied = {r["case"] for r in records if r.get("style_receipt", {}).get("candidates")}
        if not applied:
            raise ValueError("No reviewed candidate applies to any case; nothing to compare")
        plan["control"] = control
        plan["style_cases_applied"] = len(applied)
        plan["style_candidates"] = sorted(
            [c.candidate_id, c.approved_version] for c in style["candidates"])
    plan["plan_sha256"] = sha(plan)
    output.mkdir(parents=True, exist_ok=False)
    (output / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
    return plan


def check_style(plan, expected, control):
    """The treatment adds only a bounded observation segment after the v2 system text."""
    for (case, repeat, strategy), row in expected.items():
        if strategy == control:
            if "style_receipt" in row:
                raise ValueError("Control requests carry no style segment")
            continue
        base = expected[(case, repeat, control)]["payload"]["messages"][0]["content"]
        system = row["payload"]["messages"][0]["content"]
        receipt = row.get("style_receipt") or {}
        added = system[len(base):]
        if (not system.startswith(base) or receipt.get("mode") != "SHADOW"
            or (added and not added.startswith(style_observation.HEADER))
            or bool(added) != bool(receipt.get("candidates"))
            or receipt.get("added_units") != style_observation.units(added)
            or receipt["added_units"] > style_observation.MAX_UNITS
            or any(ref not in plan["style_candidates"] for ref in receipt["candidates"])):
            raise ValueError("Style segment does not match its reviewed candidates")


def validate_plan(plan, measurement_settings=None):
    """Read v1 unchanged; recompute v2 evidence against explicit local count settings."""
    content = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if sha(content) != plan["plan_sha256"]:
        raise ValueError("Plan changed after preparation")
    if plan.get("schema_version") not in {1, 2}:
        raise ValueError("Unsupported experiment plan version")
    treatment, control = plan_treatment(plan), plan_control(plan)
    expected = {(r["case"], r["repetition"], r["strategy"]): r for r in plan["requests"]}
    if not expected or len(expected) != len(plan["requests"]):
        raise ValueError("Empty or duplicate planned requests")
    pairs = {}
    for (case, repeat, strategy), request in expected.items():
        payload = request["payload"]
        messages = payload.get("messages", [])
        if (strategy not in {control, treatment} or len(messages) < 2
            or messages[0].get("role") != "system"
            or sha(payload) != request["payload_sha256"]
            or sha(messages[1:]) != request["shared_context_sha256"]):
            raise ValueError("Planned payload or shared input mismatch")
        pair = pairs.setdefault((case, repeat), {})
        pair[strategy] = {**payload, "messages": messages[1:]}
    if any(set(pair) != {control, treatment}
           or pair[control] != pair[treatment] for pair in pairs.values()):
        raise ValueError("Comparison requires the same input and model parameters")
    if treatment == STYLE:
        check_style(plan, expected, control)
    if plan["schema_version"] == 2:
        settings = measurement_settings or measurement_defaults()
        profile = measurement_profile(settings)
        if plan.get("measurement_profile") != profile:
            raise ValueError("Measurement policy, source or route changed; prepare a new plan")
        if (plan["rates_micro_usd_per_million"] != profile["rates_micro_usd_per_million"]
            or (plan["provider"], plan["model"]) != (settings.provider, settings.model_name)
            or plan["request_count"] != len(expected)
            or plan["case_count"] != len({key[0] for key in expected})
            or type(plan["repetitions"]) is not int or not 1 <= plan["repetitions"] <= 3
            or set(expected) != {(case, repeat, strategy)
                for case in {key[0] for key in expected}
                for repeat in range(plan["repetitions"])
                for strategy in (control, treatment)}):
            raise ValueError("Plan scope or budget inputs changed")
        costs = []
        for row in expected.values():
            messages = row["payload"]["messages"]
            if any(set(message) != {"role", "content"}
                   or not isinstance(message["content"], str)
                   or message["role"] not in ({"system"} if i == 0 else {"user", "assistant"})
                   for i, message in enumerate(messages)):
                raise ValueError("Evaluation plans require text-only shared context")
            if row["planned_max_output_tokens"] != 2048 or settings.output_tokens(
                "evaluation"
            ) != 2048:
                raise ValueError("Output allowance changed")
            measured = request_measurement(settings, row["payload"])
            if any(row.get(key) != value for key, value in measured.items()):
                raise ValueError("Measurement receipt or budget estimate changed")
            costs.append(measured["estimated_reservation_micro_usd"])
        if plan["estimated_reservation_micro_usd"] != (None if None in costs else sum(costs)):
            raise ValueError("Plan reservation estimate changed")
    return expected


def blind_export(plan, observations, output, seed=20261024, *, measurement_settings=None,
                 expression_review=False):
    """Export every declared pair; failures stay in the denominator, never get retried."""
    expected = validate_plan(plan, measurement_settings)
    results = {}
    for row in observations:
        key = (row["case"], row["repetition"], row["strategy"])
        if key not in expected or key in results:
            raise ValueError("Unknown or duplicate observation")
        if row["payload_sha256"] != expected[key]["payload_sha256"]:
            raise ValueError("Observation belongs to another input")
        kinds = {"MODEL_RESPONSE", "FAILED_ATTEMPT"}
        if plan["schema_version"] == 2:
            kinds.add("SYNTHETIC_MODEL_RESPONSE")
        if row.get("evidence_type") not in kinds or not isinstance(
            row.get("text"), str,
        ) or row.get("state") not in {"SETTLED", "UNKNOWN", "FAILED", "NOT_STARTED"}:
            raise ValueError("Blinded replies require observed outcomes")
        if row["evidence_type"] == "FAILED_ATTEMPT" and row["text"]:
            raise ValueError("Failed attempts cannot contain invented replies")
        results[key] = row
    if set(results) != set(expected):
        raise ValueError("All declared observations are required, including failures")
    pairs = sorted({key[:2] for key in expected})
    style = plan_treatment(plan) == STYLE
    axes = STYLE_AXES if style else AXES
    # The mapping stays private until every pair is scored; see verdict().
    verdicts = {"overall_choices": list(CHOICES)} if style else {}
    positions = [index % 2 for index in range(len(pairs))]
    random.Random(seed).shuffle(positions)
    ballot, mapping = [], []
    for index, ((case, repeat), position) in enumerate(zip(pairs, positions, strict=True)):
        variants = [plan_control(plan), plan_treatment(plan)]
        if position:
            variants.reverse()
        responses = [results[(case, repeat, variant)] for variant in variants]
        pair_id = f"pair_{index + 1:03}"
        ballot.append({"pair_id": pair_id, "case": case, "repetition": repeat,
                       "input": expected[(case, repeat, variants[0])]["payload"]["messages"][1:],
                       "A": responses[0]["text"], "B": responses[1]["text"],
                       "A_state": responses[0]["state"], "B_state": responses[1]["state"],
                       "scorable": all(row["state"] == "SETTLED" and row["text"] and (
                           plan["schema_version"] == 1 or (
                               row.get("complete_response") is True
                               and row.get("finish_reason") == "stop"
                               and row.get("text_redacted") is False
                               and row["evidence_type"] == "MODEL_RESPONSE"))
                           for row in responses),
                       "ratings": {axis: None for axis in axes},
                       "overall": None, **verdicts, "failure_notes": ""})
        mapping.append({"pair_id": pair_id, "A": variants[0], "B": variants[1]})
    output = Path(output).resolve()
    if not output.is_relative_to(ROOT / ".local"):
        raise ValueError("Blinded artifacts must stay in this worktree's .local directory")
    output.mkdir(parents=True, exist_ok=False)
    for name, value in (("ballot.json", ballot), ("mapping.private.json", mapping)):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
    if expression_review:
        from scripts.evaluate_expression_repetition import assess

        reviews = [{"pair_id": row["pair_id"], **{
            side: assess(row["input"], row[side], scorable=row["scorable"])
            for side in ("A", "B")}} for row in ballot]
        (output / "expression-review.json").write_text(
            json.dumps(reviews, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return ballot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--dataset", type=Path, default=DATASET)
    prepare_parser.add_argument("--private-baseline", type=Path)
    prepare_parser.add_argument("--repetitions", type=int, default=1)
    prepare_parser.add_argument("--treatment", choices=TREATMENTS[:2], default="participation_v1")
    prepare_parser.add_argument("--output", type=Path, required=True)
    prepare_parser.add_argument("--measurement-settings", type=Path,
                                help="Explicit Settings JSON used only for counting/prices")
    export_parser = sub.add_parser("blind")
    export_parser.add_argument("--plan", type=Path, required=True)
    export_parser.add_argument("--observations", type=Path, required=True)
    export_parser.add_argument("--output", type=Path, required=True)
    export_parser.add_argument("--measurement-settings", type=Path)
    export_parser.add_argument("--expression-review", action="store_true",
                               help="Also export a blinded, read-only repetition review rubric")
    args = parser.parse_args()
    counting = (Settings.model_validate_json(args.measurement_settings.read_text("utf-8"))
                if args.measurement_settings else None)
    if args.command == "prepare":
        cases = ([private_case(args.private_baseline)] if args.private_baseline
                 else load_cases(args.dataset))
        plan = prepare(cases, args.output, repetitions=args.repetitions,
                       measurement_settings=counting, treatment=args.treatment)
        print(encode({key: value for key, value in plan.items() if key != "requests"}))
    else:
        plan = json.loads(args.plan.read_text("utf-8"))
        observations = json.loads(args.observations.read_text("utf-8"))
        print(len(blind_export(plan, observations, args.output, measurement_settings=counting,
                              expression_review=args.expression_review)))


if __name__ == "__main__":
    main()
