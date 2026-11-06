"""Owner-selected gallery originals use the native discovery/caption responsibility."""

import hashlib
import json
from dataclasses import asdict

import httpx
import pytest
from pydantic import ValidationError

from companion import meme_policy, meme_service, owner_authority
from companion.contracts import CoreError
from companion.meme_core import _proof
from companion_meme_protocol import digest
from companion_meme_sources.gallery import IMAGE_BASE, TERMS_BASE, Selection
from tests.conftest import run
from tests.test_meme_core import worker
from tests.test_meme_native_discovery import native_case, release_foreground
from tests.test_next_memes_catalog import png

TERMS = b"Test-authored gallery fixture. Personal noncommercial chat use.\n"
SELECTION = {
    "version": 1, "index_sha256": "a" * 64, "terms_revision": "b" * 40,
    "terms_sha256": hashlib.sha256(TERMS).hexdigest(), "usage_basis": "Synthetic personal chat",
    "images": [{"path": "001.png", "intents": ["comfort", "celebrate"]}],
}


class GalleryUpstream:
    def __init__(self):
        self.calls = []
        self.terms = TERMS
        self.image = png()
        self.redirect = False

    def handle(self, request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == request.extensions["sni_hostname"]
        original = str(request.url.copy_with(host=request.headers["Host"]))
        self.calls.append(original)
        if original == TERMS_BASE + SELECTION["terms_revision"] + "/README.md":
            return httpx.Response(200, content=self.terms, headers={"Content-Type": "text/plain"})
        assert original == IMAGE_BASE + "001.png"
        if self.redirect:
            return httpx.Response(302, headers={"Location": "https://different.invalid/001.png"})
        return httpx.Response(200, content=self.image, headers={"Content-Type": "image/png"})


def setup(c, upstream):
    c.service.discovery.transport = httpx.MockTransport(upstream.handle)


def test_native_gallery_originals_wait_for_original_caption_fee(core, tmp_path, monkeypatch):
    async def scenario():
        selection = meme_policy.GallerySource(**SELECTION)
        async with native_case(core, tmp_path, monkeypatch, text="想要温暖的抱抱",
            meme_gallery_source=selection, meme_trusted_sources=[]) as c:
            upstream = GalleryUpstream()
            setup(c, upstream)
            await c.retrieval.prepare(c.job)
            assert not upstream.calls
            request = meme_service._metadata(core.store, core.store.one(
                "SELECT job_id FROM jobs WHERE kind='meme_discovery'")["job_id"])
            assert request["provider"] == "gallery"
            release_foreground(c)
            assert await c.service.run_once("gallery-import")
            reservation = core.store.one("SELECT * FROM meme_discovery_reservations")
            assert reservation["state"] == "SETTLED" and reservation["requests"] == 2
            assert reservation["download_bytes"] == len(TERMS) + len(upstream.image)
            assert reservation["storage_bytes"] == len(upstream.image)
            asset = core.store.one("SELECT * FROM meme_assets")
            caption = core.store.one("SELECT * FROM meme_captions")
            assert asset["data"] == caption["asset_data"] == upstream.image
            assert not core.store.rows("SELECT * FROM model_usage")
            source = json.loads(caption["source_json"])
            _proof(source, asset["asset_sha256"], asset["source_sha256"])
            proof = source["acquisition"]["gallery_selection"]
            assert proof["selection_sha256"] == selection.selection().sha256
            assert proof["index_sha256"] == SELECTION["index_sha256"]
            assert proof["terms"]["body_sha256"] == SELECTION["terms_sha256"]
            assert "source_files" not in source["acquisition"]
            assert meme_policy.source_allowed(json.loads(asset["metadata_json"]), core.settings)
            calls = []
            assert await worker(core, calls)
            assert len(calls) == 1
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
            assert c.retrieval._resolve(asset["asset_id"], caption["job_id"])[0].asset_id == (
                asset["asset_id"])
            assert c.service.discovery._held(c.event["event_id"]) == {IMAGE_BASE + "001.png"}
            assert not core.store.rows("SELECT * FROM outbox")

    run(scenario())


@pytest.mark.parametrize("fault", ["terms", "redirect", "private_dns", "excluded"])
def test_gallery_refusals_keep_observed_acquisition_and_create_no_caption(
    core, tmp_path, monkeypatch, fault,
):
    async def scenario():
        upstream = GalleryUpstream()
        excluded = [hashlib.sha256(upstream.image).hexdigest()] if fault == "excluded" else []
        async with native_case(core, tmp_path, monkeypatch,
            meme_gallery_source=meme_policy.GallerySource(**SELECTION), meme_trusted_sources=[],
            meme_excluded_sha256=excluded) as c:
            setup(c, upstream)
            if fault == "terms":
                upstream.terms = b"Changed terms"
            elif fault == "redirect":
                upstream.redirect = True
            elif fault == "private_dns":
                async def private(host, port):
                    return ["127.0.0.1"]
                c.service.discovery.resolver = private
            await c.retrieval.prepare(c.job)
            release_foreground(c)
            assert await c.service.run_once("gallery-refusal")
            result = core.store.one("SELECT * FROM meme_discovery_reservations")
            assert result["state"] == ("SETTLED" if fault in {"terms", "excluded"} else "UNKNOWN")
            assert result["storage_bytes"] == 0
            assert not core.store.rows("SELECT * FROM meme_assets")
            assert not core.store.rows("SELECT * FROM meme_captions")
            assert not core.store.rows("SELECT * FROM model_usage")
            before = list(upstream.calls)
            assert not await c.service.run_once("gallery-no-retry")
            assert upstream.calls == before

    run(scenario())


@pytest.mark.parametrize("field,value", [
    ("version", True), ("terms_revision", "main"), ("index_sha256", "unknown"),
    ("images", [{"path": "../001.png", "intents": ["comfort"]}]),
    ("images", [{"path": "001.webp", "intents": ["comfort"]}]),
    ("images", [{"path": "001.png", "intents": ["unapproved"]}]),
    ("images", SELECTION["images"] * 2),
])
def test_gallery_selection_is_exact_and_bounded(field, value):
    with pytest.raises(ValidationError):
        meme_policy.GallerySource(**{**SELECTION, field: value})


def test_changed_gallery_selection_requires_current_owner_policy(core, tmp_path, monkeypatch):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch,
            meme_gallery_source=meme_policy.GallerySource(**SELECTION),
            meme_trusted_sources=[]) as c:
            before = meme_service.policy_hash(core.settings)
            core.settings.meme_gallery_source.images = [{"path": "002.png", "intents": ["comfort"]}]
            assert meme_service.policy_hash(core.settings) != before
            with pytest.raises(CoreError, match="OWNER_AUTHORITY_CAPABILITY_CHANGED"):
                owner_authority.check_job(core.store, core.settings, core.clock, c.job,
                                          purpose="meme")
            assert not core.store.rows("SELECT * FROM meme_discovery_reservations")
    run(scenario())


