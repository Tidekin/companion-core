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
    authority_custody._projections.clear()


def test_projection_is_validated_once_per_identical_content(core, tmp_path, monkeypatch):
    from companion.contracts import CoreError
    from tests.support.owner_input import captured

    captured(core, tmp_path)
    authority_custody._projections.clear()
    original, calls = owner_authority._validate_projection, []

    def validate(*args):
        calls.append(None)
        return original(*args)

    monkeypatch.setattr(owner_authority, "_validate_projection", validate)
    expected = owner_authority.current(core.store, core.settings, core.clock)
    for _ in range(3):
        assert owner_authority.current(core.store, core.settings, core.clock) == expected
    assert owner_authority.evidence(core.store, core.settings,
                                    expected.authority.as_dict()).authority == expected.authority
    assert len(calls) == 1

    # Changed DB content changes the digest and is fully validated again.
    core.store.execute("UPDATE owner_authority_revisions SET request_sha256=%s "
                       "WHERE revision=1", ("0" * 64,))
    with pytest.raises(CoreError):
        owner_authority.current(core.store, core.settings, core.clock)
    assert len(calls) == 2


def _scoped_service(monkeypatch, result):
    service = mood.MoodService.__new__(mood.MoodService)
    seen = []

    def snapshot(*, at=None, allowed=None, only=None):
        seen.append(only)
        return result

    monkeypatch.setattr(service, "snapshot", snapshot)
    return service, seen


def test_revalidate_reads_only_the_proof_events(monkeypatch):
    proof = {"evaluated_at": "2026-11-11T00:00:00+00:00", "allowed": None, "truncated": False,
             "signals": [{"event_id": "evt_b"}, {"event_id": "evt_a"}, {"event_id": "evt_b"}]}
    service, seen = _scoped_service(monkeypatch, proof)
    service.revalidate(proof)
    assert seen == [["evt_a", "evt_b"]]


def test_revalidate_rebuilds_a_truncated_proof_in_full(monkeypatch):
    proof = {"evaluated_at": "2026-11-11T00:00:00+00:00", "allowed": None, "truncated": True,
             "signals": [{"event_id": "evt_a"}]}
    service, seen = _scoped_service(monkeypatch, proof)
    service.revalidate(proof)
    assert seen == [None]


def test_revalidate_rejects_any_difference(monkeypatch):
    from companion.contracts import CoreError

    proof = {"evaluated_at": "2026-11-11T00:00:00+00:00", "allowed": None, "truncated": False,
             "signals": []}
    service, _ = _scoped_service(monkeypatch, {**proof, "paused": True})
    with pytest.raises(CoreError) as error:
        service.revalidate(proof)
    assert error.value.code == "PRIVACY_CHANGED"
