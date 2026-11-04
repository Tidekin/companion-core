"""Core-owned bounded hybrid offers over exact native source/usage evidence."""

import json
import re
import time
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from companion import meme_assets, meme_policy, owner_authority, owner_input, reply_revision
from companion import meme_index as index
from companion.contracts import CoreError
from companion.expression import MemeOffer, query_intent
from companion.privacy import epoch
from companion.store import after, encode
from companion_meme_protocol import (
    CaptionResult,
    caption_text,
    digest,
    parse,
    strict_json,
    validate_bytes,
)

SHA = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
OFFER = "meme_offer:"


def core_owned(settings):
    return getattr(settings, "meme_catalog_owner", "bridge") == "core"


class SourceBinding(index.ProofModel):
    asset_id: SHA
    asset_sha256: SHA
    custody_sha256: SHA
    source_sha256: SHA
    record_sha256: SHA
    caption_job_id: str = Field(pattern=r"^job_[a-f0-9]{32}$")
    caption_sha256: SHA
    caption_identity_sha256: SHA
    meme_policy_sha256: SHA
    privacy_epoch: int = Field(strict=True, ge=0)


class AssetBinding(SourceBinding):
    version: Literal[2]
    ranking_sha256: SHA


class FrozenOffer(index.ProofModel):
    job_id: index.JOB
    event_id: str
    reply_revision: int = Field(ge=1)
    query_sha256: SHA
    offer: MemeOffer | None
    catalog_owner: Literal["core"]
    binding_version: int = Field(ge=2, le=2)
    meme_policy_sha256: SHA
    privacy_epoch: int = Field(ge=0)
    catalog_bindings: dict[SHA, AssetBinding] = Field(max_length=8)
    ranking: index.Ranking


def media(settings, data):
    policy = settings.paid_model.meme_input if settings.paid_model else None
    if policy is None:
        raise CoreError("PAID_IMAGE_APPROVAL_REQUIRED", 503)
    return validate_bytes(
        data,
        max_bytes=min(4 * 1024 * 1024, policy.max_bytes_per_image),
        max_total_pixels=min(16_000_000, policy.max_pixels_per_image),
        max_dimension=2048,
        max_frames=120,
    )


def blocked(store, settings, sha, now, *, exclude=None):
    """Physical image identity, on the single authoritative SQLite transaction."""
    return bool(
        store.one(
            "SELECT 1 FROM outbox o JOIN conversations c USING(conversation_id) "
            "WHERE c.scope='private' AND c.owner_actor_id='owner' "
            "AND json_extract(o.content_json,'$.image.candidate_id')=? "
            "AND (? IS NULL OR o.message_id<>?) AND "
            "(o.state IN ('PENDING','RETRY_WAIT','LEASED','SENDING','DELIVERY_UNCERTAIN') "
            "OR (o.state='SENT' AND o.sent_at>=?)) LIMIT 1",
            (sha, exclude, exclude, after(now, -getattr(settings, "meme_cooldown_seconds", 1800))),
        )
    )


def query_for(text):
    text = text[-2000:]
    return {
        "text": text,
        "intent": query_intent(text, explicit_fallback=False),
        "target": "self" if re.search(r"我.{0,8}自嘲|自嘲.{0,8}我", text) else "recipient",
    }


def _candidate(sha, caption, meta):
    return {
        "candidate_id": sha,
        "category": meta["category"],
        "description": (
            caption.description
            + "\n图中文字："
            + caption.visible_text
            + "\n语气："
            + caption.tone
            + "；对象："
            + caption.target
        )[:2000],
        "tags": list(caption.tags),
    }


