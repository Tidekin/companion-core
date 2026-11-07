import asyncio
import hashlib
import io
import json
import sqlite3
import stat
import zipfile

import httpx
import pytest

from companion_meme_sources.community import CommunityPacks, archive_images, bounded_archive
from companion_onebot_bridge.meme_catalog import MemeCatalog
from companion_onebot_bridge.meme_discovery import run_discovery
from companion_onebot_bridge.meme_library import MemeLibrary
from tests.support.next_memes_catalog import check, png
from tests.support.next_memes_discovery import DurableBudget, async_check, resolver, review
from tests.support.next_memes_packs import PACK, REVISION, archive


@pytest.mark.parametrize("semantic", [False, True])
@pytest.mark.parametrize("subpath", ["", "packs/example/"])
def test_pack_manifest_semantics_and_exact_source_revision(semantic, subpath):
    pack = {**PACK, "source": {**PACK["source"], "subpath": subpath.rstrip("/") or "."}}
    results = list(archive_images(archive(semantic=semantic, subpath=subpath),
                                  pack, REVISION, "celebrate"))
    assert len(results) == 1
    candidate, data, member_path = results[0]
    assert data == png() and candidate.revision == REVISION
    assert candidate.source_page.endswith(REVISION)
    assert candidate.asset_url.endswith("/" + subpath + "memes/happy/a.png")
    assert candidate.license == "CC0-1.0"
    assert member_path.endswith(subpath + "memes/happy/a.png")
    if semantic:
        assert "绿色" in candidate.description
    assert list(archive_images(archive(), PACK, REVISION, "sleep")) == []


@pytest.mark.parametrize("path", [
    "../escape.png", "/absolute.png", "C:/drive.png", "root/../../escape", "root\\backslash",
    "root/name:stream", "root/./a", "root//a", "root/file. ", "root/dot.",
])
def test_archive_path_traversal_never_extracts(path):
    data = archive(extra=(path, b"payload"))
    if "\\" in path:
        # Windows ZipFile normalizes names while writing. Restore both raw header names.
        data = data.replace(path.replace("\\", "/").encode(), path.encode())
    with pytest.raises(ValueError, match="PATH_INVALID"):
        list(archive_images(data, PACK, REVISION, "celebrate"))


def test_zip_symlinks_and_special_files_rejected():
    for kind in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
        entry = zipfile.ZipInfo("root/link")
        entry.create_system = 3
        entry.external_attr = (kind | 0o777) << 16
        with pytest.raises(ValueError, match="SPECIAL_FILE"):
            bounded_archive(archive(extra=(entry, b"../escape")))


def test_zip_bomb_duplicate_and_entry_count_limits():
    with pytest.raises(ValueError, match="EXPANSION_LIMIT"):
        bounded_archive(archive(extra=("root/bomb", b"0" * 1000000)))
    with pytest.raises(ValueError, match="DUPLICATE_PATH"):
        bounded_archive(archive(extra=("fixture-" + REVISION + "/MEMES/happy/a.png", png())))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zip_file:
        for number in range(1025):
            zip_file.writestr(f"root/{number}", b"x")
    with pytest.raises(ValueError, match="ENTRY_LIMIT"):
        bounded_archive(stream.getvalue())


@pytest.mark.parametrize("field,value", [
    ("entry_id", "0" * 64), ("content_sha256", "0" * 64),
    ("relative_path", "memes/happy/missing.png"), ("category", "sad"),
    ("caption_status", "invented"), ("caption", ""), ("tags", ["x" * 65]),
])
def test_supplied_semantics_cannot_lie_about_identity(field, value):
    data = archive(semantic=True, mutate=lambda row: row.update({field: value}))
    with pytest.raises(ValueError, match="MEME_PACK"):
        list(archive_images(data, PACK, REVISION, "celebrate"))


def test_community_real_index_contract_resolves_then_downloads_immutable_revision(tmp_path):
    calls = []
    pack_data = archive(semantic=True)

    def handle(request):
        calls.append(request)
        host = request.headers["Host"]
        if host == "raw.githubusercontent.com":
            assert request.url.path.endswith("/community-index.json")
            return httpx.Response(200, json={"schema_version": "1.0.0", "packs": [PACK]})
        if host == "api.github.com":
            assert request.url.path == "/repos/example/fixture/commits/main"
            return httpx.Response(200, json={"sha": REVISION})
        assert host == "codeload.github.com"
        assert request.url.path == f"/example/fixture/zip/{REVISION}"
        return httpx.Response(200, content=pack_data)

    db = sqlite3.connect(tmp_path / "spool.sqlite3")
    try:
        catalog = MemeCatalog(db, MemeLibrary(tmp_path, enabled=True))
        result = asyncio.run(run_discovery(
            "community-job", "celebrate", CommunityPacks(), catalog,
            budget=DurableBudget(db), check=async_check, review=review, commit_check=check,
            transport=httpx.MockTransport(handle), resolver=resolver,
        ))
        assert len(result["accepted"]) == 1 and len(calls) == 3
        stored = json.loads(db.execute("SELECT metadata FROM meme_assets").fetchone()[0])
        assert stored["revision"] == REVISION
        assert stored["asset_url"] == f"https://codeload.github.com/example/fixture/zip/{REVISION}"
        evidence = json.loads(stored["acquisition_json"])
        assert evidence["archive_member"].endswith("/memes/happy/a.png")
        assert evidence["image_sha256"] == hashlib.sha256(png()).hexdigest()
        assert evidence["fetch"]["body_sha256"] == hashlib.sha256(pack_data).hexdigest()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["spool.sqlite3"]
    finally:
        db.close()


def test_pinned_community_sources_cannot_redirect_and_claim_original_revision():
    from companion_meme_sources import DiscoveryLimits, Usage
    from companion_meme_sources.fetch import PublicFetcher

    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://public.example/forged-index"})

    async def discover():
        fetcher = PublicFetcher(DiscoveryLimits(), Usage(), async_check,
                                transport=httpx.MockTransport(handle), resolver=resolver)
        return [item async for item in CommunityPacks().discover("celebrate", fetcher)]

    with pytest.raises(ValueError, match="REDIRECT_REJECTED"):
        asyncio.run(discover())
    assert len(calls) == 1