def test_retained_failed_gallery_caption_does_not_restart_import(core, tmp_path, monkeypatch):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch,
            meme_gallery_source=meme_policy.GallerySource(**SELECTION),
            meme_trusted_sources=[]) as c:
            upstream = GalleryUpstream()
            setup(c, upstream)
            await c.retrieval.prepare(c.job)
            release_foreground(c)
            assert await c.service.run_once("gallery-original")
            calls = []
            assert await worker(core, calls, content="not valid caption JSON")
            caption = core.store.one("SELECT job_id FROM meme_captions")
            assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                                  (caption["job_id"],))["state"] == "FAILED"
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
            assert c.service.discovery._held(c.event["event_id"]) == {IMAGE_BASE + "001.png"}
            assert len(calls) == 1
    run(scenario())


def test_gallery_proof_cannot_claim_another_host_or_unfetched_terms():
    from companion_meme_sources import FetchedResource, FetchEvidence, FetchHop, ReviewEvidence

    selection = Selection(SELECTION)
    raw = png()
    sha = hashlib.sha256(raw).hexdigest()
    now = "2026-01-01T00:00:00+00:00"

    def fetched(url, data, mime):
        return FetchedResource(data, mime, FetchEvidence(url, url,
            (FetchHop(url, "93.184.216.34", 200),), hashlib.sha256(data).hexdigest(), now))

    image = fetched(IMAGE_BASE + "001.png", raw, "image/png")
    terms = fetched(selection.terms_url, TERMS, "text/plain")
    evidence = ReviewEvidence("https://aigengtu.com/meme/001", IMAGE_BASE + "001.png",
        selection.sha256, image.evidence, sha, gallery_selection={
            "index_sha256": selection.value["index_sha256"], "selection_sha256": selection.sha256,
            "path": "001.png", "terms_revision": selection.value["terms_revision"],
            "terms": asdict(terms.evidence)})
    source = {"schema_version": 1, "source_page": evidence.source_page,
              "asset_url": evidence.discovered_asset_url, "license": "owner-reviewed-personal-chat",
              "usage_basis": SELECTION["usage_basis"], "fetched_at": now,
              "revision": selection.sha256, "acquisition": json.loads(evidence.json)}
    _proof(source, sha, digest(source))
    source["acquisition"]["gallery_selection"]["terms"]["hops"][0]["address"] = "127.0.0.1"
    with pytest.raises(CoreError, match="MEME_SOURCE_EVIDENCE_INVALID"):
        _proof(source, sha, digest(source))
