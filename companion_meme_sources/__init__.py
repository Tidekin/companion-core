"""Source facts shared by Core and Bridge; standard library only.

These values describe downloaded material. Applications retain all permission,
budget, storage, model and delivery responsibility.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass

CATEGORY = re.compile(r"[a-z][a-z0-9_-]{0,31}")

INTENTS = {
    "celebrate": ("celebrate", "congratulations", "happy", "开心", "庆祝", "成功"),
    "comfort": ("comfort", "hug", "安慰", "抱抱", "陪伴"),
    "like": ("like", "love", "喜欢", "喜爱", "爱心"),
    "surprised": ("surprised", "surprise", "惊讶", "意外"),
    "morning": ("morning", "早安", "早上好"),
    "sleep": ("sleep", "good night", "晚安", "睡觉"),
    "playful": ("playful", "joke", "调皮", "玩笑"),
}

MOCKERY = ("mock", "laugh at", "sarcas", "嘲笑", "嘲讽", "活该", "哈哈")


@dataclass(frozen=True)
class Metadata:
    category: str
    description: str
    tags: tuple[str, ...]
    suitable: tuple[str, ...]
    avoid: tuple[str, ...]
    source_page: str
    asset_url: str
    license: str
    usage_basis: str
    fetched_at: str
    revision: str = ""
    acquisition_json: str = ""

    def __post_init__(self):
        if not isinstance(self.category, str) or not CATEGORY.fullmatch(self.category):
            raise ValueError("MEME_CATEGORY_INVALID")
        for field, maximum in (
            (self.description, 2000), (self.source_page, 2048), (self.asset_url, 2048),
            (self.license, 256), (self.usage_basis, 512), (self.fetched_at, 64),
        ):
            if not isinstance(field, str) or not field.strip() or len(field) > maximum:
                raise ValueError("MEME_METADATA_INVALID")
        if not isinstance(self.revision, str) or len(self.revision) > 200:
            raise ValueError("MEME_METADATA_INVALID")
        if not isinstance(self.acquisition_json, str) or len(self.acquisition_json) > 32768:
            raise ValueError("MEME_METADATA_INVALID")
        if (not isinstance(self.tags, tuple) or len(self.tags) > 32
            or any(not isinstance(t, str) or not 1 <= len(t) <= 64 for t in self.tags)
            or any(not isinstance(v, tuple) or len(v) > len(INTENTS)
                   or any(t not in INTENTS for t in v) for v in (self.suitable, self.avoid))):
            raise ValueError("MEME_TAGS_INVALID")


@dataclass(frozen=True)
class DiscoveryLimits:
    queries: int = 1
    requests: int = 12
    download_bytes: int = 16 * 1024 * 1024
    storage_bytes: int = 4 * 1024 * 1024
    model_cost: int = 0
    max_images: int = 3

    def __post_init__(self):
        for value, low, high in (
            (self.queries, 1, 1), (self.requests, 1, 20),
            (self.download_bytes, 1, 32 * 1024 * 1024),
            (self.storage_bytes, 1, 16 * 1024 * 1024),
            (self.model_cost, 0, 0), (self.max_images, 1, 8),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("MEME_LIMIT_INVALID")


@dataclass
class Usage:
    queries: int = 0
    requests: int = 0
    download_bytes: int = 0
    storage_bytes: int = 0
    model_cost: int = 0


DEFAULT_LIMITS = DiscoveryLimits()


@dataclass(frozen=True)
class SearchImage:
    source_page: str
    asset_url: str
    description: str
    tags: tuple[str, ...] = ()
    license: str = "unknown"
    revision: str = ""
    semantic_verified: bool = False
    license_sha256: str = ""


@dataclass(frozen=True, slots=True)
class FetchHop:
    url: str
    address: str
    status: int


@dataclass(frozen=True, slots=True)
class FetchEvidence:
    requested_url: str
    final_url: str
    hops: tuple[FetchHop, ...]
    body_sha256: str
    fetched_at: str


@dataclass(frozen=True, slots=True)
class FetchedResource:
    data: bytes
    mime: str
    evidence: FetchEvidence


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    resource: FetchedResource
    path: str


@dataclass(frozen=True, slots=True)
class RepositoryImage:
    resource: FetchedResource
    manifest: FetchedResource
    license: FetchedResource
    listing: FetchedResource
    path: str


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    source_page: str  # Discovery claim, not a fetched licensing attestation.
    discovered_asset_url: str
    revision: str
    fetch: FetchEvidence
    image_sha256: str
    archive_member: str = ""
    source_files: dict[str, FetchEvidence] | None = None
    gallery_selection: dict | None = None

    @property
    def json(self):
        value = asdict(self)
        if self.source_files is None:
            del value["source_files"]  # Preserve every historical source/cache identity.
        if self.gallery_selection is None:
            del value["gallery_selection"]
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def sha256(self):
        return hashlib.sha256(self.json.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewApproval:
    metadata: Metadata
    evidence_sha256: str


