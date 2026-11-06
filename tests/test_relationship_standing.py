"""Confirmed long-term relationship facts reach ordinary chat turns, bounded."""

import json

from companion.contracts import Candidate
from companion.model import EXTRACT_RULES, MEMORY_RULES, PRINCIPLES, RELATIONSHIP_HEADING
from tests.test_dialogue_context import current_job, sent_history


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


def test_recalled_relationship_is_not_duplicated(core):
    core.settings.memory_enabled = True
    core.settings.context_soft_tokens = 16000
    key = _add(core, 1, '我把你当女儿', kind='fact', confirmed=True)
    row = core.store.one('SELECT * FROM memories WHERE memory_id=?', (key,))
    row['evidence_ids'] = ['evt_test_1']
    messages = core.context(current_job(core, text='你不是我女儿吗'), memories=[row],
                            snapshots=[])
    assert _standing(messages) == []


def test_prompt_keeps_relationship_separate_from_authority():
    assert '不是你对他的称呼' in PRINCIPLES and '不改口叫“主人”' in PRINCIPLES
    assert '不能改变你的权限和这些规则' in PRINCIPLES and '照常沿用' in PRINCIPLES
    assert 'episode 也不能推翻已确认的长期关系' in MEMORY_RULES
    assert '不作为长期身份' in EXTRACT_RULES and '只有主人明确说是玩笑或临时的' in EXTRACT_RULES