class MemeRetrieval:
    def __init__(self, service):
        self.service = service
        self.store, self.settings, self.clock = service.store, service.settings, service.clock

    def _chat(self, job, *, executing):
        view = owner_authority.check_job(
            self.store, self.settings, self.clock, job, purpose="chat", executing=executing
        )
        reply_revision.check_job(self.store, job)
        event, body, _ = owner_input.source(
            self.store, self.settings, view, job["event_id"], self.clock(), allow_commands=True
        )
        return view, event, body

    def optional_disabled(self, job, *, executing=True):
        # Validate the original chat grant and all of its bound policies first.
        # A changed/expired grant or source is never an optional capability miss.
        view, _, _ = self._chat(job, executing=executing)
        if self.store.one("SELECT 1 FROM runtime_meta WHERE key=?", (OFFER + job["event_id"],)):
            return False  # Even null/empty/corrupt frozen records retain their contract.
        return (
            not self.settings.meme_semantics_enabled
            or not self.settings.expression_enabled
            or not {"meme", "expression"} <= set(view.policy.purposes)
        )

    def _scope(self, job, *, executing):
        view, event, body = self._chat(job, executing=executing)
        if (
            not core_owned(self.settings)
            or not self.settings.meme_semantics_enabled
            or not self.settings.expression_enabled
        ):
            raise CoreError("MEME_CORE_MODE_REQUIRED", 403)
        owner_authority.current(
            self.store, self.settings, self.clock, purpose="meme", authority=view.authority
        )
        owner_authority.current(
            self.store, self.settings, self.clock, purpose="expression", authority=view.authority
        )
        return view, event, body

    def _resolve(self, asset_id, caption_job_id):
        """Exact source only. No latest-caption lookup or source substitution."""
        from companion import meme_service

        s = self.store
        asset = s.one("SELECT * FROM meme_assets WHERE asset_id=?", (asset_id,))
        if not asset or asset["state"] != "STAGED":
            raise CoreError("MEME_ASSET_UNAVAILABLE", 409)
        meta = meme_assets.metadata(json.loads(asset["metadata_json"]))
        item = {
            "asset_sha256": asset["asset_sha256"],
            "source_sha256": asset["source_sha256"],
            "active": True,
            "metadata": meta,
        }
        image = media(self.settings, asset["data"])
        if (
            image is None
            or image.sha256 != asset["asset_sha256"]
            or image.mime_type != asset["mime"]
            or meme_assets.source_digest(meta) != asset["source_sha256"]
            or digest(
                {
                    "asset_sha256": asset["asset_sha256"],
                    "source_sha256": asset["source_sha256"],
                    "metadata_sha256": digest(meta),
                }
            )
            != asset["record_sha256"]
        ):
            raise CoreError("MEME_ASSET_UNAVAILABLE", 409)
        original_job = s.one("SELECT event_id FROM jobs WHERE job_id=?", (caption_job_id,))
        if original_job and not reply_revision.source_active(s, original_job["event_id"]):
            raise CoreError("MEME_ASSET_SOURCE_CHANGED", 403)
        caption = self.service._usable(caption_job_id)
        if (
            caption["asset_sha256"] != asset["asset_sha256"]
            or caption["source_sha256"] != asset["source_sha256"]
        ):
            raise CoreError("MEME_ASSET_UNAVAILABLE", 409)
        receipt = s.one(
            "SELECT value FROM runtime_meta WHERE key=?", ("owner_input:" + caption["event_id"],)
        )
        capture, original = owner_input.verify(
            s, self.settings, json.loads(receipt["value"])["capture"]
        )
        custody = {
            "identity": [
                self.settings.owner_qq_id,
                self.settings.bot_qq_id,
                "onebot-direct:FriendMessage:" + self.settings.owner_qq_id,
                capture.protocol_epoch,
            ],
            "capture_public_key": original.policy.capture.public_key,
        }
        if (
            meme_assets.custody_digest(custody) != asset["custody_sha256"]
            or meme_assets.asset_id(custody, item) != asset_id
        ):
            raise CoreError("MEME_ASSET_UNAVAILABLE", 409)
        sources, withdrawn = meme_assets.provenance(
            s, self.settings, custody, item, asset["data"], caption_job_id=caption_job_id
        )
        recorded = {
            (r["event_id"], r["caption_job_id"])
            for r in s.rows("SELECT * FROM meme_asset_sources WHERE asset_id=?", (asset_id,))
        }
        if withdrawn or not sources or not set(sources) <= recorded:
            raise CoreError("MEME_ASSET_SOURCE_CHANGED", 403)
        current = owner_authority.current(s, self.settings, self.clock, purpose="meme")
        for event_id, _ in sources:
            saved = s.one(
                "SELECT value FROM runtime_meta WHERE key=?", ("owner_input:" + event_id,)
            )
            cap, old = owner_input.verify(s, self.settings, json.loads(saved["value"])["capture"])
            lifetime = 86400 * min(
                old.policy.source_retention_days,
                current.policy.source_retention_days,
                self.settings.raw_event_retention_days,
            )
            if (
                not 0
                <= (datetime.fromisoformat(self.clock()) - cap.received_at).total_seconds()
                < lifetime
            ):
                raise CoreError("MEME_ASSET_SOURCE_CHANGED", 403)
        lineage, seen, row = [], set(), caption
        while row:
            if row["job_id"] in seen or len(seen) >= 4096:
                raise CoreError("MEME_ASSET_SOURCE_CHANGED", 403)
            seen.add(row["job_id"])
            lineage.append(row)
            row = self.service._usable(row["reuse_job_id"]) if row["reuse_job_id"] else None
        root = lineage[-1]
        usage = s.one("SELECT * FROM model_usage WHERE request_id=?", (root["usage_request_id"],))
        admission = s.one(
            "SELECT * FROM model_admissions WHERE request_id=?", (root["usage_request_id"],)
        )
        root_job = s.one("SELECT authority_json FROM jobs WHERE job_id=?", (root["job_id"],))
        if (
            not usage
            or usage["state"] != "SETTLED"
            or usage["job_id"] != root["job_id"]
            or not admission
            or admission["authority_json"] != root_job["authority_json"]
            or any(
                r["usage_request_id"] != root["usage_request_id"]
                or digest(json.loads(r["result_json"]))
                != digest(json.loads(caption["result_json"]))
                for r in lineage
            )
        ):
            raise CoreError("MEME_CAPTION_UNSETTLED", 403)
        if not meme_policy.trusted_source(meta, meme_policy.source_rules(self.settings)):
            raise CoreError("MEME_SOURCE_POLICY_CHANGED", 403)
        result = parse(CaptionResult, caption["result_json"])
        if not meme_policy.caption_eligible(result):
            raise CoreError("MEME_ASSET_UNAVAILABLE", 409)
        binding = SourceBinding(
            asset_id=asset_id,
            **{
                k: asset[k]
                for k in ("asset_sha256", "custody_sha256", "source_sha256", "record_sha256")
            },
            caption_job_id=caption_job_id,
            caption_sha256=digest(result.model_dump(mode="json")),
            caption_identity_sha256=digest(json.loads(caption["identity_json"])),
            meme_policy_sha256=meme_service.policy_hash(self.settings),
            privacy_epoch=epoch(s),
        )
        return binding, result, meta, sources, image

    async def prepare(self, job):
        from companion import meme_service

        s, deadline = self.store, time.monotonic() + 2
        # Never suspend while holding the shared Core connection's transaction.
        if s.db.in_transaction:
            raise CoreError("MEME_INDEX_TRANSACTION_ACTIVE", 500)
        facts, vectors, scanned, cache_hits = {}, {}, 0, 0
        fallback = None
        with s.transaction():
            view, event, body = self._scope(job, executing=True)
            if s.one("SELECT 1 FROM runtime_meta WHERE key=?", (OFFER + job["event_id"],)):
                self.check(job)
                return
            query = query_for(body["text"])
            snapshot = (view.policy.capability_policies["meme"], epoch(s), event["reply_revision"],
                        digest(query))
            seen = set()
            if query["intent"]:
                rows = s.rows(
                    "SELECT a.asset_id,a.asset_sha256,src.caption_job_id FROM meme_assets a "
                    "JOIN meme_asset_sources src USING(asset_id) WHERE a.state='STAGED' "
                    "GROUP BY a.asset_id,src.caption_job_id "
                    "ORDER BY a.asset_id,src.caption_job_id LIMIT 4096"
                )
                for row in rows:
                    if time.monotonic() >= deadline:
                        facts, vectors, cache_hits, fallback = {}, {}, 0, "LOCAL_DEADLINE"
                        break
                    scanned += 1
                    sha = row["asset_sha256"]
                    if sha in seen or blocked(s, self.settings, sha, self.clock()):
                        continue
                    try:
                        binding, caption, meta, _, _ = self._resolve(
                            row["asset_id"], row["caption_job_id"]
                        )
                    except CoreError as error:
                        if error.code in {"MEME_ASSET_UNAVAILABLE", "MEME_CAPTION_UNAVAILABLE",
                                          "MEME_SOURCE_POLICY_CHANGED"}:
                            continue
                        raise
                    if meta["category"] not in self.settings.expression_meme_categories:
                        continue
                    # Select the legal source before testing overlap or its own vector cache.
                    seen.add(sha)
                    if not meme_policy.context_allows(caption, query):
                        continue
                    identity = index.CacheIdentity.source(binding)
                    vector = index.cached(s, identity)
                    facts[sha] = (binding, caption_text(caption),
                                  _candidate(sha, caption, meta),
                                  meme_policy.lexical_score(caption, query), identity)
                    if vector is not None:
                        vectors[sha] = vector
                        cache_hits += 1
            if query["intent"] and time.monotonic() >= deadline:
                facts, vectors, cache_hits, fallback = {}, {}, 0, "LOCAL_DEADLINE"
        cold = [sha for sha in facts if sha not in vectors][:index.BATCH]
        query_vector, computed = None, ()
        if facts:
            query_vector, computed, fallback = await self.service.index.infer(
                self.settings.embedding_cache_dir, query["text"],
                tuple(facts[sha][1] for sha in cold), deadline
            )
            if query_vector is not None:
                vectors.update(zip(cold, computed, strict=True))
        proofs = {}
        for sha, (binding, _, _, lexical, identity) in facts.items():
            vector = vectors.get(sha) if query_vector is not None else None
            similarity = index.cosine(query_vector, vector) if vector is not None else None
            score = lexical + (similarity if similarity is not None and similarity >= 0.5 else 0.0)
            if score <= 0:
                continue
            proofs[sha] = index.RankedCandidate(
                version=1,
                asset_id=binding.asset_id, caption_job_id=binding.caption_job_id,
                caption_sha256=binding.caption_sha256,
                cache_key=identity.key if vector is not None else None,
                basis="SEMANTIC" if vector is not None else "LEXICAL",
                reason="VECTOR_AVAILABLE" if vector is not None else
                       "BATCH_LIMIT" if query_vector is not None else fallback,
                lexical_score=lexical, cosine=similarity, score=score,
                vector_sha256=index.vector_sha(vector) if vector is not None else None,
                vector_base64=index.vector_text(vector) if vector is not None else None,
            )
        selected = sorted(proofs, key=lambda sha: (-proofs[sha].score, sha))[:8]
        unindexed = len(facts) - len(vectors) if query_vector is not None else len(facts)
        ranking = index.Ranking(
            version=1,
            header=index.RankingHeader(
                profile=index.PROFILE, profile_sha256=digest(index.PROFILE),
                query_sha256=digest(query),
                mode="LEXICAL" if query_vector is None else "HYBRID" if unindexed else "SEMANTIC",
                query_vector_sha256=index.vector_sha(query_vector) if query_vector else None,
                query_fallback_reason=fallback, scanned=scanned, eligible=len(facts),
                cache_hits=cache_hits, cold_batch=len(cold), unindexed=unindexed,
            ),
            query_vector_base64=index.vector_text(query_vector) if query_vector else None,
            selected={sha: proofs[sha] for sha in selected},
        ).model_dump(mode="json")
        with s.transaction():
            current, event, body = self._scope(job, executing=True)
            if s.one("SELECT 1 FROM runtime_meta WHERE key=?", (OFFER + job["event_id"],)):
                self.check(job)
                return  # A competing first offer is never overwritten.
            if snapshot != (current.policy.capability_policies["meme"], epoch(s),
                            event["reply_revision"], digest(query_for(body["text"]))):
                raise CoreError("MEME_ASSET_SOURCE_CHANGED", 403)
            fresh = cold if computed else []
            bindings = {}
            for sha in dict.fromkeys([*fresh, *selected]):
                binding, caption, meta, roots, _ = self._resolve(
                    facts[sha][0].asset_id, facts[sha][0].caption_job_id
                )
                if (binding != facts[sha][0] or caption_text(caption) != facts[sha][1]
                    or _candidate(sha, caption, meta) != facts[sha][2]
                    or blocked(s, self.settings, sha, self.clock())):
                    raise CoreError("MEME_ASSET_SOURCE_CHANGED", 403)
                if sha in fresh:
                    index.persist(s, facts[sha][4], vectors[sha])
                if sha in selected:
                    proof = ranking["selected"][sha]
                    bindings[sha] = AssetBinding(
                        version=2,
                        **binding.model_dump(),
                        ranking_sha256=index.ranking_sha(ranking["header"], proof),
                    ).model_dump(mode="json")
                    for event_id, _ in roots:
                        s.execute("INSERT OR IGNORE INTO job_sources(job_id,event_id) VALUES (?,?)",
                                  (job["job_id"], event_id))
            # Own chat root is also explicit, including empty/null offers.
            s.execute("INSERT OR IGNORE INTO job_sources(job_id,event_id) VALUES (?,?)",
                      (job["job_id"], job["event_id"]))
            value = {
                "job_id": job["job_id"], "event_id": job["event_id"],
                "reply_revision": event["reply_revision"], "query_sha256": digest(query),
                "offer": MemeOffer(
                    offer_key="meme:" + job["event_id"], intent=query["intent"],
                    candidates=[facts[sha][2] for sha in selected],
                ).model_dump(mode="json") if query["intent"] else None,
                "catalog_owner": "core", "binding_version": 2,
                "meme_policy_sha256": snapshot[0], "privacy_epoch": snapshot[1],
                "catalog_bindings": bindings, "ranking": ranking,
            }
            self._bounded(value)
            s.insert("runtime_meta", key=OFFER + job["event_id"], value=encode(value))
            self.check(job)
            if (query["intent"] and not facts and not selected and fallback is None
                and self.settings.meme_discovery_enabled and self.settings.meme_trusted_sources
                and self.settings.paid_model and self.settings.paid_model.meme_input):
                meme_service.submit_native(s, self.settings, self.clock,
                    meme_service.DiscoveryRequest(event_id=job["event_id"],
                        reply_revision=event["reply_revision"], intent=query["intent"],
                        provider="community"), origin_job=job)

    @staticmethod
    def _bounded(value):
        if (len(encode(value).encode("utf-8")) > index.OFFER_BYTES
            or len(encode(value.get("ranking")).encode("utf-8")) > index.RANKING_BYTES):
            raise CoreError("MEME_OFFER_BINDING_INVALID", 403)

    def check(self, job, *, executing=True):
        from companion import meme_service

        _, _, body = self._scope(job, executing=executing)
        saved = self.store.one(
            "SELECT value FROM runtime_meta WHERE key=?", (OFFER + job["event_id"],)
        )
        try:
            raw = saved["value"] if saved else ""
            # Bound raw bytes before JSON decoding or constructing proof/vector objects.
            if (type(raw) is not str or len(raw) > index.OFFER_BYTES
                or len(raw.encode("utf-8")) > index.OFFER_BYTES):
                raise ValueError("MEME_OFFER_BINDING_INVALID")
            value = strict_json(raw)
            if type(value) is not dict:
                raise ValueError("MEME_OFFER_BINDING_INVALID")
            self._bounded(value)
            frozen = FrozenOffer.model_validate(value)
        except (ValueError, TypeError, ValidationError) as error:
            raise CoreError("MEME_OFFER_BINDING_INVALID", 403) from error
        query = query_for(body["text"])
        offer, ranking = frozen.offer, frozen.ranking
        if (frozen.job_id != job["job_id"] or frozen.event_id != job["event_id"]
            or frozen.privacy_epoch != epoch(self.store)
            or frozen.query_sha256 != digest(query)
            or frozen.meme_policy_sha256 != meme_service.policy_hash(self.settings)
            or ranking.header.query_sha256 != frozen.query_sha256):
            raise CoreError("MEME_OFFER_BINDING_INVALID", 403)
        reply_revision.require_current(self.store, job["event_id"], frozen.reply_revision)
        expected_ids = {c.candidate_id for c in offer.candidates} if offer else set()
        if (set(frozen.catalog_bindings) != expected_ids or set(ranking.selected) != expected_ids
            or len(expected_ids) > ranking.header.eligible
            or (offer is None) != (query["intent"] is None)
            or (offer and (offer.offer_key != "meme:" + job["event_id"]
                           or offer.intent != query["intent"]))):
            raise CoreError("MEME_OFFER_BINDING_INVALID", 403)
        if (not query["intent"] and
            (any(getattr(ranking.header, key) for key in
                 ("scanned", "eligible", "cache_hits", "cold_batch", "unindexed"))
             or ranking.query_vector_base64 is not None
             or ranking.header.query_fallback_reason is not None)):
            raise CoreError("MEME_OFFER_BINDING_INVALID", 403)
        if offer and [c.candidate_id for c in offer.candidates] != sorted(
            expected_ids, key=lambda sha: (-ranking.selected[sha].score, sha)
        ):
            raise CoreError("MEME_OFFER_BINDING_INVALID", 403)
        for sha, binding in frozen.catalog_bindings.items():
            actual, caption, meta, sources, _ = self._resolve(
                binding.asset_id, binding.caption_job_id
            )
            candidate = next(c for c in offer.candidates if c.candidate_id == sha)
            if (actual.model_dump() != binding.model_dump(exclude={"version", "ranking_sha256"})
                or binding.asset_sha256 != sha
                or candidate.model_dump(mode="json") != _candidate(sha, caption, meta)
                or meta["category"] not in self.settings.expression_meme_categories
                or not meme_policy.context_allows(caption, query)
                or binding.ranking_sha256 != index.ranking_sha(
                    value["ranking"]["header"], value["ranking"]["selected"][sha])
                or any(not self.store.one(
                    "SELECT 1 FROM job_sources WHERE job_id=? AND event_id=?",
                    (job["job_id"], source)) for source, _ in sources)):
                raise CoreError("MEME_ASSET_SOURCE_CHANGED", 403)
            try:
                ranking.verify(sha, binding, meme_policy.lexical_score(caption, query))
            except (ValueError, TypeError) as error:
                raise CoreError("MEME_OFFER_BINDING_INVALID", 403) from error
        return value

    def selected(self, job, image, *, executing=False):
        value = self.check(job, executing=executing)
        binding = value["catalog_bindings"].get(image.get("candidate_id"))
        candidate = next(
            (
                c
                for c in (value["offer"] or {}).get("candidates", [])
                if c["candidate_id"] == image.get("candidate_id")
            ),
            None,
        )
        if (
            binding is None
            or candidate is None
            or image.get("offer_key") != "meme:" + job["event_id"]
            or image.get("source") != "companion.meme_catalog"
            or image.get("category") != candidate["category"]
        ):
            raise CoreError("MEME_DELIVERY_BINDING_INVALID", 403)
        return binding
