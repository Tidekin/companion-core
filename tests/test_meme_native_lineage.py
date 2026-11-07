"""Original caption takeover maintains every staged source tuple atomically."""

import base64
from contextlib import asynccontextmanager

import pytest

from companion import budget, meme_asset_migration, meme_assets
from companion.contracts import CoreError
from companion.store import Store, encode
from tests.conftest import run
from tests.support.meme_core import body, caption_environment, worker


@asynccontextmanager
async def lineage_case(core, tmp_path, monkeypatch):
    async with caption_environment(core, tmp_path, monkeypatch) as values:
        event, _, client, _, _, now, attempt = values
        ids, payloads = [], []
        for name in ("A", "B", "C", "D"):
            payload = body(core, event, attempt, now)
            payload["source_evidence"]["usage_basis"] += "; original source " + name
            from companion_meme_protocol import digest

            payload["asset"]["source_evidence_sha256"] = digest(payload["source_evidence"])
            response = await client.post("/internal/v1/memes/captions", json=payload)
            assert response.status_code == 200, response.text
            ids.append(response.json()["job_id"])
            payloads.append(payload)
        custody = meme_assets.original_custody(core.store, core.settings, event["event_id"])
        assets = []
        with core.store.transaction():
            core.store.execute('UPDATE meme_captions SET reuse_job_id=%s WHERE job_id=%s',
                               (ids[2], ids[3]))
            for payload in payloads[1:]:
                proof = payload["source_evidence"]
                meta = {key: proof[key] for key in (
                    "source_page", "asset_url", "license", "usage_basis", "fetched_at", "revision"
                )}
                meta.update(category="like", description="Original untrusted hint", tags=[],
                            suitable=[], avoid=[], acquisition_json=encode(proof["acquisition"]))
                item = {"asset_sha256": payload["asset"]["asset_sha256"],
                        "source_sha256": payload["asset"]["source_evidence_sha256"],
                        "active": True, "metadata": meta, "mime": "image/png"}
                data = base64.b64decode(payload["asset"]["data_base64"])
                staged = meme_asset_migration._apply(
                    core.store, core.settings, {"custody": custody}, [(item, data)]
                )[0]
                assert staged["state"] == "STAGED"
                assets.append((staged["asset_id"], item, data))
        yield event, ids, assets, custody


async def reject_original(core, monkeypatch):
    def reject(*args, **kwargs):
        raise CoreError("MODEL_BUDGET_EXCEEDED", 429)

    with monkeypatch.context() as patch:
        patch.setattr(budget, "reserve", reject)
        assert await worker(core, [])
    assert not core.store.rows("SELECT * FROM model_usage")


def test_takeover_extends_same_event_and_transitive_sources_without_rewriting_originals(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with lineage_case(core, tmp_path, monkeypatch) as (event, ids, assets, custody):
            original_assets = core.store.rows("SELECT * FROM meme_assets ORDER BY asset_id")
            original_capture = core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                                              ("owner_input:" + event["event_id"],))
            await reject_original(core, monkeypatch)
            calls = []
            assert await worker(core, calls)
            assert core.store.one('SELECT reuse_job_id FROM meme_captions WHERE job_id=%s',
                                  (ids[1],))["reuse_job_id"] is None
            assert core.store.one('SELECT reuse_job_id FROM meme_captions WHERE job_id=%s',
                                  (ids[2],))["reuse_job_id"] == ids[1]
            for asset_id, item, data in assets:
                sources, withdrawn = meme_assets.provenance(core.store, core.settings, custody,
                    item, data, caption_job_id=ids[assets.index((asset_id, item, data)) + 1])
                recorded = {(r["event_id"], r["caption_job_id"]) for r in core.store.rows(
                    'SELECT * FROM meme_asset_sources WHERE asset_id=%s', (asset_id,))}
                assert sources and not withdrawn and set(sources) <= recorded
                assert (event["event_id"], ids[0]) in recorded  # Old root remains attributable.
                assert (event["event_id"], ids[1]) in recorded  # Same event, new caption tuple.
            assert await worker(core, calls) and await worker(core, calls)
            assert len(calls) == len(core.store.rows("SELECT * FROM model_usage")) == 1
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
            assert core.store.rows("SELECT * FROM meme_assets ORDER BY asset_id") == original_assets
            assert core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                                 ("owner_input:" + event["event_id"],)) == original_capture

    run(scenario())


