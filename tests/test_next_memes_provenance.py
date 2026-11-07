import asyncio
import hashlib
import json
from dataclasses import FrozenInstanceError, replace

import httpx
import pytest

from companion_meme_sources import ArchiveMember, ReviewApproval, SearchImage
from companion_onebot_bridge.meme_discovery import run_discovery
from tests.support.next_memes_catalog import check, metadata, png
from tests.support.next_memes_discovery import async_check, resolver
from tests.support.next_memes_discovery import setup as setup

REQUESTED = "https://licensed.example/image.png"
FINAL = "https://unlicensed.example/image.png"


class RedirectCandidate:
    async def discover(self, intent, fetcher):
        await fetcher.begin_query()
        yield SearchImage("https://licensed.example/license", REQUESTED, "celebrate"), None


def test_review_sees_final_origin_before_license_decision(setup):
    catalog, budget = setup
    seen = []

    def handle(request):
        if request.headers["Host"] == "licensed.example":
            return httpx.Response(302, headers={"Location": FINAL})
        return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})

    async def licensed_only(candidate, image, *evidence):
        seen.append(candidate.asset_url)
        if candidate.asset_url != REQUESTED:
            return None
        return metadata(source_page=candidate.source_page, asset_url=candidate.asset_url)

    result = asyncio.run(run_discovery(
        "redirect-review", "celebrate", RedirectCandidate(), catalog, budget=budget,
        check=async_check, commit_check=check, review=licensed_only,
        transport=httpx.MockTransport(handle), resolver=resolver,
    ))
    assert result["accepted"] == []
    assert result["rejected"] == 1 and seen == [FINAL]
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0
    assert result["usage"] == {"queries": 1, "requests": 2, "download_bytes": len(png()),
                               "storage_bytes": 0, "model_cost": 0}


def redirect_transport(calls, *, cancel=None):
    def handle(request):
        calls.append(request)
        if request.headers["Host"] == "licensed.example":
            if cancel:
                cancel("redirect")
            return httpx.Response(302, headers={"Location": FINAL})
        if cancel:
            cancel("final")
        return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})
    return httpx.MockTransport(handle)


def test_allowed_cross_cdn_approval_binds_actual_bytes_and_full_acquisition(setup):
    catalog, budget = setup
    observed, calls = [], []

    async def permit_known_cdn(candidate, image, evidence):
        assert candidate.asset_url == FINAL
        assert evidence.discovered_asset_url == evidence.fetch.requested_url == REQUESTED
        assert evidence.fetch.final_url == FINAL
        assert [(h.url, h.address, h.status) for h in evidence.fetch.hops] == [
            (REQUESTED, "93.184.216.34", 302), (FINAL, "93.184.216.34", 200),
        ]
        assert evidence.image_sha256 == evidence.fetch.body_sha256 == image.sha256
        assert image.sha256 == hashlib.sha256(png()).hexdigest()
        with pytest.raises(FrozenInstanceError):
            evidence.fetch.final_url = REQUESTED
        observed.append(evidence)
        return ReviewApproval(metadata(
            source_page=candidate.source_page, asset_url=FINAL,
            revision=evidence.revision, fetched_at=evidence.fetch.fetched_at,
        ), evidence.sha256)

    result = asyncio.run(run_discovery(
        "approved-cdn", "celebrate", RedirectCandidate(), catalog, budget=budget,
        check=async_check, commit_check=check, review=permit_known_cdn,
        transport=redirect_transport(calls), resolver=resolver,
    ))
    assert len(calls) == 2 and len(result["accepted"]) == 1
    stored = json.loads(catalog.db.execute("SELECT metadata FROM meme_assets").fetchone()[0])
    assert stored["asset_url"] == FINAL and stored["acquisition_json"] == observed[0].json
    assert result["usage"]["download_bytes"] == result["usage"]["storage_bytes"] == len(png())


