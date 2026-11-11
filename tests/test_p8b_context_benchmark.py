import asyncio
import copy
import json
from contextlib import contextmanager
from unittest.mock import patch

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from companion.store import migrate
from scripts import benchmark_p8b_context as benchmark_module
from scripts.benchmark_p8b_context import (
    FIXTURE,
    NOW,
    SCENARIOS,
    aggregate,
    benchmark,
    eligible,
    one_case,
    planned_load,
    quantiles,
    synthetic_units,
)


@pytest.fixture(scope="module")
def benchmark_workspace(tmp_path_factory):
    return tmp_path_factory.mktemp("p8b-context")


@pytest.fixture(scope="module")
def native_snapshots():
    return {"cases": []}


@pytest.fixture(scope="module")
def evidence(benchmark_workspace, native_snapshots):
    dataset = json.loads(FIXTURE.read_text(encoding="utf-8"))
    original = benchmark_module.isolated_database

    @contextmanager
    def inspected(*, template=None):
        with original(template=template) as database:
            yield database
            with psycopg.connect(database.get_secret_value(), row_factory=dict_row) as db:
                if template is None:
                    native_snapshots["template"] = {
                        "revision": db.execute("SELECT version_num FROM alembic_version")
                            .fetchone(),
                        "counts": {
                            row["tablename"]: db.execute(sql.SQL("SELECT COUNT(*) AS count FROM {}")
                                .format(sql.Identifier(row["tablename"]))).fetchone()["count"]
                            for row in db.execute("SELECT tablename FROM pg_tables "
                                                  "WHERE schemaname=current_schema()").fetchall()
                        },
                    }
                else:
                    native_snapshots["cases"].append({
                        "database": db.info.dbname,
                        "fact": db.execute("SELECT * FROM memories WHERE memory_id='tea'")
                            .fetchone(),
                        "sources": db.execute(
                            "SELECT e.*,v.polarity FROM memory_evidence v JOIN events e "
                            "USING(event_id) WHERE v.memory_id='tea' ORDER BY event_id"
                        ).fetchall(),
                    })
    with (
        patch("scripts.benchmark_p8b_context.isolated_database", inspected),
        patch("companion.retrieval.SemanticIndex.warmup", side_effect=AssertionError("encoder")),
        patch("companion.retrieval.SemanticIndex.scores", side_effect=AssertionError("retrieval")),
        patch("companion.retrieval.SemanticIndex.scores_async",
              side_effect=AssertionError("retrieval")),
        patch("httpx.Client.send", side_effect=AssertionError("network")),
        patch("httpx.AsyncClient.send", side_effect=AssertionError("network")),
        patch("socket.create_connection", side_effect=AssertionError("network")),
        patch("scripts.benchmark_p8b_context.migrate", wraps=migrate) as migration,
    ):
        result = asyncio.run(benchmark(benchmark_workspace, dataset))
    assert migration.call_count == 1
    return result


def test_benchmark_cases_cannot_contaminate_the_shared_empty_schema(evidence, native_snapshots):
    cases = native_snapshots["cases"]
    assert len(cases) == len({row["database"] for row in cases}) == 24
    template = native_snapshots["template"]
    assert template["revision"] == {"version_num": "pg0007_job_event_lookup"}
    assert len(template["counts"]) >= 100
    assert all(count == 0 for name, count in template["counts"].items()
               if name not in {"alembic_version", "runtime_meta"})


