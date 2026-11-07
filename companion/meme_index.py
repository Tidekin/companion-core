"""Bounded, rebuildable caption vectors and self-contained frozen ranking proofs."""

import asyncio
import base64
import hashlib
import math
import struct
import time
from itertools import islice
from numbers import Real
from pathlib import Path
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from companion.contracts import CoreError, StrictModel
from companion.retrieval import INDEX_VERSION, _inference_lock, encoder
from companion_meme_protocol import canonical, digest

SHA = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
JOB = Annotated[str, Field(pattern=r"^job_[a-f0-9]{32}$")]
LIMIT, BATCH, DIMENSIONS = 4096, 32, 512
AMBIENT_LIMIT = 4
VECTOR_BYTES, RANKING_BYTES, OFFER_BYTES = 2048, 48 * 1024, 192 * 1024
PROFILE = {
    "version": 1,
    "model_version": INDEX_VERSION,
    "dimensions": DIMENSIONS,
    "vector_encoding": "f32le-512",
    "caption_text": "companion_meme_protocol.caption_text/v1:2000-chars",
    "query_text": "current-query_for/v3:2000-chars",
    "ranker": "caption-hybrid-cosine-f32-v3",
    "lexical_divisor": 20,
    "lexical_ceiling": 0.49,
    "cosine_threshold": 0.5,
    "selection_order": "score-desc,physical-sha-asc",
    "source_order": "asset-id-asc,exact-caption-job-id-asc",
    "scan_limit": LIMIT,
    "offer_limit": 8,
    "ambient_offer_limit": AMBIENT_LIMIT,
    "ambient_selection": "light-tone-category-diverse-v1",
    "cold_batch_limit": BATCH,
    "optional_budget_ms": 2000,
    "coverage_policy": "per-candidate-bounded-v2",
}
Fallback = Literal["LOCAL_WEIGHTS_MISSING", "INFERENCE_BUSY", "LOCAL_DEADLINE"]


class ProofModel(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def version_type(cls, value):
        if isinstance(value, dict) and "version" in value and type(value["version"]) is not int:
            raise ValueError("MEME_PROOF_VERSION_INVALID")
        return value


class CacheIdentity(ProofModel):
    version: Literal[1] = 1
    asset_id: SHA
    asset_sha256: SHA
    custody_sha256: SHA
    source_sha256: SHA
    record_sha256: SHA
    caption_job_id: JOB
    caption_identity_sha256: SHA
    caption_sha256: SHA
    model_version: Literal[INDEX_VERSION] = INDEX_VERSION
    dimensions: int = Field(default=DIMENSIONS, strict=True, ge=DIMENSIONS, le=DIMENSIONS)

    @classmethod
    def source(cls, binding):
        return cls.model_validate({k: v for k, v in binding.model_dump().items()
                                   if k in cls.model_fields and k != "version"})

    @property
    def key(self):
        return digest(self.model_dump(mode="json"))


def unpack(vector):
    if type(vector) is not bytes or len(vector) != VECTOR_BYTES:
        raise ValueError("MEME_VECTOR_INVALID")
    values = struct.unpack("<512f", vector)
    norm = sum(v * v for v in values)
    if not all(math.isfinite(v) for v in values) or not math.isfinite(norm) or norm <= 0:
        raise ValueError("MEME_VECTOR_INVALID")
    return values


def pack(values):
    values = tuple(islice(iter(values), DIMENSIONS + 1))
    if (len(values) != DIMENSIONS
        or any(isinstance(v, bool) or not isinstance(v, Real) for v in values)):
        raise ValueError("MEME_VECTOR_INVALID")
    vector = struct.pack("<512f", *(float(v) for v in values))
    unpack(vector)
    return vector


def vector_sha(vector):
    return hashlib.sha256(vector).hexdigest()


def vector_text(vector):
    return base64.b64encode(vector).decode("ascii")


def decode_vector(text, sha):
    if type(text) is not str or len(text) != 2732 or type(sha) is not str:
        raise ValueError("MEME_VECTOR_INVALID")
    vector = base64.b64decode(text, validate=True)
    if vector_text(vector) != text or vector_sha(vector) != sha:
        raise ValueError("MEME_VECTOR_INVALID")
    unpack(vector)
    return vector


def cosine(query, passage):
    left, right = unpack(query), unpack(passage)
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        math.sqrt(sum(v * v for v in left)) * math.sqrt(sum(v * v for v in right))
    )


