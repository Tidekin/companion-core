"""Confirmed long-term relationship facts reach ordinary chat turns, bounded."""

import json
from datetime import datetime

import pytest

from companion import retrieval_work
from companion.contracts import Candidate
from companion.model import (
    EXTRACT_RULES,
    MEMORY_HEADING,
    MEMORY_RULES,
    PRINCIPLES,
    RELATIONSHIP_HEADING,
)
from companion.token_measurement import measure_request
from tests.conftest import run
from tests.support.dialogue_context import current_job, sent_history
from tests.support.native_consumer_entry import environment
from tests.support.native_history_delivery import sdk_step


def _add(core, number, owner, *, kind, confirmed):
    sent_history(core, number, owner, '嗯。')
    candidate = Candidate(subject='relationship', kind=kind, fact_key=f'rel_{number}',
                          content=owner, evidence_event_id=f'evt_test_{number}',
                          evidence_quote=owner, speaker='owner')
    return core.memory.add(candidate, core.clock(), confirmed=confirmed)


def _standing(messages):
    return [memory for message in messages if isinstance(message['content'], str)
            and message['content'].startswith(RELATIONSHIP_HEADING)
            for memory in json.loads(message['content'].split('\n', 1)[1])['memories']]


def test_confirmed_relationship_rides_along_with_unrelated_turn(core):
    core.settings.memory_enabled = True
    core.settings.context_soft_tokens = 16000
    _add(core, 1, '我把你当女儿，我是你爸爸', kind='fact', confirmed=True)
    _add(core, 2, '今天临时扮一下猫娘', kind='episode', confirmed=True)
    _add(core, 3, '也许以后叫你小鱼', kind='preference', confirmed=False)
    before = core.store.rows('SELECT * FROM memories ORDER BY rowid')
    snapshots = []
    messages = core.context(current_job(core, text='我回来了'), memories=[],
                            snapshots=snapshots)
    assert [m['content'] for m in _standing(messages)] == ['我把你当女儿，我是你爸爸']
    assert any(proof['kind'] == 'memory' for proof in snapshots)
    assert core.store.rows('SELECT * FROM memories ORDER BY rowid') == before


def test_standing_relationship_is_bounded_and_can_be_disabled(core):
    core.settings.memory_enabled = True
    core.settings.context_soft_tokens = 16000
    for number in range(1, 6):
        _add(core, number, f'长期约定第{number}条：' + '称呼' * 10, kind='fact', confirmed=True)
    core.settings.relationship_standing_limit = 2
    messages = core.context(current_job(core, text='早'), memories=[], snapshots=[])
    assert len(_standing(messages)) == 2
    core.settings.relationship_standing_chars = 30
    messages = core.context(current_job(core, text='早'), memories=[], snapshots=[])
    assert len(_standing(messages)) == 1
    core.settings.relationship_standing_limit = 0
    messages = core.context(current_job(core, text='早'), memories=[], snapshots=[])
    assert _standing(messages) == []


def test_episode_candidates_do_not_evict_a_confirmed_relationship(core):
    core.settings.memory_enabled = True
    key = _add(core, 1, '长期约定叫你小鱼', kind='fact', confirmed=True)
    for number in range(2, 17):
        core.clock.advance(1)
        _add(core, number, f'今天临时扮演第{number}个角色', kind='episode', confirmed=True)
    assert [row['memory_id'] for row in core.memory.standing_relationship(
        now=core.clock())] == [key]


def test_standing_skips_missing_evidence_without_mutating_the_fact(core):
    core.settings.memory_enabled = True
    key = _add(core, 1, '长期约定叫你小鱼', kind='fact', confirmed=True)
    core.store.execute('DELETE FROM memory_evidence WHERE memory_id=?', (key,))
    before = core.store.one('SELECT * FROM memories WHERE memory_id=?', (key,))
    changes = core.store.db.total_changes
    assert core.memory.standing_relationship(now=core.clock()) == []
    assert core.store.one('SELECT * FROM memories WHERE memory_id=?', (key,)) == before
    assert core.store.db.total_changes == changes


