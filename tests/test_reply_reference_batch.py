"""Reply references: one combined source check, and one reused capability carry view."""

from types import SimpleNamespace

from companion import capabilities, model_carry, owner_authority, paid_budget, watch_ledger
from companion.contracts import CoreError


def test_watch_references_check_all_items_once_then_fall_back(monkeypatch):
    items = [{"source_event_id": f"evt_{n}"} for n in range(5)]
    monkeypatch.setattr(watch_ledger, "recent", lambda store, limit: items)
    seen = []
    monkeypatch.setattr(watch_ledger, "context_items",
                        lambda store, watched, *args: seen.append(list(watched)) or [])
    calls = []

    def available_all(ids, purpose):
        calls.append(tuple(ids))
        return True

    watch_ledger.add_reply_references(None, None, None, {"conversation_id": "c",
        "created_at": "t"}, None, None, available_all)
    assert calls == [tuple(item["source_event_id"] for item in items)]
    assert seen[-1] == items

    calls.clear()

    def one_missing(ids, purpose):
        calls.append(tuple(ids))
        return "evt_2" not in ids

    watch_ledger.add_reply_references(None, None, None, {"conversation_id": "c",
        "created_at": "t"}, None, None, one_missing)
    # The combined check fails, so each item is checked alone and only evt_2 is dropped.
    assert len(calls) == 1 + len(items)
    assert [item["source_event_id"] for item in seen[-1]] == ["evt_0", "evt_1", "evt_3", "evt_4"]


def test_capability_carry_view_is_reused_until_its_inputs_change(monkeypatch):
    keys, calls, failing = ["k1"], [], []

    def journal(*_args, **_kwargs):
        calls.append(1)
        if failing:
            raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
        return {"model_closures": {"r": "x"}}

    monkeypatch.setattr(owner_authority, "required", lambda *_: True)
    monkeypatch.setattr(model_carry, "active", lambda _settings: True)
    monkeypatch.setattr(model_carry, "supported", lambda *_: None)
    monkeypatch.setattr(paid_budget, "inputs_key", lambda *_: keys[0])
    monkeypatch.setattr(paid_budget, "journal", journal)
    monkeypatch.setattr(capabilities, "_CARRY_VIEW", {})
    settings = SimpleNamespace(paid_model=None)

    first = capabilities._carry_view(None, settings)
    first["model_closures"]["mutated"] = True  # A caller's copy never reaches the cache.
    assert capabilities._carry_view(None, settings) == {"model_closures": {"r": "x"}}
    assert len(calls) == 1
    keys[0] = "k2"
    capabilities._carry_view(None, settings)
    assert len(calls) == 2
    failing.append(1)
    keys[0] = "k3"
    assert capabilities._carry_view(None, settings) == {}
    assert capabilities._carry_view(None, settings) == {}
    assert len(calls) == 4