def cached(store, identity):
    """Called only in current authorized scope, inside its short transaction."""
    row = store.one('SELECT meme_vectors.cache_key AS cache_key, meme_vectors.asset_id AS '
        'asset_id, meme_vectors.caption_job_id AS caption_job_id, meme_vectors.asset_sha256 AS '
        'asset_sha256, meme_vectors.custody_sha256 AS custody_sha256, meme_vectors.source_sha256 '
        'AS source_sha256, meme_vectors.record_sha256 AS record_sha256, '
        'meme_vectors.caption_identity_sha256 AS caption_identity_sha256, '
        'meme_vectors.caption_sha256 AS caption_sha256, meme_vectors.model_version AS '
        'model_version, meme_vectors.dimensions AS dimensions, meme_vectors.vector_sha256 AS '
        'vector_sha256, meme_vectors.vector AS vector FROM meme_vectors AS meme_vectors WHERE '
        'meme_vectors.asset_id = %s AND meme_vectors.caption_job_id = %s',
                    (identity.asset_id, identity.caption_job_id))
    if not row:
        return None
    expected = identity.model_dump(exclude={"version"})
    try:
        if (row["cache_key"] != identity.key or any(row[k] != v for k, v in expected.items())
            or vector_sha(row["vector"]) != row["vector_sha256"]):
            raise ValueError("MEME_VECTOR_INVALID")
        unpack(row["vector"])
        return row["vector"]
    except (ValueError, TypeError):
        store.execute('DELETE FROM meme_vectors WHERE cache_key = %s AND asset_id = %s AND '
            'caption_job_id = %s', (row["cache_key"], identity.asset_id,
                                              identity.caption_job_id))
        return None


def persist(store, identity, vector):
    """The caller has re-resolved this exact snapshot in the offer transaction."""
    unpack(vector)
    old = store.one('SELECT 1 FROM meme_vectors WHERE asset_id = %s AND caption_job_id = %s',
                    (identity.asset_id, identity.caption_job_id))
    if not old and store.one('SELECT COUNT(*) AS n FROM meme_vectors')["n"] >= LIMIT:
        return
    store.execute('DELETE FROM meme_vectors WHERE asset_id = %s AND caption_job_id = %s',
                  (identity.asset_id, identity.caption_job_id))
    store.insert("meme_vectors", cache_key=identity.key,
                 **identity.model_dump(exclude={"version"}),
                 vector_sha256=vector_sha(vector), vector=vector)


class RankingHeader(ProofModel):
    profile: dict
    profile_sha256: SHA
    query_sha256: SHA
    mode: Literal["SEMANTIC", "HYBRID", "LEXICAL"]
    query_vector_sha256: SHA | None
    query_fallback_reason: Fallback | None
    scanned: int = Field(ge=0, le=LIMIT)
    eligible: int = Field(ge=0, le=LIMIT)
    cache_hits: int = Field(ge=0, le=LIMIT)
    cold_batch: int = Field(ge=0, le=BATCH)
    unindexed: int = Field(ge=0, le=LIMIT)

    @model_validator(mode="after")
    def consistent(self):
        if (canonical(self.profile) != canonical(PROFILE)
            or self.profile_sha256 != digest(PROFILE)
            or not self.cache_hits <= self.eligible <= self.scanned
            or self.cold_batch > self.eligible - self.cache_hits
            or self.unindexed > self.eligible
            or self.unindexed != (self.eligible if self.query_vector_sha256 is None else
                                  self.eligible - self.cache_hits - self.cold_batch)
            or (self.query_vector_sha256 is not None and self.query_fallback_reason is not None)
            or (self.query_vector_sha256 is not None and self.eligible == 0)
            or (self.query_vector_sha256 is None and self.eligible > 0
                and self.query_fallback_reason is None)
            or self.mode != ("LEXICAL" if self.query_vector_sha256 is None else
                             "HYBRID" if self.unindexed else "SEMANTIC")):
            raise ValueError("MEME_RANKING_INVALID")
        return self


class RankedCandidate(ProofModel):
    version: Literal[1]
    asset_id: SHA
    caption_job_id: JOB
    caption_sha256: SHA
    cache_key: SHA | None
    basis: Literal["SEMANTIC", "LEXICAL"]
    reason: Literal["VECTOR_AVAILABLE", "LOCAL_WEIGHTS_MISSING", "INFERENCE_BUSY",
                    "LOCAL_DEADLINE", "BATCH_LIMIT"]
    lexical_score: float = Field(ge=0, le=0.49, allow_inf_nan=False)
    cosine: float | None = Field(ge=-1.000000000001, le=1.000000000001, allow_inf_nan=False)
    score: float = Field(gt=0, le=1.5, allow_inf_nan=False)
    vector_sha256: SHA | None
    vector_base64: str | None = Field(max_length=2732)

    @field_validator("lexical_score", "cosine", "score", mode="before")
    @classmethod
    def float_type(cls, value):
        if value is not None and type(value) is not float:
            raise ValueError("MEME_RANKING_INVALID")
        return value


