"""Frozen campaign path: signed source, SDK recall, erasure and native fee closure."""

import hashlib
import json
import sys
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import owner_authority, paid_budget
from companion.contracts import Candidate
from companion.core import Core
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, call
from tests.support.owner_authority import activate
from tests.support.paid_model import approved_live


def test_native_campaign_exam_keeps_memory_and_settled_liability(core, tmp_path):
    runner = Path(__file__).resolve().parents[1] / "benchmarks/memory_campaign/runner"
    sys.path.insert(0, str(runner))
    if "scripts" in sys.modules:
        sys.modules["scripts"].__path__.append(str(runner / "scripts"))
    from scripts.memory_benchmark.native import Harness, execution_audit

    core.settings.memory_enabled = True
    approved_live(core, tmp_path, concurrency_basis="local_active_attempts_v2")
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    key = Ed25519PrivateKey.generate()
    purposes = ["chat", "memory", "history"]
    policy = OwnerGrantPolicy(
        owner_qq_id=core.settings.owner_qq_id, purposes=purposes,
        memory_scope="owner_private", authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capability_policies={k: v for k, v in owner_authority.capability_hashes(
            core.settings).items() if k in purposes},
        history={"scope": "owner_private", "not_before": core.clock(),
                 "max_turns": 14, "max_utf8_bytes": 32000},
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
                 "identity_sha256": paid_budget.sha([
                     core.settings.owner_qq_id, core.settings.bot_qq_id]),
                 "protocol_epoch": "public-memory-benchmark-v1"},
    )
    authority = activate(core, policy)[0]["authority"]
    provider = Provider(
        answer(calls=[call("recall", "memory_recall", {"query": "相机存放位置"})]),
        answer("相机放在湖蓝色箱子里"),
    )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            with Core(core.settings, ModelClient(core.settings, http), core.clock) as worker:
                harness = Harness(worker, key, authority, tmp_path)
                def unexpected_failure(job, code):
                    raise AssertionError("native campaign failed: " + code) from sys.exception()
                worker.fail_job = unexpected_failure
                text = "相机放在湖蓝色箱子里"
                harness.import_turn({"轮次": 1, "用户消息": text, "助手回复": "记住了"})
                event_id = next(iter(harness.turn_ids))
                with worker.store.transaction(control_write=True):
                    identity = worker.memory.add(Candidate(
                        subject="project", kind="fact", fact_key="camera", content=text,
                        evidence_event_id=event_id, evidence_quote=text),
                        worker.clock(), confirmed=True)
                await harness.exam({"id": "synthetic/d00", "question": "相机放在哪？",
                                    "cutoff_turn": 1})
                record = json.loads((tmp_path / "answers.jsonl").read_text("utf8"))
                assert record["status"] == "SUCCEEDED", record
                assert text in record["answer"]
                assert worker.store.one("SELECT status FROM memories WHERE memory_id=%s",
                                        (identity,)) == {"status": "CONFIRMED"}
                assert worker.store.one("SELECT redacted FROM events WHERE event_id=%s",
                                        (record["event_id"],)) == {"redacted": 1}
                usage = worker.store.rows("SELECT state,billed_micro_usd FROM model_usage")
                assert len(usage) == 2 and all(row["state"] == "SETTLED" for row in usage)
                assert all(row["billed_micro_usd"] > 0 for row in usage)
                audit = execution_audit(worker)
                assert audit["unclosed_requests"] == []
                assert audit["journal_sha256"] == hashlib.sha256(
                    Path(worker.settings.paid_model.journal_path).read_bytes()  # noqa: ASYNC240 -- local test receipt
                ).hexdigest()
                from companion.ops_status import snapshot

                observed = snapshot(worker.settings, integrity=True)
                assert observed["resources"]["budget_journal"]["checked"] == "FULL_JOURNAL"
                assert audit["journal_sha256"] == hashlib.sha256(
                    Path(worker.settings.paid_model.journal_path).read_bytes()  # noqa: ASYNC240 -- local test receipt
                ).hexdigest()
                json.dumps(observed)
    run(scenario())
