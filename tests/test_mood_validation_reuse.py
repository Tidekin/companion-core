"""Authority chains fold once per journal; mood lineage reuse stays inside one batch."""

import marshal

import pytest

from companion import authority_custody, mood, owner_authority, paid_budget


def _records(monkeypatch, records):
    monkeypatch.setattr(paid_budget, "_journal_records", lambda raw: ([dict(r) for r in records],
                                                                       True))
    authority_custody._material.cache_clear()


def test_material_folds_chain_once_per_journal(monkeypatch):
    folded = []

    def fold(records, row, digest):
        folded.append(digest)
        records.append({**row, "record_sha256": digest})

    monkeypatch.setattr(owner_authority, "fold", fold)
    _records(monkeypatch, [
        {"kind": "header", "journal_id": "j1", "sha256": "h"},
        {"kind": owner_authority.RECORD_KIND, "revision": 1, "sha256": "a"},
    ])
    first = authority_custody._read_material(b"journal-1")
    again = authority_custody._read_material(b"journal-1")
    assert folded == ["a"]
    authority, lifecycle, managed = marshal.loads(first.chain)
    assert authority == [{"kind": owner_authority.RECORD_KIND, "revision": 1,
                          "record_sha256": "a"}]
    assert (lifecycle, managed) == (None, False)
    # Each check gets its own copy of the folded records.
    marshal.loads(again.chain)[0].clear()
    assert marshal.loads(again.chain)[0] == authority


def test_invalid_chain_is_kept_out_of_admission_material(monkeypatch):
    def fold(records, row, digest):
        raise ValueError("authority revision chain")

    monkeypatch.setattr(owner_authority, "fold", fold)
    _records(monkeypatch, [
        {"kind": "header", "journal_id": "j1", "sha256": "h"},
        {"kind": owner_authority.RECORD_KIND, "revision": 1, "sha256": "a"},
    ])
    material = authority_custody._read_material(b"journal-2")
    assert material.chain is None
    assert material.journal_id == "j1"


def test_mood_lineage_memo_returns_fresh_lists(monkeypatch):
    service = mood.MoodService.__new__(mood.MoodService)
    reads = []

    def read(key, sources, source_check):
        reads.append(key)
        return None if key == "gone" else [{"event_id": key}]

    monkeypatch.setattr(service, "_read_lineage", read)
    memo = {}
    first = service._lineage("e1", None, None, memo)
    first += [{"event_id": "extra"}]
    assert service._lineage("e1", None, None, memo) == [{"event_id": "e1"}]
    assert service._lineage("gone", None, None, memo) is None
    assert service._lineage("gone", None, None, memo) is None
    assert reads == ["e1", "gone"]
    # Without a batch memo every call reads the native lineage again.
    service._lineage("e1", None, None)
    assert reads == ["e1", "gone", "e1"]


@pytest.fixture(autouse=True)
def _clear_material():
    yield
    authority_custody._material.cache_clear()
