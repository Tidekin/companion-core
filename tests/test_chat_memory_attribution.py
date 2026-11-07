"""Chat keeps the distinction between a lasting fact, an episode and original speakers."""

import json

from companion.contracts import Candidate
from companion.model import MEMORY_HEADING, RELATIONSHIP_HEADING
from tests.conftest import run
from tests.support.dialogue_context import current_job, sent_history


def test_chat_memory_preserves_episode_kind_and_confirmed_fact(core):
    core.settings.memory_enabled = True
    core.settings.context_soft_tokens = 16000
    statements = [
        ('以后我们就叫你雪狐吧', '好，以后就用雪狐这个称呼。', 'fact', True),
        ('今天临时演一下猫侍卫', '今天演猫侍卫，称呼还是雪狐。', 'episode', False),
    ]
    memories = []
    for number, (owner, reply, kind, confirmed) in enumerate(statements, 1):
        sent_history(core, number, owner, reply)
        candidate = Candidate(subject='relationship', kind=kind, fact_key='name',
            content=owner, evidence_event_id=f'evt_test_{number}', evidence_quote=owner,
            speaker='owner')
        key = core.memory.add(candidate, core.clock(), confirmed=confirmed)
        row = core.store.one('SELECT * FROM memories WHERE memory_id=%s', (key,))
        row['evidence_ids'] = [f'evt_test_{number}']
        memories.append(row)
    before = core.store.rows('SELECT * FROM memories ORDER BY record_sequence')
    snapshots = []
    job = current_job(core, text='雪狐和猫侍卫都是长期称呼吗？')
    messages = core.context(job,
                            memories=memories, snapshots=snapshots)
    blocks = {
        heading: [memory for message in messages if isinstance(message['content'], str)
                  and message['content'].startswith(heading)
                  for memory in json.loads(message['content'].split('\n', 1)[1])['memories']]
        for heading in (RELATIONSHIP_HEADING, MEMORY_HEADING)
    }
    assert [m['content'] for m in blocks[RELATIONSHIP_HEADING]] == [statements[0][0]]
    assert [m['content'] for m in blocks[MEMORY_HEADING]] == [statements[1][0]]
    supplied = [*blocks[RELATIONSHIP_HEADING], *blocks[MEMORY_HEADING]]
    assert [(memory['kind'], memory['status'], memory['origin']) for memory in supplied] == [
        ('fact', 'CONFIRMED', 'user_confirmed'), ('episode', 'HYPOTHESIS', 'model_inferred')]
    assert all(memory['needs_review'] == 0 for memory in supplied)
    assert [memory['evidence_ids'] for memory in supplied] == [['evt_test_1'], ['evt_test_2']]
    # The model receives the words with their actual roles, not a synthesized owner claim.
    for owner, reply, _, _ in statements:
        assert {'role': 'user', 'content': owner} in messages
        assert {'role': 'assistant', 'content': reply} in messages
        assert {'role': 'user', 'content': reply} not in messages
    assert core.store.rows('SELECT * FROM memories ORDER BY record_sequence') == before
    assert any(proof['kind'] == 'memory' for proof in snapshots)
    assert not core.model.calls
    run(core.recall_for_reply(job))
    request, scenario = core.model.calls[-1]
    assert scenario == 'memory_review'
    candidates = json.loads(request[-1]['content'])['memories']
    assert {(row['kind'], row['origin']) for row in candidates} == {
        ('fact', 'user_confirmed'), ('episode', 'model_inferred')}
