"""Recent delivered dialogue survives image bursts through the real native entry point."""

import asyncio
import json

import httpx
import pytest

from companion.store import encode
from tests.conftest import run
from tests.support.live_image_input import image_parts, options, png, resolve
from tests.support.native_images import image_host, upload
from tests.support.onebot_events import raw_event
from tests.support.onebot_images import image_segment
from tests.support.owner_image_recovery import run_worker


@pytest.mark.parametrize("image_count,image_limit", [(3, 3), (5, 5), (8, "unlimited"), (5, 3)])
def test_image_burst_keeps_recent_complete_dialogue_in_independent_worker(
    core, tmp_path, image_count, image_limit,
):
    incoming = asyncio.Queue()

    async def source(ws):
        await ws.send(await incoming.get())
        await ws.wait_closed()

    async def scenario():
        scope = options()
        scope['core_options'].update(context_soft_tokens=2000, recent_protected_turns=2,
                                     max_recent_messages=20)
        scope['paid_options'].update(max_input_tokens=60000)
        scope['paid_options']['image_input'] = scope['paid_options']['image_input'].model_copy(
            update={'max_images_per_request': image_limit, 'estimated_tokens_per_image': 1000})
        expected_images = (image_count if image_limit == 'unlimited'
                           else min(image_count, image_limit))
        async with image_host(core, tmp_path, source, options=scope, history=True,
                              independent_api=True) as (active, _, sends, _):
            async def accept(number, text=None):
                message = ([{'type': 'text', 'data': {'text': text}}] if text is not None
                           else [image_segment(f'https://gchat.qpic.cn/synthetic-{number}')])
                await incoming.put(encode(raw_event(number,
                    time=int(core.clock.now.timestamp()), message=message)))
                await active.capture(number)
                await active.flush()

            dialogue = [
                ('我们平时怎么称呼你？', '我们一直用雪狐这个称呼。'),
                ('戴上猫耳会怎样？', '戴猫耳是今天的玩笑，称呼还是雪狐。'),
            ]
            for number, (question, reply) in enumerate(dialogue, 1):
                await accept(number, question)
                await run_worker(core, tmp_path, number, reply)
                assert await active.pump()
            assert len(sends) == 2
            for number in range(3, image_count + 3):
                await accept(number)
                await upload(active, resolver=resolve, transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=png(),
                                                  headers={'Content-Type': 'image/png'})))
            assert core.store.one('SELECT COUNT(*) n FROM captured_images')['n'] == image_count
            followup = image_count + 3
            await accept(followup, '你刚才怎么又这么说？')
            first = (await run_worker(core, tmp_path, followup, '刚才猫耳只是个玩笑。'))[0]
            prompt = encode(first['messages'])
            assert len(image_parts(first)) == expected_images
            originals = core.store.rows('SELECT item_id FROM captured_images ORDER BY rowid')
            selected = core.store.rows('SELECT item_id FROM agent_image_inputs WHERE job_id='
                                      '(SELECT job_id FROM jobs ORDER BY rowid DESC LIMIT 1) '
                                      'ORDER BY position')
            assert selected == originals[-expected_images:]
            assert all(row['item_id'] in prompt for row in selected)
            for question, reply in dialogue:
                assert question in prompt and reply in prompt
            assert await active.pump()

            # Pending image-only inputs must not consume the complete-dialogue floor,
            # and the next text follow-up must see the answer just delivered.
            await accept(followup + 1, '小小的也很可爱')
            second = (await run_worker(core, tmp_path, followup + 1, '接着刚才的话聊。'))[0]
            prompt = encode(second['messages'])
            assert len(image_parts(second)) == expected_images
            assert dialogue[1][1] in prompt
            assert '刚才猫耳只是个玩笑。' in prompt
            assert await active.pump()
            assert len(sends) == 4 and active.attempts() == {'ACK': 4}
            assert core.store.rows('SELECT scenario,state FROM model_usage') == [
                {'scenario': 'chat', 'state': 'SETTLED'}] * 4
            last = core.store.one('SELECT snapshots_json FROM agent_conversations '
                                  'ORDER BY rowid DESC LIMIT 1')
            plan = next(item['metadata']['core_context']
                        for item in json.loads(last['snapshots_json'])
                        if item['kind'] == 'context_plan')
            assert plan['requests'] and plan['requests'][0]['state'] == 'ADMITTED'

    run(scenario())