@pytest.mark.parametrize("mismatch", ["source", "bytes", "redirect", "old_callback"])
def test_approval_cannot_switch_reviewed_source_or_replay_a_different_acquisition(setup, mismatch):
    catalog, budget = setup

    async def wrong_approval(candidate, image, evidence):
        approved = metadata(source_page=candidate.source_page, asset_url=candidate.asset_url,
                            fetched_at=evidence.fetch.fetched_at)
        if mismatch == "source":
            return ReviewApproval(replace(approved, asset_url=REQUESTED), evidence.sha256)
        if mismatch == "old_callback":
            return approved
        altered = (replace(evidence, image_sha256="0" * 64) if mismatch == "bytes" else
                   replace(evidence, fetch=replace(evidence.fetch, hops=evidence.fetch.hops[1:])))
        return ReviewApproval(approved, altered.sha256)

    with pytest.raises(ValueError, match="MEME_REVIEW_"):
        asyncio.run(run_discovery(
            "wrong-approval", "celebrate", RedirectCandidate(), catalog, budget=budget,
            check=async_check, commit_check=check, review=wrong_approval,
            transport=redirect_transport([]), resolver=resolver,
        ))
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0
    state, usage = catalog.db.execute("SELECT state,actual FROM test_meme_budget").fetchone()
    assert state == "UNKNOWN"
    assert json.loads(usage) == {"queries": 1, "requests": 2, "download_bytes": len(png()),
                                "storage_bytes": 0, "model_cost": 0}


@pytest.mark.parametrize("cancel_at", ["redirect", "review"])
def test_cancellation_retains_redirect_usage_and_prevents_late_approval(setup, cancel_at):
    catalog, budget = setup
    stopped, calls, reviews = False, [], []

    def cancel(stage):
        nonlocal stopped
        if stage == cancel_at:
            stopped = True

    async def fence():
        if stopped:
            raise ValueError("RECALLED")

    async def late_approval(candidate, image, evidence):
        reviews.append(evidence)
        cancel("review")
        return ReviewApproval(metadata(
            source_page=candidate.source_page, asset_url=candidate.asset_url,
            fetched_at=evidence.fetch.fetched_at,
        ), evidence.sha256)

    with pytest.raises(ValueError, match="RECALLED"):
        asyncio.run(run_discovery(
            "cancel-redirect", "celebrate", RedirectCandidate(), catalog, budget=budget,
            check=fence, commit_check=check, review=late_approval,
            transport=redirect_transport(calls, cancel=cancel), resolver=resolver,
        ))
    assert len(calls) == (1 if cancel_at == "redirect" else 2)
    assert len(reviews) == (0 if cancel_at == "redirect" else 1)
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0
    state, usage = catalog.db.execute("SELECT state,actual FROM test_meme_budget").fetchone()
    assert state == "UNKNOWN"
    assert json.loads(usage) == {
        "queries": 1, "requests": len(calls),
        "download_bytes": 0 if cancel_at == "redirect" else len(png()),
        "storage_bytes": 0, "model_cost": 0,
    }


def test_provider_cannot_supply_forged_fetch_receipt(setup):
    catalog, budget = setup

    class ForgedProvider:
        async def discover(self, intent, fetcher):
            await fetcher.begin_query()
            resource = await fetcher.get(REQUESTED, 4096)
            forged = replace(resource, evidence=replace(resource.evidence, final_url=REQUESTED))
            yield SearchImage("https://licensed.example/license", REQUESTED, "celebrate"), (
                ArchiveMember(forged, "memes/happy/image.png")
            )

    async def never_review(*args):
        raise AssertionError("forged acquisition reached reviewer")

    with pytest.raises(ValueError, match="FETCH_EVIDENCE_INVALID"):
        asyncio.run(run_discovery(
            "forged-evidence", "celebrate", ForgedProvider(), catalog, budget=budget,
            check=async_check, commit_check=check, review=never_review,
            transport=redirect_transport([]), resolver=resolver,
        ))
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0
