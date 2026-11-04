"""Authenticated TCP input -> original chat settlement -> separate Life process/restart."""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import paid_budget
from companion.memory_context import MemoryContext
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from companion.store import encode
from companion_delivery_protocol.authority import capture_signing_payload
from tests.conftest import event, run
from tests.support.core_http import real_core_http
from tests.support.owner_authority import activate
from tests.test_diary import DAY
from tests.test_paid_model_gate import approved_live


def test_authenticated_ingress_and_independent_nightly_steps_restart(core, tmp_path):
    approved_live(core, tmp_path)
    cfg = core.settings
    cfg.memory_enabled = cfg.diary_enabled = True
    cfg.reflection_enabled = cfg.consolidation_enabled = True
    workspace = Path(__file__).resolve().parents[1]
    cfg.extraction_min_events = 3
    cfg.extraction_idle_seconds = 0
    cfg.consolidation_catchup_days = 1
    cfg.reflection_daily_micro_usd = 50000
    cfg.reflection_monthly_micro_usd = 100000
    later = core.clock.now
    core.clock.now = datetime.fromisoformat(DAY.isoformat() + "T04:00:00+00:00")
    key = Ed25519PrivateKey.generate()
    cfg.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    policy = OwnerGrantPolicy(owner_qq_id=cfg.owner_qq_id,
        purposes=["chat", "memory", "diary", "consolidation", "reflection"],
        authorization_sha256="d" * 64, memory_scope="owner_private",
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(cfg)),
        capture=dict(public_key=key.public_key().public_bytes_raw().hex(),
                     identity_sha256=paid_budget.sha([cfg.owner_qq_id, cfg.bot_qq_id]),
                     protocol_epoch="nightly-test-epoch"))
    granted, _ = activate(core, policy)

    def chat_response(request):
        assert request.url.host == "synthetic.invalid"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "已收到这条项目记录。"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    async def scenario():
        async with (
            real_core_http(core) as port,
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False,
                headers={"Authorization": "Bearer " + cfg.bridge_token.get_secret_value()},
            ) as client,
            httpx.AsyncClient(transport=httpx.MockTransport(chat_response)) as transport,
        ):
            core.model = ModelClient(cfg, transport)
            texts = ("青禾项目先检查配色", "松林项目整理阅读笔记", "周末再核对资料")
            for number, text in enumerate(texts, 1):
                payload = event(core, number, text,
                    origin="onebot-direct:FriendMessage:" + cfg.owner_qq_id)
                source = (f"{cfg.bot_qq_id}:{payload.conversation.platform_session_key}:"
                          f"epoch:nightly-test-epoch:{number}")
                payload = payload.model_copy(update={"source_event_id": source,
                    "event_id": "evt_" + hashlib.sha256(source.encode()).hexdigest()})
                from companion.qq_rich import input_digest

                capture = dict(authority=granted["authority"], kind="authenticated_onebot_ws",
                    source_event_id=source, event_id=payload.event_id,
                    input_sha256=input_digest(payload.payload.model_dump(mode="json")),
                    frame_sha256="b" * 64, evidence_sha256="c" * 64,
                    identity_sha256=policy.capture.identity_sha256,
                    protocol_epoch=policy.capture.protocol_epoch,
                    occurred_at=payload.occurred_at.isoformat(),
                    received_at=payload.occurred_at.isoformat())
                capture["signature"] = key.sign(capture_signing_payload(capture)).hex()
                approved = await client.post("/internal/v1/owner-authority/inputs/approve",
                                              json=capture)
                assert approved.status_code == 200, approved.text
                accepted = await client.post("/internal/v1/inbox/events",
                    headers={"X-Idempotency-Key": payload.event_id},
                    json=payload.model_dump(mode="json"))
                assert accepted.status_code == 202, accepted.text
                assert await core.run_once()
            core.clock.now = later
            queued_before = core.store.one("SELECT COUNT(*) AS n FROM outbox")["n"]
            config = cfg.model_dump(mode="json")
            config.update(model_key=cfg.model_key.get_secret_value(),
                          bridge_token=cfg.bridge_token.get_secret_value())
            path = tmp_path / "nightly-settings.json"
            await asyncio.to_thread(path.write_text, encode(config), encoding="utf-8")
            results = []
            for attempt in range(3):
                child = await asyncio.create_subprocess_exec(
                    sys.executable, "-B", "-c",
                    "import sys; from tests.support.nightly_process import run_worker; "
                    "run_worker(sys.argv[1], sys.argv[2], sys.argv[3]=='1')",
                    str(path), core.clock(), "1" if attempt == 0 else "0",
                    cwd=workspace,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(child.communicate(), 30)
                except BaseException:
                    child.kill()
                    await child.communicate()
                    raise
                assert child.returncode == 0, stderr.decode("utf-8", errors="replace")
                result = json.loads(stdout.decode("utf-8").splitlines()[-1])
                assert result["pid"] != os.getpid()
                assert all(unit["admissions"] <= 1 for unit in result["units"])
                results.append(result)
            assert results[0]["calls"] == ["memory_extract"]
            assert results[1]["calls"] == ["memory_review", "diary", "reflection"]
            assert results[2]["calls"] == []
            assert core.consolidation.status(DAY)["state"] == "COMPLETE"
            assert core.store.one("SELECT COUNT(*) AS n FROM outbox")["n"] == queued_before
            conversation = core.store.one("SELECT conversation_id FROM events LIMIT 1")
            assert MemoryContext(core.memory).continuation(
                conversation["conversation_id"], core.clock(),
            )["items"]
            assert len(core.store.rows("SELECT * FROM memories")) == 2
            assert core.store.rows(
                "SELECT status,importance FROM memories ORDER BY importance") == [
                {"status": "HYPOTHESIS", "importance": 0.3},
                {"status": "HYPOTHESIS", "importance": 0.8}]
            episode = core.store.one("SELECT * FROM episodes WHERE status='ACTIVE'")
            ids = [row["event_id"] for row in core.store.rows(
                "SELECT event_id FROM episode_events WHERE episode_id=? ORDER BY event_id",
                (episode["episode_id"],))]
            changes = core.store.db.total_changes
            summary = core.memory.episode_summary(episode["episode_id"], ids, core.clock())
            assert summary["kind"] == "model_summary" and summary["title"] == "项目安排"
            assert "仍是待办事项" in summary["text"] and summary["text"] == episode["summary"]
            assert core.store.db.total_changes == changes
            assert core.store.one("SELECT scenario,state FROM model_usage WHERE request_id=?",
                                   (summary["request_id"],)) == {
                "scenario": "memory_extract", "state": "SETTLED"}
            checkpoint = core.store.one("SELECT value FROM runtime_meta "
                                         "WHERE key='memory_extraction_checkpoint'")
            assert "episode_summaries" not in json.loads(checkpoint["value"])["batch"]
            assert not core.store.one("SELECT 1 FROM model_usage WHERE state!='SETTLED'")
    run(scenario())
