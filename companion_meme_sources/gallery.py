"""Owner-selected Aigengtu originals over the existing bounded public fetcher.

The selected paths and original index digest are operator configuration. They are
not represented as a live index fetch or as an image's caption. Original terms are
fetched at a pinned upstream revision; images are still decoded and captioned by Core.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from companion_meme_sources import (
    INTENTS,
    FetchedResource,
    Metadata,
    ReviewApproval,
    ReviewEvidence,
    SearchImage,
)
from companion_meme_sources.review import CATEGORIES

INDEX_URL = "https://aigengtu.com/gallery-index.json"
IMAGE_BASE = "https://img.aigengtu.com/meme/"
PAGE_BASE = "https://aigengtu.com/meme/"
TERMS_BASE = ("https://raw.githubusercontent.com/"
              "the-beating-light-of-the-nail/deepseek-chan-meme-pack/")
LICENSE = "owner-reviewed-personal-chat"
SHA = re.compile(r"[a-f0-9]{64}")
PATH = re.compile(r"[0-9]{3,6}\.(?:png|jpg|jpeg|gif)")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class Selection:
    def __init__(self, value):
        if (not isinstance(value, dict) or set(value) != {
            "version", "index_sha256", "terms_revision", "terms_sha256", "usage_basis", "images"}
            or type(value["version"]) is not int or value["version"] != 1
            or any(not isinstance(value[k], str) or not SHA.fullmatch(value[k])
                   for k in ("index_sha256", "terms_sha256"))
            or not isinstance(value["terms_revision"], str)
            or not re.fullmatch(r"[a-f0-9]{40}", value["terms_revision"])
            or not isinstance(value["usage_basis"], str)
            or not 1 <= len(value["usage_basis"].strip()) <= 256
            or not isinstance(value["images"], list) or not 1 <= len(value["images"]) <= 512):
            raise ValueError("MEME_GALLERY_SELECTION_INVALID")
        paths = set()
        for item in value["images"]:
            if (not isinstance(item, dict) or set(item) != {"path", "intents"}
                or not isinstance(item["path"], str) or not PATH.fullmatch(item["path"])
                or item["path"] in paths or not isinstance(item["intents"], list)
                or not 1 <= len(item["intents"]) <= len(INTENTS)
                or any(not isinstance(i, str) or i not in INTENTS for i in item["intents"])
                or len(set(item["intents"])) != len(item["intents"])):
                raise ValueError("MEME_GALLERY_SELECTION_INVALID")
            paths.add(item["path"])
        self.value = json.loads(json.dumps(value))
        self.sha256 = digest(value)
        self.entries = {item["path"]: item for item in self.value["images"]}
        self.terms_url = TERMS_BASE + value["terms_revision"] + "/README.md"


@dataclass(frozen=True, slots=True)
class GalleryImage:
    resource: FetchedResource
    terms: FetchedResource
    path: str


def _exact(fetch, url):
    if isinstance(fetch, dict):
        return (fetch["requested_url"] == fetch["final_url"] == url
                and len(fetch["hops"]) == 1 and fetch["hops"][0]["url"] == url
                and fetch["hops"][0]["status"] == 200)
    return (fetch.requested_url == fetch.final_url == url and len(fetch.hops) == 1
            and fetch.hops[0].url == url and fetch.hops[0].status == 200)


def proof(value, acquired):
    """Structural proof only; Core checks fetch shape and current configured selection."""
    selected = acquired["gallery_selection"]
    if (not isinstance(selected, dict) or set(selected) != {
        "index_sha256", "selection_sha256", "path", "terms_revision", "terms"}
        or any(not isinstance(selected[k], str) or not SHA.fullmatch(selected[k])
               for k in ("index_sha256", "selection_sha256"))
        or not isinstance(selected["path"], str) or not PATH.fullmatch(selected["path"])
        or not isinstance(selected["terms_revision"], str)
        or not re.fullmatch(r"[a-f0-9]{40}", selected["terms_revision"])
        or "source_files" in acquired or acquired["archive_member"]
        or value["revision"] != selected["selection_sha256"]
        or value["license"] != LICENSE
        or value["source_page"] != PAGE_BASE + selected["path"].split(".")[0]
        or value["asset_url"] != IMAGE_BASE + selected["path"]
        or acquired["discovered_asset_url"] != value["asset_url"]
        or not _exact(acquired["fetch"], value["asset_url"])
        or not _exact(selected["terms"], TERMS_BASE + selected["terms_revision"] + "/README.md")):
        raise ValueError("MEME_GALLERY_EVIDENCE_INVALID")


def allowed(metadata, selection):
    try:
        acquired = json.loads(metadata["acquisition_json"])
        proof(metadata, acquired)
        selected = acquired["gallery_selection"]
        return (selected["selection_sha256"] == selection.sha256
                and selected["index_sha256"] == selection.value["index_sha256"]
                and selected["terms_revision"] == selection.value["terms_revision"]
                and selected["terms"]["body_sha256"] == selection.value["terms_sha256"]
                and selected["path"] in selection.entries
                and metadata["usage_basis"] == selection.value["usage_basis"])
    except (ValueError, KeyError, TypeError):
        return False


class AigengtuGallery:
    def __init__(self, selection, *, known=()):
        self.selection = selection
        self.known = frozenset(known)

    async def discover(self, intent, fetcher):
        if intent not in INTENTS:
            raise ValueError("MEME_GALLERY_INTENT_INVALID")
        await fetcher.begin_query()
        selected = [item for item in self.selection.entries.values()
                    if intent in item["intents"] and IMAGE_BASE + item["path"] not in self.known]
        if not selected:
            return
        terms = await fetcher.get(self.selection.terms_url, 64 * 1024, redirects=False)
        if (terms.evidence.body_sha256 != self.selection.value["terms_sha256"]
            or not _exact(terms.evidence, self.selection.terms_url)):
            raise ValueError("MEME_GALLERY_TERMS_CHANGED")
        staged = 0
        for item in selected[:fetcher.limits.max_images]:
            url = IMAGE_BASE + item["path"]
            resource = await fetcher.get(url, 4 * 1024 * 1024, redirects=False)
            if resource.mime not in {"image/png", "image/jpeg", "image/gif"}:
                continue
            if staged + len(resource.data) > fetcher.limits.storage_bytes:
                continue
            staged += len(resource.data)
            yield SearchImage(PAGE_BASE + item["path"].split(".")[0], url,
                "待理解素材", license=LICENSE, revision=self.selection.sha256), GalleryImage(
                    resource, terms, item["path"])

    def review(self, candidate, supplied, image, fetcher):
        selection = self.selection
        resource, terms = fetcher.verify(supplied.resource), fetcher.verify(supplied.terms)
        if (supplied.path not in selection.entries or resource.data != image.data
            or hashlib.sha256(image.data).hexdigest() != image.sha256
            or resource.evidence.body_sha256 != image.sha256
            or terms.evidence.body_sha256 != selection.value["terms_sha256"]):
            raise ValueError("MEME_GALLERY_EVIDENCE_INVALID")
        selected = {"index_sha256": selection.value["index_sha256"],
                    "selection_sha256": selection.sha256, "path": supplied.path,
                    "terms_revision": selection.value["terms_revision"],
                    "terms": asdict(terms.evidence)}
        evidence = ReviewEvidence(candidate.source_page, candidate.asset_url, candidate.revision,
            resource.evidence, image.sha256, gallery_selection=selected)
        category = CATEGORIES[selection.entries[supplied.path]["intents"][0]]
        metadata = Metadata(category, "待理解素材", (), (), (), candidate.source_page,
            candidate.asset_url, LICENSE, selection.value["usage_basis"],
            resource.evidence.fetched_at, selection.sha256, evidence.json)
        if not allowed(asdict(metadata), selection):
            raise ValueError("MEME_GALLERY_EVIDENCE_INVALID")
        return ReviewApproval(metadata, evidence.sha256), evidence
