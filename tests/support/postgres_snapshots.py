"""Observe real PostgreSQL snapshot churn without replacing a validation fence."""


def observe_snapshots(store, monkeypatch):
    original, seen = store.change_token, set()

    def change_token():
        value = original()
        seen.add(value["snapshot"])
        return value

    monkeypatch.setattr(store, "change_token", change_token)
    return seen
