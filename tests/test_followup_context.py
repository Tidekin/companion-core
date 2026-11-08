"""Shared dialogue/episode context through HTTP and an independent original Life worker."""

import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import paid_budget
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from companion.store import after
from companion.transport import enqueue
from companion_onebot_bridge.config import BridgeConfig
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.life_worker_integration import process_worker
from tests.support.owner_authority import activate
from tests.support.paid_model import approved_live
from tests.support.reading_followup import admit
from tests.support.turn_taking import enable, history_messages


@pytest.mark.parametrize("change", ["none", "erase_during", "erase_before_delivery",
                                  "without_history"])


def test_followup_uses_ten_turns_and_eligible_episodes_under_native_custody(core, tmp_path, change):
    enable(core)
    core.settings.max_recent_messages = 20
    core.settings.context_soft_tokens = 12000
    core.clock.now = datetime.fromisoformat("2026-11-01T23:50:00+00:00")
    first_at = core.clock()
    marker = "那次望远镜项目里讨论过的光学问题"
    approved_live(core, tmp_path,
                  max_output_tokens=4096, )
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    key = Ed25519PrivateKey.generate()
    config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
        bot_qq_id=core.settings.bot_qq_id,
        bridge_token=core.settings.bridge_token.get_secret_value(),
        onebot_token="s" * 32, protocol_epoch="followup-context-epoch")
    scoped = change != "without_history"
    policy = OwnerGrantPolicy(owner_qq_id=core.settings.owner_qq_id,
        purposes=["chat", "proactive", *(["history", "memory"] if scoped else [])],
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
                 "identity_sha256": paid_budget.sha([config.owner_qq_id, config.bot_qq_id]),
                 "protocol_epoch": config.protocol_epoch},
        memory_scope="owner_private" if scoped else "none",
        history={"scope": "owner_private", "not_before": after(first_at, -1),
                 "max_turns": 15, "max_utf8_bytes": 16000} if scoped else {})
    authority = activate(core, policy, operation="followup-context-scope")[0]["authority"]
    grant = SimpleNamespace(policy=policy, key=key, authority=authority)

    async def exercise():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as http:
            sources = []
            for number in range(1, 14):
                if number > 1:
                    core.clock.now = datetime.fromisoformat("2026-11-02T00:00:00+00:00")
                    core.clock.advance(number * 60)
                text = marker if number == 1 else (
                    "我正在上早八，要去教室" if number == 4 else f"当前话题第{number}句")
                incoming, accepted = await admit(http, core, config, grant, number, text)
                sources.append(incoming["event_id"])
                # Signed HTTP inputs plus synthetic already-delivered history are
                # prior state; the independent worker executes the actual follow-up.
                with core.store.transaction():
                    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s",
                                       (accepted["job_id"],))
                    conversation = core.store.one((
                        'SELECT conversation_id FROM events WHERE event_id=%s'
                    ), (incoming["event_id"],))["conversation_id"]
                    message_id = enqueue(core.store, incoming["event_id"], conversation,
                                         "这点我明白了。", core.clock())
                    core.store.execute((
                        "UPDATE outbox SET state='SENT',sent_at=%s WHERE "
                        'message_id=%s'
                    ), (core.clock(), message_id))
            core.settings.memory_enabled = True
            source = core.store.one('SELECT * FROM events WHERE event_id=%s', (sources[0],))
            core.memory.episodes([source], core.clock())
            core.clock.advance(900)

            async def erase():
                with core.store.transaction(control_write=True):
                    core.ledger.expire_events(core.store, [sources[0]], core.clock())

            observed = await process_worker(core, tmp_path, "followup", sources[-1],
                interrupt=erase if change == "erase_during" else None)
            assert observed["provider_calls"] == 1
            messages = observed["prompts"][0]
            history = history_messages(messages)
            episodes = [m for m in messages if m["role"] == "user"
                        and m["content"].startswith("相关情景接续")]
            if change == "without_history":
                assert history == episodes == []
                assert marker not in json.dumps(messages, ensure_ascii=False)
            else:
                assert len(history) == 10
                assert history[0]["content"] == "【11月2日 周一 08:04】我正在上早八，要去教室"
                assert marker in json.dumps(episodes, ensure_ascii=False)
            assert "2026年11月2日 星期一 08:28（Asia/Shanghai）" in messages[-2]["content"]
            assert "turns" not in observed["materials"][0]
            usage = core.store.rows("SELECT state FROM model_usage WHERE scenario='activity'")
            assert usage == [{"state": "SETTLED"}]
            if change == "erase_before_delivery":
                await erase()
            leased = await http.post("/internal/v1/deliveries/lease", json={
                "worker_id": "context-bridge", "authority": authority})
            assert leased.status_code == 200, leased.text
            items = leased.json()["items"]
            if change.startswith("erase_"):
                assert items == []
            else:
                assert [item["action_type"] for item in items] == ["qq.private.proactive"]
            if change == "none":
                restarted = await process_worker(core, tmp_path, "followup", sources[-1],
                                                 restart=True)
                assert restarted["provider_calls"] == 0
                assert len(core.store.rows("SELECT 1 FROM model_usage "
                                           "WHERE scenario='activity'")) == 1

    run(exercise())
