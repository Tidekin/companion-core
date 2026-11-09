"""A real Life draft can close without I/O across an unused Core activation."""

import asyncio
import json
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

from pydantic import SecretStr

from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion_onebot_bridge import owner_handoff
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerSendGuard
from companion_onebot_bridge.spool import Spool, encode
from tests.support.bridge_owner_resolution import child
from tests.support.evaluation_process import process_config
from tests.support.life_worker_integration import process_worker
from tests.support.onebot_events import raw_event
from tests.support.reading_followup import window


def test_native_proactive_zero_send_chain_and_bridge_cli(core, tmp_path, monkeypatch):
    core.settings.control_token = SecretStr('synthetic-proactive-control-token')
    owner = {'Authorization': 'Bearer synthetic-proactive-control-token'}

    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, base, plan):
            config = replace(base, owner_authority=plan.authority,
                capture_signing_key=plan.key.private_bytes_raw().hex(),
                data_dir=str(tmp_path / 'bridge'), core_url=str(client.base_url).rstrip('/'),
                onebot_http_url='http://127.0.0.1:3000', onebot_ws_url='ws://127.0.0.1:3001')
            worker = tmp_path / 'worker.json'

            async def work():
                process_config(core, worker)
                exported = json.loads(await asyncio.to_thread(worker.read_text, 'utf8'))
                for name, adapter in core.settings.observation_adapters.items():
                    exported['observation_adapters'][name]['token'] = (
                        adapter.token.get_secret_value())
                await asyncio.to_thread(worker.write_text, json.dumps(exported), 'utf8')
                args = ['--config', str(worker), '--now', core.clock()]
                assert await child('tests.owner_runtime_process', *args) == {
                    'worked': True, 'calls': 1}

            async def post(path, body):
                response = await client.post('/internal/v1/owner-authority/' + path,
                                             headers=owner, json=body)
                assert response.status_code == 200, response.text
                return response.json()

            async def activate(revision, policy):
                query = {'expected_revision': revision, 'policy': policy}
                preview = await post('preview', query)
                return (await post('activate', query | {
                    'operation_id': 'proactive-no-io-activate-' + str(revision),
                    'preview_sha256': preview['preview_sha256']}))['authority']

            with Spool(config) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                raw = encode(raw_event(1, time=int(core.clock.now.timestamp()),
                                       text='明天继续聊那个没有解决的导出问题'))
                event = normalize(raw, config, now=core.clock.now.timestamp())
                runtime.persist_capture(event, raw, received_at=core.clock.now,
                                        peer=('127.0.0.1', 1))
                assert await runtime.flush_one(client)
                await work()
                runtime.stop()
            await post('stop', {'authority': plan.authority,
                               'operation_id': 'proactive-no-io-initial-stop'})
            scope = LifeShadowApproval(scope='owner_private_life_shadow_v1',
                authorization_sha256='c' * 64,
                conversation_id=core.store.one('SELECT conversation_id FROM events '
                    'WHERE event_id=%s', (event['event_id'],))['conversation_id'],
                source_event_ids=[event['event_id']], purposes=['PRIVATE_MESSAGE_DRAFT'],
                expires_at=core.clock.now + timedelta(days=3), max_items=2,
                automatic_actions=['qq.private.proactive'])
            core.settings.life_loop = LifeLoopConfig(mode='SHADOW', follow_up_delay_seconds=3600)
            core.settings.wake_enabled = core.settings.proactive_enabled = True
            core.settings.quiet_start = core.settings.quiet_end = 0
            policy = plan.policy.model_copy(update={
                'purposes': [*plan.policy.purposes, 'life', 'proactive'], 'life_shadow': scope})
            with Spool(config, reconciliation_only=True) as spool:
                runtime = OwnerRuntime(spool)
                assert (await owner_handoff.prepare(runtime, client))['state'] == 'HANDOFF_PREPARED'
                authority = await activate(2, policy.model_dump(mode='json'))
                assert (await owner_handoff.handoff(runtime, client, authority))[
                    'state'] == 'ACTIVE'
            config = replace(config, owner_authority=authority)
            core.clock.advance(86401)
            assert core.life_topics.schedule()
            observed = await process_worker(core, tmp_path, 'bug', event['event_id'])
            assert observed['provider_calls'] == 1
            assert observed['results'][0]['state'] == 'SHADOW'
            assert core.life_topics.consume_due(diary=core.diary)
            with Spool(config) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                response = await client.post('/internal/v1/deliveries/lease', json={
                    'worker_id': 'bridge', 'authority': authority, 'limit': 1})
                assert response.status_code == 200, response.text
                item = response.json()['items'][0]
                assert item['action_type'] == 'qq.private.proactive'
                OwnerSendGuard(runtime, client).reserve(item)
                runtime.stop()
            await post('stop', {'authority': authority,
                               'operation_id': 'proactive-no-io-source-stop'})
            intermediate = await activate(4, policy.model_dump(mode='json'))
            await post('stop', {'authority': intermediate,
                               'operation_id': 'proactive-no-io-unbound-stop'})
            finance = core.store.rows('SELECT * FROM model_usage ORDER BY request_id')
            budget_path = Path(core.settings.paid_model.journal_path)
            journal = await asyncio.to_thread(budget_path.read_bytes)
            private_config = tmp_path / 'bridge.json'
            await asyncio.to_thread(private_config.write_text, json.dumps(asdict(config)), 'utf8')
            result = await child('tests.owner_reconcile_process', '--config', str(private_config),
                '--now', core.clock(), '--message-id', item['message_id'], '--operation', 'prepare')
            assert result['state'] == 'NOT_SENT'
            assert core.store.rows('SELECT * FROM model_usage ORDER BY request_id') == finance
            assert await asyncio.to_thread(budget_path.read_bytes) == journal
            with Spool(config, reconciliation_only=True) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                assert (await owner_handoff.prepare(runtime, client))['state'] == 'HANDOFF_PREPARED'
                successor = await activate(6, policy.model_dump(mode='json'))
                assert (await owner_handoff.handoff(runtime, client, successor))[
                    'state'] == 'ACTIVE'
            assert core.store.one('SELECT state,attempt_count,provider_message_id '
                'FROM outbox WHERE message_id=%s', (item['message_id'],)) == {
                    'state': 'CANCELLED', 'attempt_count': 0, 'provider_message_id': None}
            assert not core.store.one("SELECT 1 FROM audit_log WHERE action='delivery.result'")

    asyncio.run(scenario())
