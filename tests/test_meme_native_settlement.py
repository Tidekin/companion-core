"""Known acquisition, uncertain commits and cancellation keep original responsibility."""

import asyncio
from contextlib import contextmanager

import httpx
import pytest

from companion import meme_assets, meme_service, owner_authority
from companion.owner_authority_protocol import AuthorityStop
from companion.store import Store
from tests.conftest import run
from tests.support.meme_native_discovery import native_case, release_foreground


@pytest.mark.parametrize("phase", ["stage", "before_commit", "after_commit", "after_marker"])
def test_transaction_outcomes_never_lose_downloads_or_overwrite_a_committed_result(
    core, tmp_path, monkeypatch, phase
):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch) as c:
            await c.retrieval.prepare(c.job)
            release_foreground(c)
            fired = []
            original = Store.transaction

            @contextmanager
            def transaction(store):
                selected = False
                with original(store):
                    yield store
                    if store is core.store and not fired:
                        row = store.one("SELECT state FROM meme_discovery_reservations")
                        selected = bool(row and row["state"] == "SETTLED"
                                        and store.one("SELECT 1 FROM meme_captions"))
                        if selected:
                            fired.append(phase)
                            if phase == "before_commit":
                                raise RuntimeError("Synthetic fault before real commit")
                if selected and phase == "after_commit":
                    raise RuntimeError("Synthetic fault after real commit")

            def stage_failure(*args, **kwargs):
                fired.append(phase)
                raise ValueError("Synthetic stage failure after caption insert")

            with monkeypatch.context() as patch:
                if phase == "stage":
                    patch.setattr(meme_assets, "store_original", stage_failure)
                elif phase != "after_marker":
                    patch.setattr(Store, "transaction", transaction)
                task = asyncio.create_task(c.service.discovery.run_once("phase-" + phase))
                assert await task
                if phase == "after_marker":
                    assert not task.cancel()  # No suspension between committed marker and return.
                else:
                    assert fired
            row = core.store.one("SELECT * FROM meme_discovery_reservations")
            committed = phase in {"after_commit", "after_marker"}
            assert row["state"] == "SETTLED"
            assert row["queries"] == 1 and row["requests"] == 5
            assert row["download_bytes"] == sum(map(len, c.upstream.bodies))
            assert row["storage_bytes"] == (len(next(iter(c.upstream.images.values())))
                                             if committed else 0)
            assert len(core.store.rows("SELECT * FROM meme_captions")) == int(committed)
            assert len(core.store.rows("SELECT * FROM meme_assets")) == int(committed)
            assert not core.store.rows("SELECT * FROM model_usage")
            before = list(c.upstream.calls)
            assert not await c.service.discovery.run_once("no-replay")
            assert c.upstream.calls == before

    run(scenario())


def test_stop_after_complete_download_settles_accounting_without_native_content(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch) as c:
            await c.retrieval.prepare(c.job)
            release_foreground(c)
            commit = c.service.discovery._commit

            def stop_then_commit(*args):
                owner_authority.stop(core.store, core.settings, core.clock,
                    AuthorityStop(operation_id="native-complete-stop", authority=c.grant.authority))
                return commit(*args)

            monkeypatch.setattr(c.service.discovery, "_commit", stop_then_commit)
            assert await c.service.discovery.run_once("known-stop")
            row = core.store.one("SELECT * FROM meme_discovery_reservations")
            assert row["state"] == "SETTLED" and row["storage_bytes"] == 0
            assert row["download_bytes"] == sum(map(len, c.upstream.bodies))
            assert not core.store.rows("SELECT * FROM meme_captions")
            assert not core.store.rows("SELECT * FROM meme_assets")
            assert not core.store.rows("SELECT * FROM model_usage")

    run(scenario())


def test_partial_body_cancellation_keeps_unknown_and_never_redispatches(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch) as c:
            await c.retrieval.prepare(c.job)
            release_foreground(c)
            data = next(iter(c.upstream.images.values()))

            class Interrupted(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield data[:len(data) // 2]
                    raise asyncio.CancelledError()

            def public(request):
                response = c.upstream.handle(request)
                if request.url.path.endswith("/0.png"):
                    return httpx.Response(200, stream=Interrupted(),
                                          headers={"content-type": "image/png"})
                return response

            c.service.discovery.transport = httpx.MockTransport(public)
            with pytest.raises(asyncio.CancelledError):
                await c.service.discovery.run_once("cancelled-body")
            row = core.store.one("SELECT * FROM meme_discovery_reservations")
            assert row["state"] == "UNKNOWN" and row["storage_bytes"] == 0
            assert row["download_bytes"] == sum(map(len, c.upstream.bodies[:-1])) + len(data) // 2
            assert row["reserved_download_bytes"] >= row["download_bytes"]
            assert not core.store.rows("SELECT * FROM meme_captions")
            assert not core.store.rows("SELECT * FROM meme_assets")
            assert not core.store.rows("SELECT * FROM model_usage")
            before = list(c.upstream.calls)
            assert not await c.service.discovery.run_once("no-cancel-replay")
            assert c.upstream.calls == before

    run(scenario())


def test_failed_accounting_write_retains_original_reservation_for_unknown_recovery(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch) as c:
            await c.retrieval.prepare(c.job)
            release_foreground(c)
            c.upstream.license = b"Unapproved source license"

            def unavailable(*args, **kwargs):
                raise OSError("Synthetic authoritative accounting write unavailable")

            with monkeypatch.context() as patch:
                patch.setattr(meme_service, "result", unavailable)
                with pytest.raises(OSError):
                    await c.service.discovery.run_once("settlement-unavailable")
            row = core.store.one("SELECT * FROM meme_discovery_reservations")
            assert row["state"] == "RESERVED"
            assert row["reserved_download_bytes"] >= sum(map(len, c.upstream.bodies))
            before = list(c.upstream.calls)
            core.clock.advance(core.settings.lease_seconds + 1)
            assert not await c.service.discovery.run_once("recover-original-lease")
            assert core.store.one("SELECT state FROM meme_discovery_reservations")[
                "state"] == "UNKNOWN"
            assert c.upstream.calls == before
            assert not core.store.rows("SELECT * FROM meme_captions")
            assert not core.store.rows("SELECT * FROM meme_assets")

    run(scenario())
