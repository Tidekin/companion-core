"""An incorrect fact is removable without withdrawing its original evidence."""

from contextlib import closing

import pytest
from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.contracts import Candidate, CoreError, EventTime
from companion.postgres import Database
from companion.privacy import ErasureLedger, epoch
from companion.retrieval import SemanticIndex
from companion.store import Store, encode
from tests.conftest import run
from tests.support.diary import DAY, seed_day
from tests.support.owner_console import client as client
from tests.support.owner_console import login

WARMUP = SemanticIndex.warmup


def test_memory_only_record_rejects_a_source_erasure_closure(core):
    wrong, other = facts_and_diary(core)
    before = originals(core.store, other)
    with core.store.transaction(control_write=True):
        core.ledger.delete_memory(core.store, wrong, core.clock())
    lines = core.ledger.read()
    lines[-1]['event_ids'] = ['evt_test_1']
    payload = ('\n'.join(encode(line) for line in lines) + '\n').encode()
    with pytest.raises(CoreError, match='ERASURE_LEDGER_INVALID'):
        core.ledger.read(raw=payload)
    assert originals(core.store, other) == before


def facts_and_diary(core):
    seed_day(core, texts=['这是主人确认的项目目录，今天讨论了我的读书计划。',
                          '这周想先读科幻小说。', '我喜欢先列出章节再开始阅读。'])
    core.diary.schedule_day(DAY)
    assert run(core.diary.generate_once('memory-delete-diary'))
    source = core.store.one("SELECT * FROM events WHERE event_id='evt_test_1'")
    ids = []
    with core.store.transaction(control_write=True):
        for number, content in enumerate(('主人每周读十本书', '主人喜欢科幻小说'), 1):
            ids.append(core.memory.add(Candidate(
                subject='project', kind='fact', fact_key=f'console_{number}', content=content,
                evidence_event_id=source['event_id'], evidence_quote='项目目录',
                event_time=EventTime(start='2026-01-01T00:00:00+08:00',
                    end='2026-01-02T00:00:00+08:00', precision='day',
                    assertion='planned', quote='今天'),
            ), core.clock(), confirmed=True))
    return ids


def originals(store, other):
    return {
        'events': store.rows('SELECT * FROM events ORDER BY event_id'),
        'diaries': store.rows('SELECT * FROM diary_entries ORDER BY entry_id'),
        'diary_evidence': store.rows('SELECT * FROM diary_evidence ORDER BY entry_id,event_id'),
        'other': store.one('SELECT * FROM memories WHERE memory_id=%s', (other,)),
    }


def test_console_delete_keeps_sources_diary_and_shared_fact(client, core, monkeypatch):
    monkeypatch.setattr(SemanticIndex, 'warmup', WARMUP)
    wrong, other = facts_and_diary(core)
    before = originals(core.store, other)
    headers = login(client)
    path = f'/v1/console/memories/{wrong}/decision'
    assert client.post(path, json={'decision': 'delete'}, headers={
        **headers, 'Origin': 'https://unrelated.test'}).status_code == 403
    assert client.post(path, json={'decision': 'delete'}, headers={
        'Origin': headers['Origin'], 'Idempotency-Key': 'no-csrf'}).status_code == 403
    first = client.post(path, json={'decision': 'delete'}, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json() == {'memory_id': wrong, 'state': 'FORGOTTEN'}
    assert client.post(path, json={'decision': 'delete'}, headers=headers).json() == first.json()
    assert originals(core.store, other) == before
    assert core.store.one('SELECT content,status,event_time_json FROM memories WHERE memory_id=%s',
                          (wrong,)) == {
        'content': '', 'status': 'FORGOTTEN', 'event_time_json': None}
    assert not core.store.rows('SELECT * FROM memory_evidence WHERE memory_id=%s', (wrong,))
    listed = client.get('/v1/console/memories?view=all').json()['items']
    assert {item['id'] for item in listed} == {other}
    recalled = client.get('/v1/console/memories/recall', params={'q': '读书'}).json()
    assert wrong not in {item['id'] for item in recalled['items']}
    records = core.ledger.read()[1:]
    assert len(records) == 1 and records[0]['target_type'] == 'memory_only'
    assert records[0]['event_ids'] == [] and records[0]['memory_ids'] == [wrong]
    completed = epoch(core.store)
    core.ledger.replay(core.store)
    assert epoch(core.store) == completed
    assert originals(core.store, other) == before


def test_fsynced_memory_only_delete_replays_after_database_rollback(core, monkeypatch):
    wrong, other = facts_and_diary(core)
    before = originals(core.store, other)
    erase = ErasureLedger.erase_memories

    def fail(*args):
        raise RuntimeError('synthetic interruption after durable append')

    monkeypatch.setattr(ErasureLedger, 'erase_memories', staticmethod(fail))
    with (
        pytest.raises(RuntimeError, match='durable append'),
        core.store.transaction(control_write=True),
    ):
        core.ledger.delete_memory(core.store, wrong, core.clock())
    assert core.store.one('SELECT status FROM memories WHERE memory_id=%s', (wrong,))[
        'status'] == 'CONFIRMED'
    monkeypatch.setattr(ErasureLedger, 'erase_memories', staticmethod(erase))
    core.ledger.replay(core.store)
    assert core.store.one('SELECT status FROM memories WHERE memory_id=%s', (wrong,))[
        'status'] == 'FORGOTTEN'
    assert originals(core.store, other) == before


@pytest.mark.parametrize('projection', ['old_backup', 'complete_fact', 'complete_time'])
def test_memory_only_delete_replays_into_old_and_complete_backups(
    client, core, empty_database, tmp_path, projection,
):
    wrong, other = facts_and_diary(core)
    before = originals(core.store, other)
    memory = core.store.one('SELECT * FROM memories WHERE memory_id=%s', (wrong,))
    evidence = core.store.rows('SELECT * FROM memory_evidence WHERE memory_id=%s', (wrong,))
    headers = login(client)
    archive, key = tmp_path / 'before-delete.enc', Fernet.generate_key()
    if projection == 'old_backup':
        backup(core.settings.database, archive, key)
    result = client.post(f'/v1/console/memories/{wrong}/decision',
                         json={'decision': 'delete'}, headers=headers)
    assert result.status_code == 200, result.text
    if projection == 'complete_fact':
        # An old COMPLETE snapshot can still contain a stale local projection.
        core.store.execute('UPDATE memories SET content=%s,status=%s WHERE memory_id=%s',
                           (memory['content'], memory['status'], wrong))
        for row in evidence:
            core.store.insert('memory_evidence', **row)
    if projection != 'old_backup':
        core.store.execute('UPDATE memories SET event_time_json=%s WHERE memory_id=%s',
                           (memory['event_time_json'], wrong))
        backup(core.settings.database, archive, key)
    state = tmp_path / 'restored-state'
    restore(archive, empty_database, key, core.settings.erasure_ledger, state_directory=state)
    with Database(empty_database) as database, closing(Store(database, state)) as store:
        assert store.one('SELECT content,status,event_time_json FROM memories WHERE memory_id=%s',
                         (wrong,)) == {
            'content': '', 'status': 'FORGOTTEN', 'event_time_json': None}
        assert originals(store, other) == before
        current = epoch(store)
        core.ledger.replay(store)
        assert epoch(store) == current
        assert originals(store, other) == before
