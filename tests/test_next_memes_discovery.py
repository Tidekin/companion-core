import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from companion_meme_sources import DiscoveryLimits, Usage
from companion_meme_sources.fetch import PublicFetcher
from companion_onebot_bridge.meme_catalog import MemeCatalog
from companion_onebot_bridge.meme_discovery import BraveImages, run_discovery
from companion_onebot_bridge.meme_library import MemeLibrary
from tests.support.next_memes_catalog import check, png
from tests.support.next_memes_discovery import (
    DurableBudget,
    async_check,
    discover,
    resolver,
    review,
    transport_requests,
)
from tests.support.next_memes_discovery import (
    setup as setup,
)


def test_brave_search_download_review_dedup_and_provenance(setup):
    calls = []
    result = discover(setup, calls, limits=DiscoveryLimits(storage_bytes=len(png())))
    catalog, budget = setup
    assert len(calls) == 3 and len(result["accepted"]) == 1
    assert result["usage"]["storage_bytes"] == len(png())
    assert result["usage"]["queries"] == 1
    raw = catalog.db.execute("SELECT metadata FROM meme_assets").fetchone()[0]
    value = json.loads(raw)
    assert value["source_page"] == "https://example.org/page"
    assert value["asset_url"] == "https://images.example.org/one.png"
    assert value["license"] == "CC0-1.0" and value["fetched_at"].endswith("+00:00")
    assert catalog.db.execute("SELECT state FROM test_meme_budget").fetchone()[0] == "SETTLED"
    with pytest.raises(ValueError, match="ALREADY_RESERVED"):
        discover(setup, calls)
    assert len(calls) == 3


