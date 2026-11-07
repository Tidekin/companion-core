"""Fixed upstream manifests/licenses; test-authored pixels and directory responses.

The real CC notice retains third-party rights. These fixtures test transport and
evidence bindings only, and do not grant permission to publish upstream artwork.
"""

import hashlib
import json
from dataclasses import replace

import pytest

from companion_meme_sources import DiscoveryLimits
from companion_meme_sources.community import INDEX_URL, CommunityPacks, file_manifest
from companion_meme_sources.review import TrustedReviewer
from tests.support.meme_pack_files import (
    FIXTURES,
    MANIFEST,
    RULE,
    Upstream,
    blob_sha,
    discover,
)
from tests.support.meme_pack_files import (
    catalog as catalog,
)
from tests.support.next_memes_catalog import check


def test_upstream_fixture_bytes_match_pinned_source_audit():
    for receipt in json.loads((FIXTURES / "verified-files.json").read_text(encoding="utf-8-sig")):
        data = (FIXTURES / receipt["file"]).read_bytes()
        assert len(data) == receipt["bytes"]
        assert hashlib.sha256(data).hexdigest() == receipt["sha256"]
        assert blob_sha(data) == receipt["git_blob_sha1_verified"]


@pytest.mark.parametrize("subpath", ["", "packs/example/"])
def test_actual_manifests_bounded_files_and_three_fetch_witnesses(catalog, subpath):
    upstream = Upstream(subpath=subpath, count=5)
    rule = {**RULE, "asset_prefix": subpath + "memes"}
    result = discover(catalog, upstream, rule=rule)
    assert result["accepted"] == [] and len(result["pending_caption"]) == 3
    assert len(upstream.calls) == result["usage"]["requests"] == 7
    assert result["usage"]["download_bytes"] == sum(map(len, upstream.bodies))
    assert result["usage"]["storage_bytes"] == sum(len(value) for value in
                                                   list(upstream.images.values())[:3])
    assert result["usage"]["model_cost"] == 0
    assert not any("/zip/" in url or "semantic_metadata" in url or "/commits/" in url
                   for url in upstream.calls)
    assert catalog.candidates("unready", "celebrate", check=check) == []
    for sha, raw in catalog.db.execute("SELECT sha,metadata FROM meme_assets"):
        stored = json.loads(raw)
        proof = json.loads(stored["acquisition_json"])
        assert set(proof) == {"source_page", "discovered_asset_url", "revision", "fetch",
                              "image_sha256", "archive_member", "source_files"}
        assert proof["archive_member"] == ""
        assert proof["fetch"]["body_sha256"] == proof["image_sha256"] == sha
        assert proof["fetch"]["final_url"] == stored["asset_url"]
        assert set(proof["source_files"]) == {"manifest", "license", "listing"}
        for role, url in {"manifest": upstream.manifest_url, "license": upstream.license_url,
                          "listing": upstream.listing_url}.items():
            fetch = proof["source_files"][role]
            assert fetch["requested_url"] == fetch["final_url"] == url
            assert fetch["hops"] == [{"url": url, "address": "93.184.216.34", "status": 200}]
        assert proof["source_files"]["license"]["body_sha256"] == RULE["license_sha256"]


def test_original_bom_manifest_is_supported_but_missing_license_still_blocks(catalog):
    original = (FIXTURES / "official-manifest.json").read_bytes()
    assert original.startswith(b"\xef\xbb\xbf")
    assert "schema_version" not in file_manifest(original)
    upstream = Upstream()
    upstream.pack["id"] = "official-001"
    upstream.manifest, upstream.license = original, None
    with pytest.raises(ValueError, match="MEME_HTTP_REJECTED"):
        discover(catalog, upstream)
    assert upstream.calls == [INDEX_URL, upstream.manifest_url, upstream.license_url]
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["manifest", "license", "listing", "image"])
def test_fixed_source_file_redirect_is_rejected(catalog, stage):
    upstream = Upstream()
    upstream.redirect = {"manifest": upstream.manifest_url, "license": upstream.license_url,
                         "listing": upstream.listing_url,
                         "image": upstream.prefix + next(iter(upstream.images))}[stage]
    with pytest.raises(ValueError, match="MEME_REDIRECT_REJECTED"):
        discover(catalog, upstream)
    assert not any("untrusted.example" in url for url in upstream.calls)
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0


