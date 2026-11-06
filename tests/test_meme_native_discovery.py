"""Native FILE admission uses signed owner facts and original attempt accounting."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from companion import meme_assets, meme_policy, meme_service
from companion.config import ImageInputPolicy
from companion.contracts import CoreError
from companion.meme_core import MemeCaptionService
from companion.meme_retrieval import MemeRetrieval
from tests.conftest import run
from tests.meme_authority_fixture import admit, environment
from tests.test_meme_core import worker
from tests.test_meme_pack_files import RULE, Upstream
from tests.test_next_memes_discovery import resolver


@asynccontextmanager
async def native_case(core, tmp_path, monkeypatch, *, text="太棒了，成功了", discovery=True,
                      **changes):
    core.settings = core.settings.model_copy(update={
        "meme_catalog_owner": "core", "meme_discovery_enabled": discovery,
        "meme_semantics_enabled": True, "meme_trusted_sources": [meme_policy.TrustedSource(**RULE)],
        "meme_caption_daily_micro_usd": 50000, "expression_selection_probability": 1.0,
        **changes,
    })
    paid = {"max_output_tokens": 4096, "meme_input": ImageInputPolicy(
        approval_sha256="e" * 64, estimated_tokens_per_image=100,
        max_bytes_per_image=4000000, max_pixels_per_image=16000000,
        max_images_per_request=5)}
    async with environment(core, tmp_path, monkeypatch, paid=paid) as (client, cfg, grant, now):
        event, response = await admit(client, cfg, grant, now, text=text)
        assert response.status_code == 202, response.text
        job = core.claim_job("native-offer", job_id=response.json()["job_id"])
        assert job is not None
        job = core.store.one("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],))
        assert job["state"] == "RUNNING"
        service = MemeCaptionService(core.store, core.settings, core.model, core.clock)
        upstream = Upstream(count=1)
        service.discovery.transport = httpx.MockTransport(upstream.handle)
        service.discovery.resolver = resolver
        case = SimpleNamespace(core=core, service=service, retrieval=MemeRetrieval(service),
            upstream=upstream, event=event, job=job, client=client, config=cfg,
            grant=grant, now=now)
        yield case


def release_foreground(case):
    case.core.store.execute("UPDATE jobs SET state='SUCCEEDED',lease_token=NULL WHERE job_id=?",
                            (case.job["job_id"],))


def test_strict_empty_offer_enqueues_once_and_staged_file_waits_for_original_caption_fee(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch) as c:
            await c.retrieval.prepare(c.job)
            frozen = core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                                    ("meme_offer:" + c.event["event_id"],))["value"]
            await c.retrieval.prepare(c.job)
            assert not c.upstream.calls and not core.store.rows("SELECT * FROM model_usage")
            jobs = core.store.rows("SELECT * FROM jobs WHERE kind='meme_discovery'")
            assert len(jobs) == 1 and jobs[0]["state"] == "QUEUED"
            assert json.loads(frozen)["offer"]["candidates"] == []
            release_foreground(c)  # Unit precondition; process acceptance executes the real chat.
            assert await c.service.run_once("native-discovery")
            reserved = core.store.one("SELECT * FROM meme_discovery_reservations")
            assert reserved["state"] == "SETTLED"
            assert reserved["queries"] == 1 and reserved["requests"] == len(c.upstream.calls) == 5
            assert reserved["download_bytes"] == sum(map(len, c.upstream.bodies))
            image = next(iter(c.upstream.images.values()))
            assert reserved["storage_bytes"] == len(image)
            asset = core.store.one("SELECT * FROM meme_assets")
            caption = core.store.one("SELECT * FROM meme_captions")
            assert asset["state"] == "STAGED" and asset["data"] == caption["asset_data"] == image
            assert not caption["result_json"] and not core.store.rows("SELECT * FROM model_usage")
            with pytest.raises(CoreError, match="MEME_CAPTION_UNAVAILABLE"):
                c.retrieval._resolve(asset["asset_id"], caption["job_id"])
            assert core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                                 ("meme_offer:" + c.event["event_id"],))["value"] == frozen
            calls = []
            assert await worker(core, calls)
            assert len(calls) == len(core.store.rows("SELECT * FROM model_usage")) == 1
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
            binding, _, _, sources, _ = c.retrieval._resolve(asset["asset_id"], caption["job_id"])
            assert binding.asset_id == asset["asset_id"] and sources
            assert meme_assets.original_custody(core.store, core.settings, c.event["event_id"])
            assert not core.store.rows("SELECT * FROM outbox")

    run(scenario())


@pytest.mark.parametrize("kind", ["no_intent", "disabled"])
def test_optional_empty_states_do_not_start_native_discovery(core, tmp_path, monkeypatch, kind):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch,
            text="普通的合成闲聊" if kind == "no_intent" else "太棒了，成功了",
            discovery=kind != "disabled") as c:
            await c.retrieval.prepare(c.job)
            assert not c.upstream.calls
            assert not core.store.rows("SELECT * FROM jobs WHERE kind='meme_discovery'")

    run(scenario())


@pytest.mark.parametrize("failure", ["license", "source_document", "dns"])
def test_source_rejection_preserves_observed_acquisition_and_never_creates_content(
    core, tmp_path, monkeypatch, failure
):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch) as c:
            await c.retrieval.prepare(c.job)
            release_foreground(c)
            if failure == "license":
                c.upstream.license = b"Different unapproved license bytes"
            elif failure == "source_document":
                c.upstream.manifest = b"{}"
            else:
                async def private_resolver(host, port):
                    return ["127.0.0.1"]
                c.service.discovery.resolver = private_resolver
            assert await c.service.run_once("native-denied")
            row = core.store.one("SELECT * FROM meme_discovery_reservations")
            assert row["state"] == ("UNKNOWN" if failure == "dns" else "SETTLED")
            assert row["queries"] == 1 and row["storage_bytes"] == 0
            assert row["download_bytes"] == sum(map(len, c.upstream.bodies))
            assert not core.store.rows("SELECT * FROM meme_assets")
            assert not core.store.rows("SELECT * FROM meme_captions")
            assert not core.store.rows("SELECT * FROM model_usage")
            before = list(c.upstream.calls)
            assert not await c.service.run_once("native-no-retry")
            assert c.upstream.calls == before

    run(scenario())


def test_acquisition_profile_change_fences_the_existing_core_grant(core, tmp_path, monkeypatch):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch) as c:
            await c.retrieval.prepare(c.job)
            release_foreground(c)
            profile = meme_service.acquisition_profile()
            monkeypatch.setattr(meme_service, "acquisition_profile",
                                lambda settings=None: {**profile, "version": 2})
            with pytest.raises(CoreError):
                await c.service.run_once("old-grant")
            assert not c.upstream.calls
            assert not core.store.rows("SELECT * FROM meme_discovery_reservations")

    run(scenario())