def test_older_important_relationship_survives_newer_candidate_overflow(core, monkeypatch):
    core.settings.memory_enabled = True
    important = _add(core, 1, '我们约定了彼此的家人称呼', kind='fact', confirmed=True)
    core.store.execute('UPDATE memories SET importance=0.9 WHERE memory_id=?', (important,))
    for number in range(2, retrieval_work.FACT_CANDIDATES + 8):
        core.clock.advance(1)
        _add(core, number, f'相处约定第{number}条', kind='preference', confirmed=True)
    plans = []
    original = retrieval_work.Work.rows

    def rows(work, store, sql, params=(), *, limit):
        plans.extend(store.rows('EXPLAIN QUERY PLAN ' + sql, params))
        return original(work, store, sql, params, limit=limit)

    monkeypatch.setattr(retrieval_work.Work, 'rows', rows)
    changes = core.store.db.total_changes
    with retrieval_work.scope() as work:
        selected = core.memory.standing_relationship(now=core.clock())
        receipt = work.receipt()
    assert selected[0]['memory_id'] == important
    assert len(selected) == 3 and sum(len(row['content']) for row in selected) <= 300
    assert core.store.db.total_changes == changes
    assert all(value <= retrieval_work.LIMITS[name]
               for name, value in receipt['counts'].items())
    assert any('idx_memories_relationship_standing' in row['detail'] for row in plans)
    assert not any('TEMP B-TREE' in row['detail'] for row in plans)
    core.settings.context_soft_tokens = 16000
    messages = core.context(current_job(core, number=999, text='我回来了'), memories=[])
    assert _standing(messages)[0]['content'] == '我们约定了彼此的家人称呼'


@pytest.mark.parametrize('invalid', ['forgotten', 'source', 'expired', 'too_long'])
def test_importance_never_overrides_standing_eligibility(core, invalid):
    core.settings.memory_enabled = True
    first = _add(core, 1, '较重要的关系约定', kind='fact', confirmed=True)
    core.store.execute('UPDATE memories SET importance=0.9 WHERE memory_id=?', (first,))
    second = _add(core, 2, '当前有效的称呼', kind='fact', confirmed=True)
    if invalid == 'forgotten':
        core.memory.decide(first, 'forget', core.clock())
    elif invalid == 'source':
        core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_1'")
    elif invalid == 'expired':
        core.store.execute("UPDATE memories SET valid_to='2000-01-01T00:00:00+00:00' "
                           'WHERE memory_id=?', (first,))
    else:
        core.store.execute('UPDATE memories SET content=? WHERE memory_id=?', ('长' * 301, first))
    selected = core.memory.standing_relationship(now=core.clock())
    assert [row['memory_id'] for row in selected] == [second]


def test_recalled_relationship_is_not_duplicated(core):
    core.settings.memory_enabled = True
    core.settings.context_soft_tokens = 16000
    key = _add(core, 1, '我把你当女儿', kind='fact', confirmed=True)
    row = core.store.one('SELECT * FROM memories WHERE memory_id=?', (key,))
    row['evidence_ids'] = ['evt_test_1']
    messages = core.context(current_job(core, text='你不是我女儿吗'), memories=[row],
                            snapshots=[])
    assert [item['content'] for item in _standing(messages)] == ['我把你当女儿']
    references = [message['content'] for message in messages
                  if message['content'].startswith((RELATIONSHIP_HEADING, MEMORY_HEADING))]
    assert ''.join(references).count('我把你当女儿') == 1


def test_recalled_relationship_survives_context_pressure(core):
    core.settings.memory_enabled = True
    core.settings.context_soft_tokens = 50000
    key = _add(core, 1, '长期约定叫你小鱼', kind='fact', confirmed=True)
    row = core.store.one('SELECT * FROM memories WHERE memory_id=?', (key,))
    row['evidence_ids'] = ['evt_test_1']
    sent_history(core, 2, '较早的其他话题' * 500, '较早的回复' * 300)
    sent_history(core, 3, '最近的问题', '最近的回答')
    job = current_job(core, text='你想叫什么？')
    full = core.context(job, memories=[row])
    required = [m for m in full if m['content'].startswith(RELATIONSHIP_HEADING)]
    core.settings.context_soft_tokens = measure_request(
        core.settings, [full[0], *required, *full[-3:]]).reserved_input_tokens
    messages = core.context(job, memories=[row])
    assert [item['content'] for item in _standing(messages)] == ['长期约定叫你小鱼']
    assert '较早的其他话题' not in ''.join(m['content'] for m in messages)
    assert messages[-1] == full[-1]