@pytest.mark.parametrize("bad", ["license", "manifest_license", "blob", "traversal", "duplicate"])
def test_provider_cannot_replace_licensed_bytes_or_listing_members(catalog, bad):
    upstream = Upstream()
    if bad == "license":
        upstream.license = b"A code license does not attest to the approved asset license"
    elif bad == "manifest_license":
        upstream.manifest = json.dumps({**json.loads(MANIFEST), "license": "MIT"}).encode()
    elif bad == "blob":
        upstream.entries[0]["sha"] = "0" * 40
    elif bad == "traversal":
        upstream.entries[0]["path"] = "memes/happy/../../outside.png"
    else:
        upstream.entries.append(upstream.entries[0])
    with pytest.raises(ValueError, match="MEME_PACK"):
        discover(catalog, upstream)
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0
    state, actual = catalog.db.execute("SELECT state,actual FROM test_meme_budget").fetchone()
    assert state == "UNKNOWN" and json.loads(actual)["download_bytes"] > 0


@pytest.mark.parametrize("bad", ["symlink", "submodule", "large", "zero"])
def test_nonregular_or_oversized_listing_members_are_not_requested(catalog, bad):
    upstream = Upstream(count=1)
    changes = {"symlink": {"type": "symlink"}, "submodule": {"submodule_git_url": "git://x"},
               "large": {"size": 4 * 1024 * 1024 + 1}, "zero": {"size": 0}}
    upstream.entries[0].update(changes[bad])
    result = discover(catalog, upstream)
    assert result["accepted"] == [] and len(upstream.calls) == 4
    assert "pending_caption" not in result


def test_reviewer_rejects_mismatched_source_document_url(catalog):
    upstream = Upstream(count=1)
    reviewer = TrustedReviewer([RULE], require_semantics=False)

    async def mismatched(candidate, image, evidence):
        files = dict(evidence.source_files)
        files["manifest"] = files["license"]
        assert await reviewer(candidate, image, replace(evidence, source_files=files)) is None
        return None

    result = discover(catalog, upstream, reviewer=mismatched,
                      provider=CommunityPacks(reviewer.license_files))
    assert result["rejected"] == 1 and result["accepted"] == []


@pytest.mark.parametrize("role", ["resource", "manifest", "license", "listing"])
def test_every_document_must_be_issued_by_the_same_public_fetcher(catalog, role):
    upstream = Upstream(count=1)
    reviewer = TrustedReviewer([RULE], require_semantics=False)

    class Forged:
        async def discover(self, intent, fetcher):
            async for candidate, source in CommunityPacks(reviewer.license_files).discover(
                intent, fetcher
            ):
                yield candidate, replace(source, **{role: replace(getattr(source, role))})

    with pytest.raises(ValueError, match="MEME_FETCH_EVIDENCE_INVALID"):
        discover(catalog, upstream, provider=Forged())
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0


def test_existing_request_quota_still_applies_before_image_fetch(catalog):
    upstream = Upstream(count=1)
    with pytest.raises(ValueError, match="MEME_REQUEST_LIMIT"):
        discover(catalog, upstream, limits=DiscoveryLimits(requests=4))
    assert len(upstream.calls) == 4


def test_cancel_after_license_prevents_listing_and_image_io(catalog):
    upstream = Upstream(count=1)
    stopped = False

    def after(url):
        nonlocal stopped
        stopped = url == upstream.license_url

    async def fence():
        if stopped:
            raise ValueError("RECALLED")

    upstream.after_request = after
    with pytest.raises(ValueError, match="RECALLED"):
        discover(catalog, upstream, fence=fence)
    assert len(upstream.calls) == 3
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0


def test_unapproved_index_sources_never_consume_file_requests(catalog):
    upstream = Upstream(count=1)
    upstream.index_packs[:0] = [
        {**upstream.pack, "id": name, "description": "celebrate " * 8,
         "source": {**upstream.pack["source"], "repo": "unapproved/" + name}}
        for name in ("clover-days", "official-001")
    ]
    result = discover(catalog, upstream)
    assert len(result["pending_caption"]) == 1 and len(upstream.calls) == 5
    assert not any("unapproved" in url for url in upstream.calls)