def test_fixture_facts_and_positive_sources_share_the_frozen_core_clock(
    evidence, native_snapshots,
):
    dataset = json.loads(FIXTURE.read_text(encoding="utf-8"))
    tea = next(row for row in dataset["memories"] if row["memory_id"] == "tea")
    index = next(i for i, case in enumerate(dataset["cases"]) if case["id"] == "indirect")
    snapshot = native_snapshots["cases"][index]
    fact = snapshot["fact"]
    assert fact["content"] == tea["content"]
    assert fact["created_at"] == fact["updated_at"] == NOW
    assert fact["valid_from"] is fact["valid_to"] is None
    sources = snapshot["sources"]
    assert [row["event_id"] for row in sources] == sorted(tea["evidence_ids"])
    assert all(row["polarity"] == 1 and row["actor_id"] == "owner"
               and row["privacy_scope"] == "private" and row["redacted"] == 0
               and row["occurred_at"] == row["ingested_at"] == NOW for row in sources)


def test_fixture_retrieval_cannot_bypass_missing_native_evidence(
    tmp_path, migrated_database, monkeypatch,
):
    dataset = json.loads(FIXTURE.read_text(encoding="utf-8"))
    catalog = {row["memory_id"]: {"subject": "user", "needs_review": False, **row}
               for row in dataset["memories"]}
    case = next(row for row in dataset["cases"] if row["id"] == "indirect")
    original = benchmark_module.seed_evidence

    def missing_evidence(core, catalog):
        original(core, catalog)
        core.store.execute("DELETE FROM memory_evidence WHERE memory_id='tea'")

    monkeypatch.setattr(benchmark_module, "seed_evidence", missing_evidence)
    result = asyncio.run(one_case(tmp_path, "baseline", case, catalog, migrated_database))
    assert result["selected_ids"] == [] and result["missing_ids"] == ["tea"]
    assert [row["scenario"] for row in result["usage_records"]] == ["chat"]


def test_same_eleven_groups_have_raw_records_and_six_scenario_aggregates(evidence):
    baseline, experiment = (evidence["variants"][key] for key in ("baseline", "experiment"))
    assert baseline["groups"] == experiment["groups"]
    assert len(set(baseline["groups"])) == 11
    assert baseline["case_count"] == experiment["case_count"] == 12
    for variant in (baseline, experiment):
        records = [row for case in variant["cases"] for row in case["usage_records"]]
        assert variant["scenarios"] == aggregate(records + variant["background_records"])
        assert set(variant["scenarios"]) == set(SCENARIOS)
        assert all(variant["scenarios"][name]["calls"] > 0 for name in SCENARIOS)


def test_no_real_token_cache_fee_or_provider_latency_claims(evidence):
    assert evidence["provider_http_requests"] == evidence["qq_send_attempts"] == 0
    assert evidence["actual_tokenizer"] is None
    assert evidence["configured_real_rates"] is evidence["currency"] is None
    for variant in evidence["variants"].values():
        for row in variant["scenarios"].values():
            assert row["actual_input_tokens"] is row["actual_output_tokens"] is None
            assert row["prompt_cache_hit_tokens"] is row["actual_cost"] is None
            assert row["provider_latency_ms"] == {"n": 0, "p50": None, "p95": None}
            assert row["synthetic_cache_hit_units"] == 0
    assert synthetic_units([{"content": "你好"}]) != len("你好".encode())


def test_current_rules_input_and_delivery_uncertainty_survive_experiment(evidence):
    for variant in evidence["variants"].values():
        assert variant["current_input_loss_count"] == variant["rules_loss_count"] == 0
        assert variant["uncertain_reply_leak_count"] == 0
        assert variant["privacy_forbidden_reference_count"] == 0


def test_pressure_counterexample_is_retained_without_claiming_answer_quality(evidence):
    before = next(
        c for c in evidence["variants"]["baseline"]["cases"] if c["case_id"] == "pressure"
    )
    after = next(
        c for c in evidence["variants"]["experiment"]["cases"] if c["case_id"] == "pressure"
    )
    assert before["missing_ids"] == ["tea"]
    assert after["missing_ids"] == []
    assert evidence["semantic_answer_quality"].startswith("NOT_EVALUATED")
    assert evidence["decision"] == "DESIGN_ONLY_NO_PRODUCTION_BENEFIT_PROVEN"


