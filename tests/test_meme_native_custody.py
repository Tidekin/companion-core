"""Fresh signed captures cannot hide a matching original paid or unknown image."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import meme_assets, owner_authority
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from tests.conftest import run
from tests.meme_authority_fixture import admit
from tests.support.meme_core import worker
from tests.support.meme_native_discovery import native_case, release_foreground
from tests.support.owner_authority import activate


@pytest.mark.parametrize("changed", ["key", "epoch"])
def test_capture_rotation_refuses_new_content_without_skipping_the_original_responsibility(
    core, tmp_path, monkeypatch, changed
):
    run(rotation_case(core, tmp_path, monkeypatch, changed=changed,
                      responsibility="SETTLED", erase=False))


@pytest.mark.parametrize("erase", [False, True])
def test_original_unknown_cache_cannot_authorize_another_caption_charge(
    core, tmp_path, monkeypatch, erase
):
    run(rotation_case(core, tmp_path, monkeypatch, changed=None,
                      responsibility="UNKNOWN", erase=erase))


def test_actual_reserved_caption_yields_without_fetch_and_cannot_supply_content(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch) as c:
            await c.retrieval.prepare(c.job)
            release_foreground(c)
            assert await c.service.run_once("before-reserved-model")
            entered, release = asyncio.Event(), asyncio.Event()

            async def held_provider():
                entered.set()
                await release.wait()

            calls = []
            task = asyncio.create_task(worker(core, calls, hook=held_provider))
            try:
                async with asyncio.timeout(5):
                    await entered.wait()
                caption = core.store.one("SELECT * FROM meme_captions")
                asset = core.store.one("SELECT * FROM meme_assets")
                before = core.store.rows("SELECT * FROM model_usage")
                assert before[0]["state"] == "RESERVED" and len(calls) == 1
                custody = meme_assets.original_custody(
                    core.store, core.settings, c.event["event_id"])
                c.service._reuse_compatible(caption["job_id"], custody)
                with pytest.raises(CoreError, match="MEME_CAPTION_UNAVAILABLE"):
                    c.retrieval._resolve(asset["asset_id"], caption["job_id"])
                requests = list(c.upstream.calls)
                assert not await c.service.discovery.run_once("yield-to-original-reservation")
                assert c.upstream.calls == requests
                assert core.store.rows("SELECT * FROM model_usage") == before
            finally:
                release.set()
                assert await task
            assert len(calls) == len(core.store.rows("SELECT * FROM model_usage")) == 1
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"

    run(scenario())


async def rotation_case(core, tmp_path, monkeypatch, *, changed, responsibility, erase):
    async with native_case(core, tmp_path, monkeypatch,
        meme_discovery_daily_download_bytes=32 * 1024 * 1024,
        meme_discovery_daily_storage_bytes=8 * 1024 * 1024) as c:
        await c.retrieval.prepare(c.job)
        release_foreground(c)
        assert await c.service.run_once("original-acquisition")
        calls = []
        if responsibility == "SETTLED":
            assert await worker(core, calls)
        else:
            async def lost_response():
                raise ValueError("Synthetic provider response lost")
            assert await worker(core, calls, hook=lost_response)
        assert len(calls) == 1
        assert core.store.one("SELECT state FROM model_usage")["state"] == responsibility
        if responsibility == "SETTLED":
            owner_authority.stop(core.store, core.settings, core.clock,
                AuthorityStop(operation_id="rotate-source-capture", authority=c.grant.authority))
            key = Ed25519PrivateKey.generate() if changed == "key" else c.grant.key
            epoch = "rotated-native-epoch" if changed == "epoch" else c.config.protocol_epoch
            capture = {**c.grant.policy.capture.model_dump(),
                       "public_key": key.public_key().public_bytes_raw().hex(),
                       "protocol_epoch": epoch}
            policy = type(c.grant.policy).model_validate({**c.grant.policy.model_dump(),
                                                         "capture": capture})
            active, _ = activate(core, policy, revision=2, operation="new-native-capture")
            grant = SimpleNamespace(authority=active["authority"], key=key, policy=policy)
            config = replace(c.config, protocol_epoch=epoch)
        else:
            original_usage = core.store.rows("SELECT * FROM model_usage")
            active, _ = activate(core, c.grant.policy, revision=1,
                                 operation="rotate-with-original-unknown")
            assert core.store.rows("SELECT * FROM model_usage") == original_usage
            grant = SimpleNamespace(authority=active["authority"], key=c.grant.key,
                                    policy=c.grant.policy)
            config = c.config
        event, response = await admit(c.client, config, grant, c.now, 2, text="太棒了，成功了")
        assert response.status_code == 202, response.text
        claimed = core.claim_job("rotated-offer", job_id=response.json()["job_id"])
        assert claimed is not None
        job = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (claimed["job_id"],))
        await c.retrieval.prepare(job)
        core.store.execute("UPDATE jobs SET state='SUCCEEDED',lease_token=NULL WHERE job_id=%s",
                           (job["job_id"],))
        if erase:
            with core.store.transaction(control_write=True):
                core.ledger.expire_events(core.store, [c.event["event_id"]], core.clock())
            assert core.store.one("SELECT asset_data FROM meme_captions")["asset_data"] is None
        original_fees = core.store.rows("SELECT * FROM model_usage")
        original_admissions = core.store.rows("SELECT * FROM model_admissions")
        original_captions = core.store.rows("SELECT * FROM meme_captions")
        original_assets = core.store.rows("SELECT * FROM meme_assets")
        before_calls, before_bodies = len(c.upstream.calls), len(c.upstream.bodies)
        reached = []
        compatible = c.service._reuse_compatible

        def measured_compatible(job_id, custody):
            reached.append(job_id)
            return compatible(job_id, custody)

        monkeypatch.setattr(c.service, "_reuse_compatible", measured_compatible)
        assert await c.service.run_once("refuse-incompatible-custody")
        assert reached == [original_captions[0]["job_id"]]
        parent = core.store.one((
            "SELECT job_id FROM jobs WHERE kind='meme_discovery' AND "
            'event_id=%s'
        ),
                                (event["event_id"],))
        settled = core.store.one('SELECT * FROM meme_discovery_reservations WHERE job_id=%s',
                                 (parent["job_id"],))
        assert settled["state"] == "SETTLED" and settled["storage_bytes"] == 0
        assert settled["requests"] == len(c.upstream.calls) - before_calls == 5
        assert settled["download_bytes"] == sum(map(len, c.upstream.bodies[before_bodies:]))
        assert core.store.rows("SELECT * FROM meme_captions") == original_captions
        assert core.store.rows("SELECT * FROM meme_assets") == original_assets
        assert core.store.rows("SELECT * FROM model_usage") == original_fees
        assert core.store.rows("SELECT * FROM model_admissions") == original_admissions
        assert not core.store.rows("SELECT * FROM outbox")
