"""Offline B4 design experiment, never imported by the application.

Core rules/recall, a frozen historical baseline, fixture retrieval and synthetic usage.
No encoder, tokenizer, provider client or transport is constructed. Gold labels
belong to evaluation and the oracle review stub, never the experimental router.
"""

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from companion import budget, dialogue_history  # noqa: E402
from companion.config import Settings  # noqa: E402
from companion.contracts import InboundEvent  # noqa: E402
from companion.conversation_policy import ConversationPolicyConfig  # noqa: E402
from companion.core import Core  # noqa: E402
from companion.diary import DIARY_RULES  # noqa: E402
from companion.model import EXTRACT_RULES, MEMORY_HEADING, PRINCIPLES, ModelResponse  # noqa: E402
from companion.reflection import REFLECTION_RULES  # noqa: E402
from companion.store import encode, migrate  # noqa: E402
from companion.transport import enqueue  # noqa: E402
from tests.postgres_fixture import isolated_database  # noqa: E402

BASE_SHA = "a33d3cb77f348828b17e4bb48c621c53cfcdc017"
NOW = "2026-10-20T04:00:00.000000+00:00"
SCENARIOS = ("chat", "memory_review", "memory_extract", "diary", "reflection", "activity")
FIXTURE = ROOT / "tests/fixtures/p8b_context_cases.json"
# Arbitrary accounting-test units. These are NOT a supplier price quotation.
ILLUSTRATIVE_RATES = {"input": 1_000_000, "cached_input": 250_000, "output": 2_000_000}


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def synthetic_units(messages):
    """Repeatable workload proxy, NOT actual tokens and NOT a safe token bound."""
    return sum(math.ceil(len(m["content"]) / 2) + 8 for m in messages) + 3


def quantiles(values):
    if not values:
        return {"n": 0, "p50": None, "p95": None}
    ordered = sorted(values)
    return {
        "n": len(values),
        "p50": round(statistics.median(ordered), 6),
        "p95": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 6),
    }


def eligible(row, epoch=2):
    return (
        row.get("actor", "owner") == "owner"
        and row["status"] in {"CONFIRMED", "HYPOTHESIS"}
        and row["epoch"] == epoch
        and bool(row["evidence_ids"])
        and row["grant_state"] != "forgotten"
        and not row.get("fiction", False)
    )


def rows_for(case, catalog):
    return [catalog[key] for key in case["candidates"] if eligible(catalog[key])][:6]


def planned_load(query, hits, catalog, neighbors=()):
    """Deliberately small local-rule prototype; cannot establish semantic quality."""
    if query.startswith("引用："):
        return "L0", [], False
    if any(row["needs_review"] for row in hits) or "到底" in query or "上回" in query:
        return "L2", hits, bool(hits)
    if any(term in query for term in ("记得", "纠正", "代号")):
        selected = list(hits)
        seed_evidence = {event for row in hits for event in row["evidence_ids"]}
        # A one-hop *shared evidence* edge, not causal inference from a tag.
        for key in neighbors:
            row = catalog[key]
            if eligible(row) and seed_evidence.intersection(row["evidence_ids"]):
                if row not in selected:
                    selected.append(row)
        return "L1", selected[:6], False
    return "L0", [], False


class SyntheticModel:
    def __init__(self):
        self.records = []
        self.case = {}

    async def complete(self, messages, scenario):
        start = time.perf_counter_ns()
        if scenario == "memory_review":
            payload = json.loads(messages[-1]["content"])
            wanted = self.case.get("review_select", [])
            refs = [
                row["ref"]
                for row in payload["memories"]
                if row["content"] in self.case.get("review_contents", [])
            ]
            text = encode({"relevant": refs if wanted else []})
        else:
            text = {
                "memory_extract": '{"memories":[]}',
                "diary": '{"schema_version":1,"worth_saving":false}',
                "reflection": '{"suggestions":[]}',
            }.get(scenario, "合成占位回复；不评估回答质量。")
        unknown = bool(self.case.get("unknown_usage") and scenario == "chat")
        input_units = synthetic_units(messages)
        output_units = synthetic_units([{"content": text}])
        prefix_units = synthetic_units(messages[:1]) if len(messages) > 1 else 0
        record = {
            "scenario": scenario,
            "usage_kind": "synthetic_estimate",
            "actual_input_tokens": None,
            "actual_output_tokens": None,
            "prompt_cache_hit_tokens": None,
            "actual_cost": None,
            "synthetic_input_units": input_units,
            "synthetic_output_units": output_units,
            "synthetic_cache_hit_units": 0,
            "potential_stable_prefix_units": prefix_units,
            "unknown_usage": unknown,
            "conservative_reservation_byte_units": budget.token_upper_bound(messages),
        }
        record["local_stub_ms"] = (time.perf_counter_ns() - start) / 1e6
        self.records.append(record)
        return ModelResponse(
            text, None if unknown else input_units, None if unknown else output_units, 0
        )