def test_missing_license_or_content_review_never_enters_library(setup):
    async def no_evidence(candidate, image, evidence):
        assert candidate.license == "unknown"
        return None

    catalog, budget = setup
    result = asyncio.run(run_discovery(
        "job", "celebrate", BraveImages("synthetic-token"), catalog,
        budget=budget, check=async_check, review=no_evidence, commit_check=check,
        transport=transport_requests([]), resolver=resolver,
    ))
    assert result["accepted"] == [] and result["rejected"] == 2
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["missing_key", "daily_quota", "storage", "database"])
def test_missing_config_limits_and_transaction_failure(setup, failure):
    catalog, budget = setup
    calls = []
    provider = BraveImages(None if failure == "missing_key" else "synthetic-token")
    if failure == "daily_quota":
        budget.daily_queries = 0
    if failure == "database":
        catalog.db.execute("CREATE TRIGGER fail_import BEFORE INSERT ON meme_assets "
                           "BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
    limits = DiscoveryLimits(storage_bytes=1 if failure == "storage" else 4194304)
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        asyncio.run(run_discovery(
            "job", "celebrate", provider, catalog, budget=budget,
            check=async_check, review=review, commit_check=check, limits=limits,
            transport=transport_requests(calls), resolver=resolver,
        ))
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0
    if failure in {"missing_key", "daily_quota"}:
        assert calls == []
    if failure != "daily_quota":
        assert catalog.db.execute("SELECT state FROM test_meme_budget").fetchone()[0] == "UNKNOWN"
    if failure == "missing_key":
        actual = json.loads(catalog.db.execute(
            "SELECT actual FROM test_meme_budget"
        ).fetchone()[0])
        assert actual == asdict(Usage())


def test_withdrawal_during_download_and_before_commit(setup):
    catalog, budget = setup
    stopped = False

    async def fence():
        if stopped:
            raise ValueError("RECALLED")

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal stopped
            stopped = True
            yield png()

    def handle(request):
        if request.headers["Host"] == "api.search.brave.com":
            return httpx.Response(200, json={"results": [
                {"url": "https://example.org/page", "properties": {
                    "url": "https://images.example.org/image.png"}},
            ]})
        return httpx.Response(200, stream=Chunks(), headers={"content-type": "image/png"})

    with pytest.raises(ValueError, match="RECALLED"):
        asyncio.run(run_discovery(
            "job", "celebrate", BraveImages("synthetic-token"), catalog,
            budget=budget, check=fence, review=review, commit_check=check,
            transport=httpx.MockTransport(handle), resolver=resolver,
        ))
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0
    raw = catalog.db.execute("SELECT actual FROM test_meme_budget").fetchone()[0]
    assert json.loads(raw)["download_bytes"] >= len(png())

    def commit_fence():
        raise ValueError("REPLY_SUPERSEDED")

    with pytest.raises(ValueError, match="SUPERSEDED"):
        asyncio.run(run_discovery(
            "different-job", "celebrate", BraveImages("synthetic-token"), catalog,
            budget=budget, check=async_check, review=review, commit_check=commit_fence,
            transport=transport_requests([]), resolver=resolver,
        ))
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0


@pytest.mark.parametrize("failure", [
    "private_dns", "mixed_dns", "redirect_private", "redirect_dns", "redirect_credentials",
    "redirect_loop", "404", "timeout", "gzip", "bytes", "requests",
])
def test_public_fetcher_rejects_network_abuse(failure):
    requests = []

    async def dns(host, port):
        if failure == "private_dns" or (failure == "redirect_dns" and host == "evil.example"):
            return ["127.0.0.1"]
        if failure == "mixed_dns":
            return ["93.184.216.34", "10.0.0.1"]
        return ["93.184.216.34"]

    def handle(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic")
        if failure.startswith("redirect"):
            destination = {
                "redirect_private": "https://127.0.0.1/a", "redirect_dns": "https://evil.example/a",
                "redirect_credentials": "https://safe.example/a", "redirect_loop": "/again",
            }[failure]
            return httpx.Response(302, headers={"location": destination})
        if failure == "404":
            return httpx.Response(404)
        return httpx.Response(200, content=b"12345", headers={
            "content-encoding": "gzip" if failure == "gzip" else "identity",
        })

    usage = Usage(requests=1 if failure == "requests" else 0)
    fetcher = PublicFetcher(DiscoveryLimits(requests=1 if failure == "requests" else 12),
                            usage, async_check, transport=httpx.MockTransport(handle), resolver=dns)
    with pytest.raises((ValueError, httpx.HTTPError)):
        asyncio.run(fetcher.get(
            "https://api.search.brave.com/test", 3 if failure == "bytes" else 1000,
            token="synthetic-token" if failure == "redirect_credentials" else None,
        ))
    if failure in {"private_dns", "mixed_dns", "requests"}:
        assert requests == []
    if failure in {"redirect_private", "redirect_dns", "redirect_credentials"}:
        assert len(requests) == 1


def test_redirect_rechecks_dns_and_sends_only_pinned_public_addresses():
    hosts, requests = [], []

    async def dns(host, port):
        hosts.append(host)
        return ["93.184.216.34"]

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "https://cdn.example.org/p.png"})
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    fetcher = PublicFetcher(DiscoveryLimits(), Usage(), async_check,
                            transport=httpx.MockTransport(handle), resolver=dns)
    result = asyncio.run(fetcher.get("https://example.org/p.png", 1000))
    assert (result.data, result.mime, result.evidence.final_url) == (
        png(), "image/png", "https://cdn.example.org/p.png"
    )
    assert hosts == ["example.org", "cdn.example.org"]
    assert all(r.url.host == "93.184.216.34" for r in requests)


def test_independent_worker_import_restart_and_budget_replay(tmp_path):
    database = tmp_path / "spool.sqlite3"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    worker = subprocess.run(
        [sys.executable, "-m", "tests.support.next_memes_discovery", str(database)],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert worker.returncode == 0, worker.stderr
    assert json.loads(worker.stdout)["status"] == "completed"
    db = sqlite3.connect(database)
    try:
        catalog = MemeCatalog(db, MemeLibrary(tmp_path, enabled=True))
        candidates = catalog.candidates("delivery", "celebrate", check=check)
        assert len(candidates) == 1
        assert catalog.prepare_selection("delivery", candidates[0]["candidate_id"],
                                         check=check).data == png()
        with pytest.raises(ValueError, match="ALREADY_RESERVED"):
            discover((catalog, DurableBudget(db)), [])
    finally:
        db.close()


def test_crashed_worker_retains_reservation_before_any_possible_retry(tmp_path):
    database = tmp_path / "spool.sqlite3"
    worker = subprocess.run(
        [sys.executable, "-m", "tests.support.next_memes_discovery", str(database), "crash"],
        cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True, text=True, timeout=30,
    )
    assert worker.returncode == 23, worker.stderr
    db = sqlite3.connect(database)
    try:
        budget = DurableBudget(db)
        assert db.execute("SELECT state FROM test_meme_budget").fetchone()[0] == "RESERVED"
        with pytest.raises(ValueError, match="ALREADY_RESERVED"):
            asyncio.run(budget.reserve("job-1", DiscoveryLimits()))
    finally:
        db.close()
