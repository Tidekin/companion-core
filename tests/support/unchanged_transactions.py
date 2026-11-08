"""Observe the native sessions used by synthetic HTTP requests."""

from contextlib import contextmanager
from unittest.mock import patch

from companion.store import Store
from tests.postgres_fixture import logical_state


@contextmanager
def unchanged_transactions(core):
    before = logical_state(core.store)
    transaction = Store.transaction
    writes = []

    @contextmanager
    def observed(store, **options):
        with transaction(store, **options):
            initial = store.change_token()["writes"]
            try:
                yield store
            finally:
                # Inspect this backend before the handler error rolls its transaction back.
                writes.append((initial, store.change_token()["writes"]))

    with patch.object(Store, "transaction", observed):
        try:
            yield
        finally:
            assert writes, "expected a native write transaction"
            assert all(first == last for first, last in writes), "request changed business rows"
            assert logical_state(core.store) == before


@contextmanager
def readonly_service_call(store, service_type, method):
    original = getattr(service_type, method)
    calls = []

    def observed(service, *args, **kwargs):
        assert service.store is store
        assert service.store.one("SHOW transaction_read_only") == {"transaction_read_only": "on"}
        calls.append(service.store.backend_pid)
        return original(service, *args, **kwargs)

    with store.read_snapshot(), patch.object(service_type, method, observed):
        yield
    assert calls == [store.backend_pid]