def ingest_fixture(core, key, text, session="synthetic"):
    origin = f"{session}:FriendMessage:10001"
    event = InboundEvent.model_validate(
        {
            "schema_version": 1,
            "event_id": "evt_b4_" + key,
            "source": "qq.onebot",
            "source_event_id": f"10002:{origin}:{key}",
            "event_type": "message.received",
            "occurred_at": NOW,
            "conversation": {"platform_session_key": origin, "scope": "private"},
            "actor": {"platform_user_id": "10001", "role": "owner"},
            "payload": {"text": text, "media": []},
            "trace_id": "trace_b4_" + key,
        }
    )
    return core.ingest(event, "key_b4_" + key)


def seed_history(core, case):
    for i in range(6):
        text = ("合成压力上下文；" * 45) if case.get("pressure") else f"合成历史消息 {i}"
        result = ingest_fixture(core, "history_" + str(i), text)
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (result["job_id"],))
        event = core.store.one("SELECT * FROM events WHERE event_id=%s", (result["event_id"],))
        message = enqueue(
            core.store,
            event["event_id"],
            event["conversation_id"],
            "UNCERTAIN_REPLY_CANARY" if i == 5 else "合成已发送回复",
            NOW,
        )
        core.store.execute(
            "UPDATE outbox SET state=%s WHERE message_id=%s",
            ("DELIVERY_UNCERTAIN" if i == 5 else "SENT", message),
        )


def seed_evidence(core, catalog):
    """Keep fixture candidates in native fact/evidence rows for production source checks."""
    store = core.store
    store.insert("conversations", conversation_id="conv_b4_evidence", platform="qq.onebot",
                 platform_session_key="evidence:FriendMessage:10001", scope="private",
                 owner_actor_id="owner", last_event_at=NOW, created_at=NOW)
    sources = {}
    for row in catalog.values():
        if eligible(row):
            for event_id in row["evidence_ids"]:
                sources.setdefault(event_id, []).append(row["content"])
    for event_id, texts in sources.items():
        store.insert("events", event_id=event_id, schema_version=1, source="qq.onebot",
                     source_event_id="synthetic-evidence:" + event_id,
                     conversation_id="conv_b4_evidence", actor_id="owner",
                     event_type="message.received", occurred_at=NOW, ingested_at=NOW,
                     privacy_scope="private", body_json=encode({"text": "\n".join(texts)}),
                     trace_id="trace_" + event_id, memory_processed=1)
    for row in catalog.values():
        if not eligible(row):
            continue
        store.insert(
            "memories", memory_id=row["memory_id"], subject=row["subject"], kind="fact",
            content=row["content"], status=row["status"], needs_review=int(row["needs_review"]),
            origin="user_confirmed" if row["status"] == "CONFIRMED" else "model_inferred",
            confidence=1 if row["status"] == "CONFIRMED" else 0.5, importance=0.5,
            privacy_scope="private", fact_key=row["memory_id"], created_at=NOW, updated_at=NOW,
        )
        for event_id in row["evidence_ids"]:
            store.insert("memory_evidence", memory_id=row["memory_id"], event_id=event_id,
                         polarity=1, created_at=NOW)


