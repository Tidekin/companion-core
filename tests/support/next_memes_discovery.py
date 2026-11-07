import asyncio
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from companion_meme_sources import DiscoveryLimits, ReviewApproval
from companion_onebot_bridge.meme_catalog import MemeCatalog
from companion_onebot_bridge.meme_discovery import BraveImages, run_discovery
from companion_onebot_bridge.meme_library import MemeLibrary
from tests.support.next_memes_catalog import check, metadata, png


async def async_check():
    pass


async def resolver(host, port):
    assert port == 443
    return ["93.184.216.34"]


class DurableBudget:
    """Synthetic implementation of the integration-owned durable reservation contract."""

    def __init__(self, db, *, daily_queries=5):
        self.db, self.daily_queries = db, daily_queries
        db.execute("CREATE TABLE IF NOT EXISTS test_meme_budget "
                   "(key TEXT PRIMARY KEY, state TEXT, reserved TEXT, actual TEXT)")

    async def reserve(self, key, limits):
        with self.db:
            if self.db.execute("SELECT 1 FROM test_meme_budget WHERE key=?", (key,)).fetchone():
                raise ValueError("ATTEMPT_ALREADY_RESERVED")
            if self.db.execute("SELECT COUNT(*) FROM test_meme_budget").fetchone()[0] >= (
                self.daily_queries
            ):
                raise ValueError("DAILY_QUERY_LIMIT")
            self.db.execute("INSERT INTO test_meme_budget VALUES (?,'RESERVED',?,NULL)",
                            (key, json.dumps(asdict(limits))))
        return Reservation(self.db, key)


class Reservation:
    def __init__(self, db, key):
        self.db, self.key = db, key

    async def check(self):
        assert self.db.execute("SELECT state FROM test_meme_budget WHERE key=?",
                               (self.key,)).fetchone()[0] == "RESERVED"

    async def finish(self, usage):
        with self.db:
            self.db.execute("UPDATE test_meme_budget SET state='SETTLED',actual=? WHERE key=?",
                            (json.dumps(asdict(usage)), self.key))

    async def unknown(self, usage):
        with self.db:
            self.db.execute("UPDATE test_meme_budget SET state='UNKNOWN',actual=? WHERE key=?",
                            (json.dumps(asdict(usage)), self.key))


@pytest.fixture
def setup(tmp_path):
    db = sqlite3.connect(tmp_path / "spool.sqlite3")
    catalog = MemeCatalog(db, MemeLibrary(tmp_path, enabled=True))
    yield catalog, DurableBudget(db)
    db.close()


async def review(candidate, image, evidence):
    # A production reviewer must verify content + usage evidence; no upstream trust shortcut.
    assert image.width == 24
    return ReviewApproval(metadata(
        source_page=candidate.source_page, asset_url=candidate.asset_url,
        revision=evidence.revision, fetched_at=evidence.fetch.fetched_at,
    ), evidence.sha256)


def transport_requests(calls):
    def handle(request):
        calls.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.extensions["sni_hostname"] == request.headers["Host"]
        assert "Cookie" not in request.headers and "Authorization" not in request.headers
        if request.headers["Host"] == "api.search.brave.com":
            assert request.headers["X-Subscription-Token"] == "synthetic-token"
            query = parse_qs(request.url.query.decode())
            assert query["q"] == ["庆祝 成功 表情包"]
            assert query["safesearch"] == ["strict"]
            return httpx.Response(200, json={"results": [
                {"url": "https://example.org/page", "title": "庆祝成功",
                 "properties": {"url": "https://images.example.org/one.png"}},
                {"url": "https://example.org/duplicate", "title": "庆祝",
                 "properties": {"url": "https://images.example.org/two.png"}},
            ]})
        assert "X-Subscription-Token" not in request.headers
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})
    return httpx.MockTransport(handle)


def discover(setup, calls, **kwargs):
    catalog, budget = setup
    return asyncio.run(run_discovery(
        "job-1", "celebrate", BraveImages("synthetic-token"), catalog,
        budget=budget, check=async_check, review=review, commit_check=check,
        transport=transport_requests(calls), resolver=resolver, **kwargs,
    ))


if __name__ == "__main__":
    database = Path(sys.argv[1])
    db = sqlite3.connect(database)
    try:
        catalog = MemeCatalog(db, MemeLibrary(database.parent, enabled=True))
        if len(sys.argv) > 2 and sys.argv[2] == "crash":
            asyncio.run(DurableBudget(db).reserve("job-1", DiscoveryLimits()))
            os._exit(23)
        print(json.dumps(discover((catalog, DurableBudget(db)), [])))
    finally:
        db.close()
