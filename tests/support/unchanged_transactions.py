"""Observe the original write transactions opened by a synthetic HTTP request."""

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
