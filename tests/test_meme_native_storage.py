"""Native storage counts exact BLOB records and obeys the original erasure replay."""

import json
from contextlib import closing

import pytest
from cryptography.fernet import Fernet

from companion import meme_assets
from companion.backup import backup, restore
from companion.store import Store
from tests.conftest import run
from tests.support.meme_core import worker
from tests.support.meme_native_discovery import native_case, release_foreground
from tests.support.meme_pack_files import Upstream, blob_sha


def test_identical_bytes_distinct_sources_count_both_blobs_and_reuse_one_original_caption_fee(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch) as c:
            upstream = Upstream(count=2)
            data = next(iter(upstream.images.values()))
            upstream.images = dict.fromkeys(upstream.images, data)
            for entry in upstream.entries:
                entry.update(size=len(data), sha=blob_sha(data))
            import httpx

            c.service.discovery.transport = httpx.MockTransport(upstream.handle)
            await c.retrieval.prepare(c.job)
            release_foreground(c)
            assert await c.service.run_once("two-exact-sources")
            assets = core.store.rows("SELECT * FROM meme_assets ORDER BY asset_id")
            captions = core.store.rows('SELECT * FROM meme_captions ORDER BY record_sequence')
            assert len(assets) == len(captions) == 2
            assert assets[0]["asset_id"] != assets[1]["asset_id"]
            assert assets[0]["asset_sha256"] == assets[1]["asset_sha256"]
            row = core.store.one("SELECT * FROM meme_discovery_reservations")
            assert row["state"] == "SETTLED" and row["storage_bytes"] == len(data) * 2
            assert row["download_bytes"] == sum(map(len, upstream.bodies))
            custody = meme_assets.original_custody(core.store, core.settings, c.event["event_id"])
            links = core.store.rows("SELECT * FROM meme_asset_sources ORDER BY asset_id,"
                                   "event_id,caption_job_id")
            for caption in captions:
                matching = next(a for a in assets if a["source_sha256"] == caption["source_sha256"])
                with core.store.transaction():
                    asset_id, added = meme_assets.stage_native(
                        core.store, core.settings, core.clock,
                        caption_job_id=caption["job_id"], metadata_value=json.loads(
                            matching["metadata_json"]), data=data, custody=custody)
                    assert asset_id == matching["asset_id"] and added == 0
            assert core.store.rows("SELECT * FROM meme_assets ORDER BY asset_id") == assets
            assert core.store.rows("SELECT * FROM meme_asset_sources ORDER BY asset_id,"
                                   "event_id,caption_job_id") == links
            calls = []
            assert await worker(core, calls)
            assert await worker(core, calls)
            assert len(calls) == len(core.store.rows("SELECT * FROM model_usage")) == 1
            assert all(r["result_json"] for r in core.store.rows("SELECT * FROM meme_captions"))
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
            assert not core.store.rows("SELECT * FROM outbox")

    run(scenario())


@pytest.mark.parametrize("responsibility", ["SETTLED", "UNKNOWN"])
def test_native_old_backup_replays_newest_erasure_without_rewriting_accounting(
    core, tmp_path, monkeypatch, responsibility
):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch) as c:
            await c.retrieval.prepare(c.job)
            release_foreground(c)
            assert await c.service.run_once("native-before-backup")
            calls = []

            async def lost_response():
                raise ValueError("Synthetic actual dispatch with lost provider response")

            assert await worker(core, calls,
                                hook=lost_response if responsibility == "UNKNOWN" else None)
            assert len(calls) == 1
            tables = ("model_usage", "model_admissions", "meme_discovery_reservations")
            original = {t: core.store.rows("SELECT * FROM " + t) for t in tables}
            assert original["model_usage"][0]["state"] == responsibility
            links = core.store.rows("SELECT * FROM meme_asset_sources")
            archive, restored = tmp_path / "native.enc", tmp_path / "restored.sqlite3"
            key = Fernet.generate_key()
            backup(core.settings.database, archive, key)
            with core.store.transaction():
                core.ledger.expire_events(core.store, [c.event["event_id"]], core.clock())
            ledger = core.ledger.path.read_bytes()

            def erased(store):
                assert store.one("SELECT state,data,metadata_json FROM meme_assets") == {
                    "state": "ERASED", "data": None, "metadata_json": None}
                assert store.one("SELECT asset_data,result_json,context_json,source_json,"
                    "request_json FROM meme_captions") == {"asset_data": None, "result_json": None,
                    "context_json": "{}", "source_json": "{}", "request_json": "{}"}
                assert not store.rows("SELECT * FROM meme_vectors")
                assert store.rows("SELECT * FROM meme_asset_sources") == links
                assert {t: store.rows("SELECT * FROM " + t) for t in tables} == original

            erased(core.store)
            restore(archive, restored, key, core.settings.erasure_ledger)
            with closing(Store(str(restored))) as store:
                erased(store)
                core.ledger.replay(store)
                erased(store)
            assert core.ledger.path.read_bytes() == ledger

    run(scenario())