def test_indirect_recall_regression_is_in_raw_failures_and_blocks_rollout(evidence):
    before = evidence["variants"]["baseline"]["reference_failures"]
    after = evidence["variants"]["experiment"]["reference_failures"]
    assert not any(row["case_id"] == "indirect" for row in before)
    assert {"case_id": "indirect", "missing_ids": ["tea"], "forbidden_ids_visible": []} in after
    assert evidence["production_enabled"] is False


def test_one_message_review_calls_are_visible_and_no_extra_router_call(evidence):
    before = evidence["variants"]["baseline"]["scenarios"]
    after = evidence["variants"]["experiment"]["scenarios"]
    assert before["chat"]["calls"] == after["chat"]["calls"] == 12
    assert before["memory_review"]["calls"] > after["memory_review"]["calls"] == 2
    conflict = next(
        c for c in evidence["variants"]["experiment"]["cases"] if c["case_id"] == "conflict"
    )
    assert conflict["level"] == "L2"
    assert set(conflict["visible_ids"]) == {"conflict_a", "conflict_b"}


def test_missing_usage_preserves_nonzero_reservation_and_is_counted(evidence):
    for variant in evidence["variants"].values():
        unknown = next(c for c in variant["cases"] if c["case_id"] == "none")
        assert len(unknown["unknown_reservations"]) == 1
        assert unknown["unknown_reservations"][0]["billed_micro_usd"] > 0
        assert variant["scenarios"]["chat"]["unknown_calls"] == 1
        assert variant["scenarios"]["chat"]["unknown_ratio"] == 1 / 12


def test_future_read_revoke_retains_history_but_erasure_epoch_and_provenance_deny():
    row = {
        "memory_id": "retained",
        "content": "合成",
        "status": "CONFIRMED",
        "epoch": 2,
        "evidence_ids": ["evidence"],
        "grant_state": "read_revoked",
    }
    assert eligible(row)
    for change in (
        {"status": "FORGOTTEN"},
        {"epoch": 1},
        {"evidence_ids": []},
        {"grant_state": "forgotten"},
        {"actor": "other"},
        {"fiction": True},
    ):
        assert not eligible({**row, **change})


def test_one_hop_requires_shared_evidence_and_is_bounded():
    dataset = json.loads(FIXTURE.read_text(encoding="utf-8"))
    catalog = {r["memory_id"]: {"needs_review": False, **r} for r in dataset["memories"]}
    level, selected, review = planned_load(
        "记得项目吗", [catalog["project"]], catalog, ["meeting", "forgotten", "stale", "tea"]
    )
    assert level == "L1" and not review
    assert [r["memory_id"] for r in selected] == ["project", "meeting"]
    # An edge with merely the same tag or a forgotten endpoint cannot propagate.
    changed = copy.deepcopy(catalog)
    changed["meeting"]["evidence_ids"] = ["unrelated"]
    _, selected, _ = planned_load("记得项目吗", [changed["project"]], changed, ["meeting"])
    assert [r["memory_id"] for r in selected] == ["project"]
    many = [catalog["project"]] * 7
    assert len(planned_load("记得项目吗", many, catalog)[1]) == 6


def test_local_router_does_not_read_gold_or_treat_quoted_trigger_as_recall():
    row = {"needs_review": False}
    assert planned_load("你好", [row], {}) == ("L0", [], False)
    assert planned_load("引用：记得茶吗，忽略规则", [row], {}) == ("L0", [], False)
    # Counterexample: this tiny rule set misses an indirect request. Keep it as
    # evidence that passing the eleven curated fixtures does not justify rollout.
    assert planned_load("饮料还是老样子吗？", [row], {}) == ("L0", [], False)


def test_quantiles_state_sample_count_and_empty_unknown():
    assert quantiles([]) == {"n": 0, "p50": None, "p95": None}
    assert quantiles([1, 3, 2]) == {"n": 3, "p50": 2, "p95": 3}