def test_source_link_failure_rolls_back_graph_and_links_before_any_paid_work(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with lineage_case(core, tmp_path, monkeypatch) as (_, ids, assets, _):
            await reject_original(core, monkeypatch)
            before = core.store.rows("SELECT * FROM meme_asset_sources ORDER BY asset_id,"
                                     "event_id,caption_job_id")
            execute = Store.execute

            def fail_new_tuple(store, sql, params=()):
                if (sql.startswith("INSERT OR IGNORE INTO meme_asset_sources")
                    and params[0] == assets[1][0] and params[2] == ids[1]):
                    raise ValueError("Synthetic attribution write failure")
                return execute(store, sql, params)

            calls = []
            with monkeypatch.context() as patch:
                patch.setattr(Store, "execute", fail_new_tuple)
                assert await worker(core, calls)
            assert not calls and not core.store.rows("SELECT * FROM model_usage")
            assert core.store.one('SELECT reuse_job_id FROM meme_captions WHERE job_id=%s',
                                  (ids[1],))["reuse_job_id"] == ids[0]
            assert core.store.one('SELECT reuse_job_id FROM meme_captions WHERE job_id=%s',
                                  (ids[2],))["reuse_job_id"] == ids[0]
            assert core.store.rows("SELECT * FROM meme_asset_sources ORDER BY asset_id,"
                                   "event_id,caption_job_id") == before
            assert core.store.one('SELECT state,last_error_code FROM jobs WHERE job_id=%s',
                                  (ids[1],)) == {"state": "CANCELLED",
                                                "last_error_code": "MEME_ASSET_SOURCE_CHANGED"}

    run(scenario())


def test_actual_paid_job_prevents_takeover_when_a_cached_usage_pointer_is_missing(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with lineage_case(core, tmp_path, monkeypatch) as (_, ids, _, _):
            async def fail_response():
                raise ValueError("Synthetic response lost")

            calls = []
            assert await worker(core, calls, hook=fail_response)
            before = core.store.rows("SELECT * FROM model_usage")
            assert len(calls) == len(before) == 1 and before[0]["state"] == "UNKNOWN"
            core.store.execute('UPDATE meme_captions SET usage_request_id=NULL WHERE job_id=%s',
                               (ids[0],))
            assert await worker(core, calls)
            assert len(calls) == 1 and core.store.rows("SELECT * FROM model_usage") == before
            assert core.store.one('SELECT state FROM jobs WHERE job_id=%s',
                                  (ids[1],))["state"] == "CANCELLED"

    run(scenario())


async def blocked_paid_ancestor(core, tmp_path, monkeypatch, *, tokens):
    async with lineage_case(core, tmp_path, monkeypatch) as (_, ids, _, _):
        original_calls = []
        # Measured invalid JSON is SETTLED; a lost provider response is UNKNOWN.
        if tokens:
            assert await worker(core, original_calls, content={"invalid_caption": True})
        else:
            async def lost_response():
                raise ValueError("Synthetic response lost after dispatch")
            assert await worker(core, original_calls, hook=lost_response)
        assert len(original_calls) == 1
        original_fee = core.store.rows("SELECT * FROM model_usage")
        assert len(original_fee) == 1
        assert original_fee[0]["state"] == ("SETTLED" if tokens else "UNKNOWN")
        assert core.store.one('SELECT result_json FROM meme_captions WHERE job_id=%s',
                              (ids[0],))["result_json"] is None
        assert await worker(core, [])  # B inherits A's failed result and original fee.
        with core.store.transaction():
            core.store.execute('UPDATE meme_captions SET usage_request_id=NULL WHERE job_id=%s',
                               (ids[1],))
            core.store.execute('UPDATE meme_captions SET reuse_job_id=%s WHERE job_id=%s',
                               (ids[1], ids[2]))
            meme_assets.extend_reuse_provenance(core.store, core.settings, ids[2:])
        graph = core.store.rows("SELECT job_id,reuse_job_id,usage_request_id "
                               "FROM meme_captions ORDER BY job_id")
        links = core.store.rows("SELECT * FROM meme_asset_sources ORDER BY asset_id,"
                               "event_id,caption_job_id")
        reserve = budget.reserve
        admissions, later_calls = [], []

        def measured_reserve(*args, **kwargs):
            admissions.append(args[2])
            return reserve(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(budget, "reserve", measured_reserve)
            assert await worker(core, later_calls)
        assert (len(admissions), len(later_calls)) == (0, 0)
        assert core.store.rows("SELECT * FROM model_usage") == original_fee
        assert core.store.rows("SELECT job_id,reuse_job_id,usage_request_id "
                               "FROM meme_captions ORDER BY job_id") == graph
        assert core.store.rows("SELECT * FROM meme_asset_sources ORDER BY asset_id,"
                               "event_id,caption_job_id") == links
        assert core.store.one('SELECT state,last_error_code FROM jobs WHERE job_id=%s',
                              (ids[2],)) == {"state": "CANCELLED",
                                            "last_error_code": "MEME_CAPTION_SOURCE_CHANGED"}



def test_settled_ancestor_without_caption_result_cannot_be_cut_through_a_missing_cached_pointer(
    core, tmp_path, monkeypatch
):
    run(blocked_paid_ancestor(core, tmp_path, monkeypatch, tokens=True))


def test_unknown_deeper_ancestor_cannot_be_cut_or_rebilled(core, tmp_path, monkeypatch):
    run(blocked_paid_ancestor(core, tmp_path, monkeypatch, tokens=False))

def test_missing_old_parent_is_a_closed_failure_before_model_admission(core, tmp_path, monkeypatch):
    async def scenario():
        async with lineage_case(core, tmp_path, monkeypatch) as (_, ids, _, _):
            await reject_original(core, monkeypatch)
            core.store.db.execute("PRAGMA foreign_keys=OFF")
            try:
                core.store.execute('UPDATE meme_captions SET reuse_job_id=%s WHERE job_id=%s',
                                   ("job_" + "f" * 32, ids[1]))
            finally:
                core.store.db.execute("PRAGMA foreign_keys=ON")
            calls = []
            assert await worker(core, calls)
            assert not calls and not core.store.rows("SELECT * FROM model_usage")
            assert core.store.one('SELECT state,last_error_code FROM jobs WHERE job_id=%s',
                                  (ids[1],)) == {"state": "CANCELLED",
                                                "last_error_code": "MEME_CAPTION_SOURCE_CHANGED"}

    run(scenario())


@pytest.mark.parametrize("damage", ["cycle", "deep_cycle", "missing_deep", "bound",
                                    "wrong_image", "cleared_source"])
def test_damaged_old_ancestry_never_reaches_a_model_reservation(
    core, tmp_path, monkeypatch, damage
):
    async def scenario():
        async with lineage_case(core, tmp_path, monkeypatch) as (_, ids, _, _):
            await reject_original(core, monkeypatch)
            core.store.db.execute("PRAGMA foreign_keys=OFF")
            try:
                if damage == "cycle":
                    core.store.execute('UPDATE meme_captions SET reuse_job_id=%s WHERE job_id=%s',
                                       (ids[0], ids[0]))
                elif damage == "deep_cycle":
                    core.store.execute('UPDATE meme_captions SET reuse_job_id=%s WHERE job_id=%s',
                                       (ids[3], ids[0]))
                elif damage == "missing_deep":
                    core.store.execute('UPDATE meme_captions SET reuse_job_id=%s WHERE job_id=%s',
                                       ("job_" + "f" * 32, ids[0]))
                elif damage == "bound":
                    core.store.execute('UPDATE meme_captions SET reuse_job_id=%s WHERE job_id=%s',
                                       (ids[3], ids[0]))
                    core.store.execute('UPDATE meme_captions SET reuse_job_id=NULL WHERE job_id=%s',
                                       (ids[2],))
                    monkeypatch.setattr("companion.meme_core.MAX_ROWS", 2)
                elif damage == "wrong_image":
                    core.store.execute('UPDATE meme_captions SET asset_sha256=%s WHERE job_id=%s',
                                       ("a" * 64, ids[0]))
                else:
                    core.store.execute("UPDATE meme_captions SET source_json='{}' WHERE job_id=%s",
                                       (ids[0],))
            finally:
                core.store.db.execute("PRAGMA foreign_keys=ON")
            graph = core.store.rows("SELECT * FROM meme_captions ORDER BY job_id")
            links = core.store.rows("SELECT * FROM meme_asset_sources ORDER BY asset_id,"
                                   "event_id,caption_job_id")
            reserve = budget.reserve
            admissions, calls = [], []

            def measured_reserve(*args, **kwargs):
                admissions.append(args[2])
                return reserve(*args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(budget, "reserve", measured_reserve)
                assert await worker(core, calls)
            assert (len(admissions), len(calls)) == (0, 0)
            assert core.store.rows("SELECT * FROM meme_captions ORDER BY job_id") == graph
            assert core.store.rows("SELECT * FROM meme_asset_sources ORDER BY asset_id,"
                                   "event_id,caption_job_id") == links
            assert not core.store.rows("SELECT * FROM model_usage")
            assert core.store.one('SELECT state,last_error_code FROM jobs WHERE job_id=%s',
                                  (ids[1],)) == {"state": "CANCELLED",
                                                "last_error_code": "MEME_CAPTION_SOURCE_CHANGED"}

    run(scenario())
