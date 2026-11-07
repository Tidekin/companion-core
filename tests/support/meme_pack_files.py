import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from companion_meme_sources import DiscoveryLimits
from companion_meme_sources.community import INDEX_URL, CommunityPacks
from companion_meme_sources.review import TrustedReviewer
from companion_onebot_bridge.meme_caption import CaptionIdentity, EmbeddingSpec
from companion_onebot_bridge.meme_catalog import MemeCatalog
from companion_onebot_bridge.meme_discovery import run_discovery
from companion_onebot_bridge.meme_library import MemeLibrary
from companion_onebot_bridge.meme_semantic import SemanticLibrary
from tests.support.next_memes_catalog import check, png
from tests.support.next_memes_discovery import DurableBudget, async_check, resolver

FIXTURES = Path(__file__).parents[1] / "fixtures" / "meme-pack-files"


REPO = "DDZS987/astrbot-meme-pack-semantic-01"


REVISION = "8a50106a7e5948546eec73f73e1e2b747ff0302f"


LICENSE = (FIXTURES / "semantic-LICENSE.md").read_bytes()


MANIFEST = (FIXTURES / "semantic-manifest.json").read_bytes()


RULE = {
    "repo": REPO, "revision": REVISION, "license": "CC-BY-NC-SA-4.0",
    "license_path": "LICENSE.md",
    "license_sha256": "666d6e2c129f08d0bdec2bf75c6a7c793a6729cc66adbfd2af13cf4440574f7e",
    "asset_prefix": "memes", "usage_basis": "Test-authored pixels; fixture transport verification",
}


def blob_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class Upstream:
    def __init__(self, *, subpath="", count=3):
        self.calls, self.bodies = [], []
        self.prefix = f"https://raw.githubusercontent.com/{REPO}/{REVISION}/"
        self.directory = subpath + "memes/happy"
        self.manifest_url = self.prefix + subpath + "manifest.json"
        self.license_url = self.prefix + "LICENSE.md"
        self.listing_url = (f"https://api.github.com/repos/{REPO}/contents/"
                            + quote(self.directory) + "?ref=" + REVISION)
        self.pack = {
            "id": "ddzs987-semantic-001", "description": "Community semantic starter",
            "tags": ["community", "semantic", "v2", "starter"], "license": RULE["license"],
            "source": {"type": "github", "repo": REPO, "ref": "v1.1.0",
                       "subpath": subpath.rstrip("/") or "."},
        }
        self.index_packs = [self.pack]
        self.images = {self.directory + f"/{number}.png": png(color=(number * 30, 120, 90))
                       for number in range(count)}
        self.entries = [{"name": Path(path).name, "path": path, "type": "file",
                         "size": len(data), "sha": blob_sha(data),
                         "download_url": "https://untrusted.example/ignored.png"}
                        for path, data in self.images.items()]
        self.manifest, self.license = MANIFEST, LICENSE
        self.redirect = None
        self.after_request = None

    def handle(self, request):
        assert request.url.host == "93.184.216.34"
        assert request.extensions["sni_hostname"] == request.headers["Host"]
        assert "Cookie" not in request.headers and "Authorization" not in request.headers
        if request.headers["Host"] == "api.github.com":
            assert request.headers["User-Agent"] == "Companion-Core-meme-discovery/1"
        url = str(request.url.copy_with(host=request.headers["Host"]))
        self.calls.append(url)
        if self.after_request:
            self.after_request(url)
        if url == self.redirect:
            return httpx.Response(302, headers={"location": "https://untrusted.example/other"})
        if url == INDEX_URL:
            body = json.dumps({"schema_version": "1.0.0", "packs": self.index_packs}).encode()
        elif url == self.manifest_url:
            body = self.manifest
        elif url == self.license_url:
            if self.license is None:
                return httpx.Response(404)
            body = self.license
        elif url == self.listing_url:
            body = json.dumps(self.entries).encode()
        else:
            assert url.startswith(self.prefix)
            body = self.images[url[len(self.prefix):]]
        self.bodies.append(body)
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})


@pytest.fixture
def catalog(tmp_path):
    db = sqlite3.connect(tmp_path / "spool.sqlite3")
    catalog = MemeCatalog(db, MemeLibrary(tmp_path, enabled=True))
    SemanticLibrary(catalog, CaptionIdentity(model_version="fixture-no-model"),
                    EmbeddingSpec(model_version="fixture-no-embedding", dimensions=1))
    yield catalog
    db.close()


def discover(catalog, upstream, *, rule=RULE, provider=None, reviewer=None,
             fence=async_check, limits=None):
    reviewer = reviewer or TrustedReviewer([rule], require_semantics=False)
    provider = provider or CommunityPacks(reviewer.license_files)
    return asyncio.run(run_discovery(
        "files", "celebrate", provider, catalog, budget=DurableBudget(catalog.db),
        check=fence, commit_check=check, review=reviewer, limits=limits or DiscoveryLimits(),
        transport=httpx.MockTransport(upstream.handle), resolver=resolver,
    ))