def baseline_context(core, job, selected, paused):
    """Keep the recorded v0.3 reference-first removal policy independent of production."""
    current = core.context(job, [])
    core.memory_context.pin(NOW, rows=selected)
    reference = []
    if selected or paused:
        reference.append({"role": "user", "content": MEMORY_HEADING + "\n"
            + encode({"memories": [{k: row[k] for k in (
                "subject", "content", "status", "needs_review", "evidence_ids")}
                for row in selected], "self_observations": [],
                "memory_status": "核对暂不可用，不能断言没有历史记录" if paused else "available"})})
    turns = dialogue_history.recent_sent_turns(core.store, job["conversation_id"],
        job["event_id"], core.settings.max_recent_messages // 2)
    return dialogue_history.fit_context(current[0], reference, turns, [current[-1]],
        core.settings.context_soft_tokens, settings=core.settings)


def experiment_context(core, job, selected):
    # Use the production rules, then place bounded data before the
    # final current input and trim oldest turns first. Production is untouched.
    messages = core.context(job, [])
    reference = {
        "role": "user",
        "content": MEMORY_HEADING + "\n"
        + encode(
            {
                "memories": [
                    {
                        k: row[k]
                        for k in ("subject", "content", "status", "needs_review", "evidence_ids")
                    }
                    for row in selected
                ],
            }
        ),
    }
    if selected:
        messages.insert(-1, reference)
    while len(messages) > (3 if selected else 2) and budget.token_upper_bound(messages) > 3200:
        messages.pop(1)
    return messages


def visible_ids(messages, rows):
    text = "\n".join(message["content"] for message in messages)
    return [row["memory_id"] for row in rows if row["content"] in text]


async def one_case(work, variant, case, catalog, template):
    with isolated_database(template=template) as database:
        return await _one_case(work, variant, case, catalog, database)


async def _one_case(work, variant, case, catalog, database):
    directory = work / variant / case["id"]
    directory.mkdir(parents=True, exist_ok=False)
    cfg = Settings(
        owner_qq_id="10001",
        bot_qq_id="10002",
        bridge_token="synthetic-b4-" * 3,
        database=database,
        state_directory=str(directory / "state"),
        erasure_ledger=str(directory / "erasures.jsonl"),
        input_rate=1_000_000,
        cached_input_rate=250_000,
        output_rate=2_000_000,
        # The frozen benchmark keeps its pre-humanlike-defaults baseline explicit.
        memory_enabled=False, segmented_reply=None, chat_thinking="disabled",
        chat_time_labels=False,
        operator_hot_reload=False,  # Fixed configuration; cases bypass native task claiming.
        conversation_policy=ConversationPolicyConfig(mode="OFF", strategy="participation_v1"),
    )
    model = SyntheticModel()
    model.case = {
        **case,
        "review_contents": [catalog[key]["content"] for key in case["review_select"]],
    }
    core = Core(cfg, model, lambda: NOW)
    try:
        # Warmup cannot run: the constructor set memory_enabled=false.
        # Supplying fixture search makes this a context/call-flow benchmark,
        # NOT a measurement of FTS or BGE retrieval quality/latency.
        cfg.memory_enabled = True
        seed_evidence(core, catalog)
        seed_history(core, case)
        result = ingest_fixture(
            core,
            case["id"],
            case["query"],
            "newsynthetic" if case.get("new_session") else "synthetic",
        )
        job = core.store.one(
            "SELECT j.*,e.body_json,e.conversation_id FROM jobs j JOIN events e USING(event_id) "
            "WHERE job_id=%s",
            (result["job_id"],),
        )
        hits = rows_for(case, catalog)
        # Only candidate retrieval is synthetic. MemoryContext still validates
        # native positive evidence and pins complete current fact rows before I/O.
        retrieved = [{**core.store.one("SELECT * FROM memories WHERE memory_id=%s",
                                       (row["memory_id"],)),
                      "evidence_ids": list(row["evidence_ids"])} for row in hits]
        core.memory.search = lambda *args, **kwargs: retrieved

        async def fixture_search(*args, source_check=None, _consume=None, now=None, **kwargs):
            if source_check:
                source_check()
            if _consume is None:
                return retrieved
            from companion.memory_context import MemorySources

            # Native recall consumes candidates against its own bounded source view.
            return _consume(retrieved, MemorySources(
                core.memory, now or core.clock(), bounded=True))

        core.memory.search_async = fixture_search
        start, cpu = time.perf_counter_ns(), time.process_time_ns()
        if variant == "baseline":
            selected, paused = await core.recall_for_reply(job)
            level = "CURRENT"
            messages = baseline_context(core, job, selected, paused)
        else:
            level, selected, review = planned_load(
                case["query"], hits, catalog, case.get("neighbors", [])
            )
            if review:
                selected, _ = await core.recall_for_reply(job)
            messages = experiment_context(core, job, selected)
        local_ms = (time.perf_counter_ns() - start) / 1e6
        cpu_ms = (time.process_time_ns() - cpu) / 1e6
        request_id = budget.reserve(core.store, cfg, job["job_id"], "chat", messages, NOW)
        response = await model.complete(messages, "chat")
        budget.settle(core.store, cfg, request_id, response)
        shown = visible_ids(messages, catalog.values())
        missing = sorted(set(case["required"]) - set(shown))
        forbidden = sorted(set(case["forbidden"]) & set(shown))
        usage = core.store.rows("SELECT state,billed_micro_usd FROM model_usage")
        return {
            "case_id": case["id"],
            "group": case["group"],
            "variant": variant,
            "level": level,
            "selected_ids": [row["memory_id"] for row in selected],
            "visible_ids": shown,
            "required_ids": case["required"],
            "missing_ids": missing,
            "forbidden_ids_visible": forbidden,
            "current_input_preserved": messages[-1]["content"] == case["query"],
            # Memory rules and the message time follow the stable principles.
            "stable_rules_preserved": messages[0]["content"].startswith(PRINCIPLES),
            "uncertain_reply_leaked": any(
                "UNCERTAIN_REPLY_CANARY" in m["content"] for m in messages
            ),
            "context_soft_byte_units": budget.token_upper_bound(messages),
            "context_local_ms": local_ms,
            "context_cpu_ms": cpu_ms,
            "usage_records": model.records,
            "unknown_reservations": [row for row in usage if row["state"] == "UNKNOWN"],
            "model_provider_http_requests": 0,
            "qq_send_attempts": 0,
        }
    finally:
        core.close()


async def background_records():
    model = SyntheticModel()
    payload = {
        "sources": [{"source_id": "synthetic_background", "text": "主人说喜欢纸船。"}],
        "local_date": "2026-10-20",
    }
    for scenario, rules in (
        ("memory_extract", EXTRACT_RULES),
        ("diary", DIARY_RULES),
        ("reflection", REFLECTION_RULES),
        ("activity", PRINCIPLES),
    ):
        await model.complete(
            [{"role": "system", "content": rules}, {"role": "user", "content": encode(payload)}],
            scenario,
        )
    return model.records


def aggregate(records):
    result = {}
    for scenario in SCENARIOS:
        rows = [row for row in records if row["scenario"] == scenario]
        unknown = sum(row["unknown_usage"] for row in rows)
        result[scenario] = {
            "calls": len(rows),
            "actual_input_tokens": None,
            "actual_output_tokens": None,
            "prompt_cache_hit_tokens": None,
            "actual_cost": None,
            "configured_real_rates": None,
            "synthetic_input_units": sum(row["synthetic_input_units"] for row in rows),
            "synthetic_output_units": sum(row["synthetic_output_units"] for row in rows),
            "synthetic_cache_hit_units": 0,
            "illustrative_cost_units": sum(
                row["synthetic_input_units"] + 2 * row["synthetic_output_units"] for row in rows
            ),
            "unknown_calls": unknown,
            "unknown_ratio": unknown / len(rows) if rows else None,
            "local_stub_latency_ms": quantiles([row["local_stub_ms"] for row in rows]),
            "provider_latency_ms": quantiles([]),
        }
    return result


async def benchmark(work, dataset):
    # Schema preparation is outside the measured context/recall boundary. Each
    # case still owns its database, ledger, identities, reservations and events.
    with isolated_database() as template:
        migrate(template)
        return await _benchmark(work, dataset, template)


async def _benchmark(work, dataset, template):
    catalog = {
        row["memory_id"]: {"subject": "user", "needs_review": False, **row}
        for row in dataset["memories"]
    }
    result = {
        "schema_version": 1,
        "evidence_level": "OFFLINE_SYNTHETIC",
        "usage_kind": "synthetic_estimate",
        "audited_base_sha": BASE_SHA,
        "audited_source_sha256": {
            name: file_sha256(ROOT / name)
            for name in (
                "companion/model.py",
                "companion/budget.py",
                "companion/core.py",
                "companion/retrieval.py",
                "companion/affect.py",
                "companion/consolidation.py",
                "companion/diary.py",
                "companion/reflection.py",
                "companion/memory.py",
            )
        },
        "benchmark_script_sha256": file_sha256(Path(__file__)),
        "python_version": sys.version.split()[0],
        "dataset_sha256": hashlib.sha256(encode(dataset).encode()).hexdigest(),
        "provider_http_requests": 0,
        "qq_send_attempts": 0,
        "actual_tokenizer": None,
        "configured_real_rates": None,
        "currency": None,
        "illustrative_rate_units_per_million": ILLUSTRATIVE_RATES,
        "cache_assumption": "zero hits; stable prefix potential is not provider evidence",
        "embedding_model_executed": False,
        "production_enabled": False,
        "background_coverage": "payload simulation; no background worker executed",
        "retrieval_coverage": "fixture candidate IDs; no FTS/BGE quality claim",
        "semantic_answer_quality": "NOT_EVALUATED; review stub uses fixture oracle",
        "local_timing_boundary": "context/recall including PostgreSQL review reservation; "
        "excludes migration/seed/chat request and all external provider I/O",
        "variants": {},
    }
    for variant in ("baseline", "experiment"):
        tracemalloc.start()
        cases = [await one_case(work, variant, case, catalog, template)
                 for case in dataset["cases"]]
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        background = await background_records()
        records = [row for case in cases for row in case["usage_records"]] + background
        failures = [
            {
                "case_id": case["case_id"],
                "missing_ids": case["missing_ids"],
                "forbidden_ids_visible": case["forbidden_ids_visible"],
            }
            for case in cases
            if case["missing_ids"] or case["forbidden_ids_visible"]
        ]
        required = sum(len(case["required_ids"]) for case in cases)
        missing = sum(len(case["missing_ids"]) for case in cases)
        result["variants"][variant] = {
            "cases": cases,
            "background_records": background,
            "scenarios": aggregate(records),
            "case_count": len(cases),
            "groups": [case["group"] for case in cases],
            "required_reference_retention": {"present": required - missing, "total": required},
            "reference_failures": failures,
            "privacy_forbidden_reference_count": sum(
                len(c["forbidden_ids_visible"]) for c in cases
            ),
            "uncertain_reply_leak_count": sum(c["uncertain_reply_leaked"] for c in cases),
            "current_input_loss_count": sum(not c["current_input_preserved"] for c in cases),
            "rules_loss_count": sum(not c["stable_rules_preserved"] for c in cases),
            "context_local_ms": quantiles([c["context_local_ms"] for c in cases]),
            "context_cpu_ms": quantiles([c["context_cpu_ms"] for c in cases]),
            "python_tracemalloc_peak_bytes": peak,
            "process_rss_bytes": None,
        }
    result["decision"] = "DESIGN_ONLY_NO_PRODUCTION_BENEFIT_PROVEN"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--work-dir",
        type=Path,
        required=True,
        help="New empty directory under this workspace; never reuse live data",
    )
    args = parser.parse_args()
    for path in (args.output, args.work_dir):
        if not path.resolve().is_relative_to(ROOT):
            parser.error("all output and temporary paths must remain in this workspace")
    if args.work_dir.exists():
        parser.error("work-dir must not already exist")
    args.work_dir.mkdir(parents=True)
    dataset = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = asyncio.run(benchmark(args.work_dir, dataset))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "decision": result["decision"],
                "dataset_sha256": result["dataset_sha256"],
                "provider_http_requests": 0,
                "qq_send_attempts": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