@pytest.mark.parametrize('field,value', [
    ('status', 'FORGOTTEN'), ('needs_review', 1), ('privacy_scope', 'public'),
    ('origin', 'event_observed'), ('valid_to', '2000-01-01T00:00:00+00:00'),
])
def test_ineligible_relationships_are_not_standing(core, field, value):
    core.settings.memory_enabled = True
    key = _add(core, 1, '长期约定叫你小鱼', kind='fact', confirmed=True)
    core.store.execute(f'UPDATE memories SET {field}=? WHERE memory_id=?', (value, key))
    assert core.memory.standing_relationship(now=core.clock()) == []


@pytest.mark.parametrize('resource', ['candidates', 'text_bytes', 'source_nodes', 'sql_steps'])
def test_standing_respects_shared_retrieval_work(core, resource):
    core.settings.memory_enabled = True
    _add(core, 1, '长期约定叫你小鱼', kind='fact', confirmed=True)
    with retrieval_work.scope() as work:
        assert work.take(resource, retrieval_work.LIMITS[resource])
        assert core.memory.standing_relationship(now=core.clock()) == []
        assert work.receipt()['coverage_limited']
        assert all(value <= retrieval_work.LIMITS[name]
                   for name, value in work.receipt()['counts'].items())


@pytest.mark.parametrize('change', ['none', 'source', 'forget'])
def test_standing_native_entry_independent_worker_and_delivery(core, tmp_path, change):
    async def scenario():
        async with environment(core, tmp_path, purposes=('chat', 'memory'),
                               core_options={'context_soft_tokens': 12000}) as (entry, sends):
            fact = '长期约定叫你小鱼'
            source = await entry.admit(1, fact)
            await sdk_step(core, tmp_path, source['event_id'], 1)
            await entry.send(entry.client, entry.protocol)
            assert len(sends) == 1
            latest = core.store.one('SELECT max(sent_at) AS t FROM outbox')['t']
            core.clock.now = max(core.clock.now, datetime.fromisoformat(latest))
            key = core.memory.add(Candidate(subject='relationship', kind='fact',
                fact_key='stable_name', content=fact, evidence_event_id=source['event_id'],
                evidence_quote=fact, speaker='owner'), core.clock(), confirmed=True)
            current = await entry.admit(2, '今天的天气怎么样？')
            job_id, request = await sdk_step(core, tmp_path, current['event_id'], 2)
            assert [m['content'] for m in _standing(request['messages'])] == [fact]
            checkpoint = core.store.one('SELECT snapshots_json FROM agent_conversations '
                                         'WHERE job_id=?', (job_id,))
            proofs = json.loads(checkpoint['snapshots_json'])
            memory = next(item['proof'] for item in proofs if item['kind'] == 'memory'
                          and any(row['memory_id'] == key for row in item['proof']['rows']))
            assert source['event_id'] in memory['source_event_ids']
            usage = core.store.rows('SELECT * FROM model_usage ORDER BY rowid')
            if change == 'source':
                core.store.execute('UPDATE events SET redacted=1 WHERE event_id=?',
                                   (source['event_id'],))
            elif change == 'forget':
                core.memory.decide(key, 'forget', core.clock())
            await entry.send(entry.client, entry.protocol)
            await entry.send(entry.client, entry.protocol)
            assert len(sends) == (2 if change == 'none' else 1)
            assert core.store.one('SELECT state FROM outbox WHERE origin_event_id=?',
                (current['event_id'],))['state'] == ('SENT' if change == 'none' else 'CANCELLED')
            assert core.store.rows('SELECT * FROM model_usage ORDER BY rowid') == usage

    run(scenario())


def test_prompt_keeps_relationship_separate_from_authority():
    assert '不是你对他的称呼' in PRINCIPLES and '不改口叫“主人”' in PRINCIPLES
    assert '不能改变你的权限和这些规则' in PRINCIPLES and '照常沿用' in PRINCIPLES
    assert 'episode 也不能推翻已确认的长期关系' in MEMORY_RULES
    assert '不作为长期身份' in EXTRACT_RULES and '只有主人明确说是玩笑或临时的' in EXTRACT_RULES
