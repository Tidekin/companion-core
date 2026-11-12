"""Life planning reuses one validated budget journal outcome until its inputs change."""

from types import SimpleNamespace

from companion import life_topics, model_carry, paid_budget
from companion.contracts import CoreError


class UnknownRows:
    """Report one retained UNKNOWN row; every other query reaches the real store."""

    def __init__(self, store):
        self._store = store

    def rows(self, statement, parameters=()):
        if "state = 'UNKNOWN'" in statement:
            return [{"request_id": "model_" + "0" * 32}]
        return self._store.rows(statement, parameters)

    def __getattr__(self, name):
        return getattr(self._store, name)


def test_unchanged_journal_and_rows_reuse_the_last_valid_check(core, monkeypatch, tmp_path):
    calls, failing = [], []

    def journal(*_args, **_kwargs):
        calls.append(1)
        if failing:
            raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)

    monkeypatch.setattr(model_carry, "active", lambda _settings: True)
    monkeypatch.setattr(model_carry, "supported", lambda *_args: None)
    monkeypatch.setattr(paid_budget, "journal", journal)
    monkeypatch.setattr(life_topics, "_JOURNAL_CHECKED", {})
    path = tmp_path / "budget.jsonl"
    path.write_bytes(b"{}\n")
    monkeypatch.setattr(core.settings, "paid_model", SimpleNamespace(journal_path=path))
    store = UnknownRows(core.store)

    assert not life_topics._uncertain(store, core.settings)
    assert not life_topics._uncertain(store, core.settings)
    assert len(calls) == 1
    # A journal append and an accounting row change each force the full check again.
    with path.open("ab") as stream:
        stream.write(b"\n")
    assert not life_topics._uncertain(store, core.settings)
    assert len(calls) == 2
    core.store.execute("INSERT INTO runtime_meta (key, value) VALUES ('model_budget_blocked', '1')")
    assert not life_topics._uncertain(store, core.settings)
    assert len(calls) == 3
    # A failed check is never cached as valid.
    failing.append(1)
    core.store.execute("DELETE FROM runtime_meta WHERE key = 'model_budget_blocked'")
    assert life_topics._uncertain(store, core.settings)
    assert life_topics._uncertain(store, core.settings)
    assert len(calls) == 5
