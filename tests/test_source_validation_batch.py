"""Native transaction changes invalidate only the synchronous source read batch."""

from contextlib import nullcontext

import psycopg
import pytest

from companion import owner_authority, owner_input
from companion.contracts import CoreError


def test_returned_read_guard_expires_before_the_next_boundary(core):
    saved = []

    def read(unchanged):
        if unchanged is not None:
            saved.append(unchanged)
        return "read"

    with core.store.read_snapshot():
        def stable(unchanged):
            assert unchanged()
            saved.append(unchanged)
        owner_input.batch_sources(core.store, stable)
        assert not saved[-1]()

    with core.store.transaction():
        assert owner_input.batch_sources(core.store, read) == "read"
        assert not saved[0]()
        core.store.execute("UPDATE runtime_meta SET value=value WHERE key=%s",
                           (owner_authority.MODE,))
        assert owner_input.batch_sources(core.store, read) == "read"
        assert not any(check() for check in saved)


def test_actual_write_during_a_read_cannot_publish_the_collected_result(core):
    def read(unchanged):
        assert unchanged is not None
        core.store.execute("UPDATE runtime_meta SET value=value WHERE key=%s",
                           (owner_authority.MODE,))
        assert not unchanged()
        return "must not publish"

    with pytest.raises(CoreError, match="PRIVACY_CHANGED"), core.store.transaction():
        owner_input.batch_sources(core.store, read)


def test_unrelated_commit_retries_native_reads_without_reusing_the_first_view(core):
    calls, saved = [], []
    with psycopg.connect(core.settings.database.get_secret_value(), autocommit=True) as other:
        def read(unchanged):
            calls.append(unchanged is not None)
            if unchanged is not None:
                saved.append(unchanged)
                other.execute("UPDATE runtime_meta SET value=value WHERE key=%s",
                              (owner_authority.MODE,))
                assert not unchanged()
                return "stale view"
            assert not saved[0]()
            return "fresh native reads"

        with core.store.transaction():
            assert owner_input.batch_sources(core.store, read) == "fresh native reads"
    assert calls == [True, False]


@pytest.mark.parametrize("transactional", [False, True])
def test_async_source_operation_cannot_extend_a_native_read_batch(core, transactional):
    async def read(unchanged):
        pytest.fail("an asynchronous source operation ran")

    with core.store.transaction() if transactional else nullcontext():
        with pytest.raises(TypeError, match="SYNCHRONOUS_SOURCE_READ_REQUIRED"):
            owner_input.batch_sources(core.store, read)