class Ranking(ProofModel):
    version: Literal[1]
    header: RankingHeader
    query_vector_base64: str | None = Field(max_length=2732)
    selected: dict[SHA, RankedCandidate] = Field(max_length=8)

    def verify(self, sha, binding, lexical):
        proof, head = self.selected[sha], self.header
        identity = CacheIdentity.source(binding)
        if (proof.asset_id != binding.asset_id or proof.caption_job_id != binding.caption_job_id
            or proof.caption_sha256 != binding.caption_sha256
            or abs(proof.lexical_score - lexical) > 1e-12):
            raise ValueError("MEME_RANKING_INVALID")
        if proof.basis == "SEMANTIC":
            if (self.query_vector_base64 is None or proof.reason != "VECTOR_AVAILABLE"
                or proof.cache_key != identity.key or proof.cosine is None):
                raise ValueError("MEME_RANKING_INVALID")
            similarity = cosine(decode_vector(self.query_vector_base64, head.query_vector_sha256),
                                decode_vector(proof.vector_base64, proof.vector_sha256))
            score = lexical + (similarity if similarity >= 0.5 else 0.0)
            if abs(proof.cosine - similarity) > 1e-12:
                raise ValueError("MEME_RANKING_INVALID")
        else:
            if (any(v is not None for v in (proof.cache_key, proof.cosine, proof.vector_sha256,
                                            proof.vector_base64))
                or proof.reason != ("BATCH_LIMIT" if self.query_vector_base64 is not None
                                    else head.query_fallback_reason)):
                raise ValueError("MEME_RANKING_INVALID")
            score = lexical
        if abs(proof.score - score) > 1e-12:
            raise ValueError("MEME_RANKING_INVALID")

    @model_validator(mode="after")
    def query_vector(self):
        if (self.query_vector_base64 is None) != (self.header.query_vector_sha256 is None):
            raise ValueError("MEME_RANKING_INVALID")
        if self.query_vector_base64 is not None:
            decode_vector(self.query_vector_base64, self.header.query_vector_sha256)
        return self


def ranking_sha(header, candidate):
    return digest({"header": header, "candidate": candidate})


def _infer(cache_dir, query, texts, held_lock):
    """Only immutable text/model facts enter CPU; no database or authority callback."""
    try:
        model = encoder(cache_dir, local_only=True)
        passages = tuple(pack(v) for v in islice(
            model.passage_embed(texts, batch_size=BATCH), len(texts) + 1
        )) if texts else ()
        questions = tuple(pack(v) for v in islice(model.query_embed(query), 2))
        if len(passages) != len(texts) or len(questions) != 1:
            raise ValueError("MEME_VECTOR_INVALID")
        return questions[0], passages
    finally:
        held_lock.release()


class MemeIndex:
    """One strong CPU-future reference on the existing persistent caption service."""

    def __init__(self):
        self._inflight = None

    def _finished(self, future):
        if self._inflight is future:
            self._inflight = None
        if not future.cancelled():
            future.exception()  # Consume failures after timeout/caller cancellation too.

    @staticmethod
    def _local_lock(cache_dir):
        path = str(Path(cache_dir).resolve())
        return path, _inference_lock(path)

    async def infer(self, cache_dir, query, texts, deadline):
        from huggingface_hub.errors import LocalEntryNotFoundError

        if (type(query) is not str or len(query) > 2000 or type(texts) is not tuple
            or len(texts) > BATCH or any(type(t) is not str or len(t) > 2000 for t in texts)):
            raise CoreError("MEME_INDEX_INPUT_INVALID", 500)
        if time.monotonic() >= deadline:
            return None, (), "LOCAL_DEADLINE"
        if self._inflight is not None and not self._inflight.done():
            return None, (), "INFERENCE_BUSY"
        cache_dir, held_lock = self._local_lock(cache_dir)
        if not held_lock.acquire(blocking=False):
            return None, (), "INFERENCE_BUSY"
        unique_texts = tuple(dict.fromkeys(texts))
        if time.monotonic() >= deadline:
            held_lock.release()  # Nothing was submitted; the caller still owns this lock.
            return None, (), "LOCAL_DEADLINE"
        try:
            # Synchronous submission makes pre-submission failure distinguishable.
            # Shield keeps even a queued worker alive to release its lock in finally.
            future = asyncio.get_running_loop().run_in_executor(
                None, _infer, cache_dir, query, unique_texts, held_lock
            )
        except BaseException:
            held_lock.release()
            raise
        self._inflight = future
        future.add_done_callback(self._finished)
        waiting = asyncio.timeout(max(0, deadline - time.monotonic()))
        try:
            async with waiting:
                query_vector, vectors = await asyncio.shield(future)
            by_text = dict(zip(unique_texts, vectors, strict=True))
            return query_vector, tuple(by_text[text] for text in texts), None
        except TimeoutError as error:
            if waiting.expired():
                return None, (), "LOCAL_DEADLINE"
            raise CoreError("MEME_INDEX_INFERENCE_FAILED", 503) from error
        except (LocalEntryNotFoundError, FileNotFoundError):
            return None, (), "LOCAL_WEIGHTS_MISSING"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise CoreError("MEME_INDEX_INFERENCE_FAILED", 503) from error
