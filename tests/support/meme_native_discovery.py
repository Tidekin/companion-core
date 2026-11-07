from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx

from companion import meme_policy
from companion.config import ImageInputPolicy
from companion.meme_core import MemeCaptionService
from companion.meme_retrieval import MemeRetrieval
from tests.meme_authority_fixture import admit, environment
from tests.support.meme_pack_files import RULE, Upstream
from tests.support.next_memes_discovery import resolver


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
