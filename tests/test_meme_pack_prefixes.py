"""Narrow regressions for exact category and multiple approved directory scopes."""

import asyncio
import json
from pathlib import PurePosixPath
from urllib.parse import quote

import pytest

from companion_meme_sources.community import CommunityPacks
from companion_meme_sources.review import TrustedReviewer
from companion_onebot_bridge.meme_discovery import run_discovery
from tests.support.meme_pack_files import (
    MANIFEST,
    REPO,
    REVISION,
    RULE,
    Upstream,
    blob_sha,
    discover,
)
from tests.support.meme_pack_files import catalog as catalog
from tests.support.meme_runtime import RULE as LEGACY_RULE
from tests.support.meme_runtime import public_transport
from tests.support.next_memes_catalog import check, png
from tests.support.next_memes_discovery import DurableBudget, async_check, resolver


@pytest.mark.parametrize("root", ["", "packs/example/"])
@pytest.mark.parametrize("scope", ["memes", "memes/happy", "memes/happy/"])
def test_exact_category_or_parent_scope_imports_only_children(catalog, root, scope):
    upstream = Upstream(subpath=root, count=1)
    result = discover(catalog, upstream, rule={**RULE, "asset_prefix": root + scope})
    assert len(result["pending_caption"]) == 1 and result["accepted"] == []
    assert len(upstream.calls) == result["usage"]["requests"] == 5
    stored = json.loads(catalog.db.execute("SELECT metadata FROM meme_assets").fetchone()[0])
    assert stored["asset_url"] == upstream.prefix + root + "memes/happy/0.png"


@pytest.mark.parametrize("prefix", [
    "memes/happ", "memes/happy_extra", "other", "memes/happy/0.png",
])
def test_partial_sibling_or_file_scope_does_not_widen_to_category(catalog, prefix):
    upstream = Upstream(count=1)
    result = discover(catalog, upstream, rule={**RULE, "asset_prefix": prefix})
    assert result["accepted"] == [] and "pending_caption" not in result
    assert upstream.calls == [upstream.calls[0], upstream.manifest_url, upstream.license_url]
    assert result["usage"]["requests"] == 3 and result["usage"]["storage_bytes"] == 0


class Categories(Upstream):
    def __init__(self, root):
        super().__init__(subpath=root, count=0)
        self.manifest = json.dumps({**json.loads(MANIFEST), "categories": {
            # Higher scoring unapproved categories must not consume the two-category limit.
            "blocked_a": {"description": "celebrate congratulations happy 开心 庆祝 成功"},
            "blocked_b": {"description": "celebrate congratulations happy 开心 庆祝 成功"},
            "happy": {"description": "celebrate"},
            "surprised": {"description": "celebrate"},
        }}).encode()
        self.listings = {}
        for index, category in enumerate(("happy", "surprised")):
            directory = root + "memes/" + category
            path = directory + "/only.png"
            data = png(color=(index * 80, 150, 60))
            self.images[path] = data
            url = f"https://api.github.com/repos/{REPO}/contents/{quote(directory)}?ref={REVISION}"
            self.listings[url] = [{"path": path, "name": PurePosixPath(path).name,
                                   "type": "file", "size": len(data), "sha": blob_sha(data)}]

    def handle(self, request):
        url = str(request.url.copy_with(host=request.headers["Host"]))
        if url in self.listings:
            self.listing_url, self.entries = url, self.listings[url]
        return super().handle(request)


@pytest.mark.parametrize("root", ["", "packs/example/"])
def test_multiple_narrow_rules_share_license_and_preserve_each_usage_basis(catalog, root):
    upstream = Categories(root)
    rules = [{**RULE, "asset_prefix": root + "memes/" + category,
              "usage_basis": "Approved fixture category " + category}
             for category in ("happy", "surprised")]
    reviewer = TrustedReviewer(rules, require_semantics=False)
    result = discover(catalog, upstream, reviewer=reviewer)
    assert len(result["pending_caption"]) == 2 and result["accepted"] == []
    assert upstream.calls.count(upstream.manifest_url) == 1
    assert upstream.calls.count(upstream.license_url) == 1
    assert len(upstream.calls) == result["usage"]["requests"] == 7
    assert not any("blocked_" in url for url in upstream.calls)
    for raw, in catalog.db.execute("SELECT metadata FROM meme_assets"):
        stored = json.loads(raw)
        assert stored["usage_basis"].startswith("Approved fixture category " + stored["category"])
    assert catalog.candidates("still-pending", "celebrate", check=check) == []


@pytest.mark.parametrize("require_semantics", [False, True])
@pytest.mark.parametrize("field,value", [
    ("license_path", "OTHER-LICENSE.md"), ("license_sha256", "0" * 64), ("license", "CC0-1.0"),
])
def test_same_version_rules_cannot_conflict_on_license(require_semantics, field, value):
    with pytest.raises(ValueError, match="MEME_TRUST_RULE_INVALID"):
        TrustedReviewer([RULE, {**RULE, "asset_prefix": "memes/happy", field: value}],
                        require_semantics=require_semantics)


def test_legacy_multiple_directory_rules_retain_zip_and_old_proof(catalog):
    rules = [{**LEGACY_RULE, "asset_prefix": prefix} for prefix in ("memes/happy", "memes/sad")]
    reviewer = TrustedReviewer(rules)
    calls = []
    result = asyncio.run(run_discovery(
        "legacy", "celebrate", CommunityPacks(reviewer.license_files), catalog,
        budget=DurableBudget(catalog.db), check=async_check, commit_check=check, review=reviewer,
        transport=public_transport(calls), resolver=resolver,
    ))
    assert len(calls) == result["usage"]["requests"] == 3
    assert calls[-1].headers["Host"] == "codeload.github.com"
    stored = json.loads(catalog.db.execute("SELECT metadata FROM meme_assets").fetchone()[0])
    proof = json.loads(stored["acquisition_json"])
    assert "source_files" not in proof and proof["archive_member"]


def test_exact_category_still_rejects_listing_entry_outside_it(catalog):
    upstream = Upstream(count=1)
    upstream.entries[0]["path"] = "memes/sad/0.png"
    with pytest.raises(ValueError, match="MEME_PACK_LISTING_INVALID"):
        discover(catalog, upstream, rule={**RULE, "asset_prefix": "memes/happy"})
    assert len(upstream.calls) == 4
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0
