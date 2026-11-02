"""Opaque Core HTTP v1 adapters; model admission and embedding live only in Core."""

import base64
import hashlib
import json
import re
from datetime import datetime

from .images import _media_request
from .meme_caption import (
    CaptionIdentity,
    CaptionOutcome,
    EmbeddingBatch,
    EmbeddingSpec,
    caption_text,
    digest,
    parse,
    source_evidence,
)
from .meme_semantic_search import SemanticQuery
from .network import checked_http_client, private_request_matches, seal_protocol_content
from .onebot_client import bounded_post

BASE = "/internal/v1/memes"
JOB = re.compile(r"job_[a-f0-9]{32}")
SHA = re.compile(r"[a-f0-9]{64}")


async def core_request(runtime, client, path, body, *, check, after=True):
    """Use the original native transport fence and retain its expiry across the await."""
    check()
    authority = dict(runtime.authority)
    expires = None

    def fence():
        check()
        runtime.verify()
        if runtime.authority != authority:
            raise ValueError("OWNER_AUTHORITY_BINDING_CHANGED")
        if expires is not None and runtime.clock() >= expires:
            raise ValueError("OWNER_AUTHORITY_EXPIRED")

    async def authorize():
        nonlocal expires
        fence()
        approval = await runtime.check(client, purpose="meme")
        expires = datetime.fromisoformat(approval["expires_at"]) if approval["expires_at"] else None
        fence()

    result = await _media_request(runtime, client, path, body,
                                 purpose="meme", authorize=authorize)
    if after:
        fence()
    return result


class SemanticHTTP:
    def __init__(self, window, client, *, check):
        self.window, self.client, self.check = window, client, check

    async def post(self, suffix, body, *, after=True):
        return await core_request(self.window, self.client, BASE + suffix, body,
                                  check=self.check, after=after)

    async def profile(self):
        value = await self.post("/semantics/profile", {})
        if not isinstance(value, dict) or set(value) != {"identity", "embedding"}:
            raise ValueError("MEME_SEMANTIC_PROFILE_INVALID")
        if (not isinstance(value["identity"], dict)
            or set(value["identity"]) != {"model_version", "prompt_version"}):
            raise ValueError("MEME_SEMANTIC_PROFILE_INVALID")
        return parse(CaptionIdentity, value["identity"]), parse(EmbeddingSpec, value["embedding"])

    async def query(self, event_id, reply_revision):
        value = await self.post("/query", {"event_id": event_id, "reply_revision": reply_revision})
        if (not isinstance(value, dict) or set(value) != {
            "job_id", "event_id", "reply_revision", "query_sha256", "query", "embedding",
        } or not isinstance(value["job_id"], str) or not JOB.fullmatch(value["job_id"])
            or value["event_id"] != event_id or type(value["reply_revision"]) is not int
            or value["reply_revision"] != reply_revision
            or not isinstance(value["query_sha256"], str)
            or not SHA.fullmatch(value["query_sha256"])):
            raise ValueError("MEME_QUERY_RECEIPT_INVALID")
        query = value["query"]
        if (not isinstance(query, dict) or set(query) != {"text", "intent", "target"}
            or not isinstance(query["text"], str) or not 1 <= len(query["text"]) <= 2000
            or not isinstance(query["target"], str)
            or query["target"] not in {"self", "recipient", "third_party", "neutral", "uncertain"}):
            raise ValueError("MEME_QUERY_RECEIPT_INVALID")
        if query["intent"] is not None:
            parse(SemanticQuery, query)
        if value["embedding"] is not None:
            parse(EmbeddingBatch, value["embedding"])
        return value

    async def offer(self, query, offer):
        binding = {key: query[key] for key in (
            "job_id", "event_id", "reply_revision", "query_sha256",
        )}
        response = await self.post("/offers", {**binding, "offer": offer})
        expected = {key: binding[key] for key in ("job_id", "event_id", "reply_revision")}
        if (not isinstance(response, dict) or set(response) != {*expected, "accepted"}
            or any(response[key] != value for key, value in expected.items())
            or type(response["reply_revision"]) is not int or response["accepted"] is not True):
            raise ValueError("MEME_OFFER_RECEIPT_INVALID")
        return response


class CaptionHTTP:
    def __init__(self, http, catalog):
        self.http, self.catalog = http, catalog

    async def get_responsibility(self, job_id):
        from .meme_responsibility import get_responsibility

        return await get_responsibility(self.http, self.catalog, job_id)

    async def submit(self, request):
        asset = request.asset
        row = self.catalog.db.execute("SELECT data,metadata,active FROM meme_assets WHERE sha=?",
                                      (asset.asset_sha256,)).fetchone()
        if (not row or row[2] != 1 or row[0] != asset.data
            or hashlib.sha256(asset.data).hexdigest() != asset.asset_sha256
            or len(asset.data) != asset.byte_length):
            raise ValueError("MEME_CAPTION_ASSET_CHANGED")
        evidence = source_evidence(self.catalog.metadata(row[1]))
        if digest(evidence) != asset.source_evidence_sha256:
            raise ValueError("MEME_CAPTION_SOURCE_CHANGED")
        body = {
            "asset": {"data_base64": base64.b64encode(asset.data).decode("ascii"),
                      "asset_sha256": asset.asset_sha256,
                      "source_evidence_sha256": asset.source_evidence_sha256,
                      "mime": asset.mime, "byte_length": asset.byte_length},
            "context": request.context.model_dump(mode="json"),
            "identity": request.identity.model_dump(mode="json"),
            "source_evidence": evidence,
        }
        # SemanticLibrary.accept records real usage first, then performs the post-await
        # source/STOP fence before any content commit. A late receipt is not a refund.
        return await self.http.post("/captions", body, after=False)

    async def get(self, job_id):
        if not isinstance(job_id, str) or not JOB.fullmatch(job_id):
            raise ValueError("MEME_CAPTION_JOB_INVALID")
        runtime = self.http.window
        config, authority = runtime.config, dict(runtime.authority)

        def original():
            if runtime.config != config or runtime.authority != authority:
                raise ValueError("MEME_CAPTION_RECEIPT_REQUEST_CHANGED")
            runtime.verify(active=False)
            if not self.catalog.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='meme_caption_bindings'"
            ).fetchone():
                raise ValueError("MEME_CAPTION_JOB_UNBOUND")
            rows = self.catalog.db.execute(
                "SELECT request_sha,sha,source_sha,context_json,outcome_json "
                "FROM meme_caption_bindings WHERE job_id=?", (job_id,)).fetchall()
            if len(rows) != 1 or not rows[0][4]:
                raise ValueError("MEME_CAPTION_JOB_UNBOUND")
            row = tuple(rows[0])
            outcome = parse(CaptionOutcome, row[4])
            context = json.loads(row[3])
            source = self.catalog.db.execute(
                "SELECT capture_json,plan_hash FROM window_inputs WHERE event_id=?",
                (context["event_id"],)).fetchone()
            if (not source or not source[0]
                or digest(json.loads(source[0])["authority"]) != source[1]
                or (outcome.job_id, outcome.request_sha256, outcome.asset_sha256,
                    outcome.source_evidence_sha256) != (job_id, *row[:3])):
                raise ValueError("MEME_CAPTION_JOB_UNBOUND")
            return row, tuple(source), {key: outcome.model_dump(mode="json")[key] for key in (
                "job_id", "asset_sha256", "source_evidence_sha256", "request_sha256",
                "caption_model_version", "prompt_version")}

        frozen = original()
        path = BASE + "/captions/" + job_id

        async def boundary(request):
            if (original() != frozen or request.method != "POST"
                or not private_request_matches(request, config.core_url, path)
                or request.headers.get("Authorization") != "Bearer " + config.bridge_token
                or "Cookie" in request.headers or request.content != b"{}"
                or request.headers.get("Content-Type") != "application/json"):
                raise ValueError("MEME_CAPTION_RECEIPT_REQUEST_CHANGED")
            seal_protocol_content(request, b"{}")

        # Only an already retained job's empty-body result endpoint is read here.
        # Submission, profiles and all other operations keep core_request's gates.
        async with checked_http_client(self.http.client, boundary) as client:
            value = await bounded_post(client, path, {})
        if (original() != frozen or not isinstance(value, dict)
            or any(value.get(key) != expected for key, expected in frozen[2].items())):
            raise ValueError("MEME_CAPTION_IDENTITY_MISMATCH")
        return value


class CaptionEmbeddingHTTP:
    def __init__(self, http, *, job_id, context, caption):
        if not isinstance(job_id, str) or not JOB.fullmatch(job_id):
            raise ValueError("MEME_CAPTION_JOB_INVALID")
        self.http, self.job_id, self.context = http, job_id, context
        self.text = caption_text(caption)

    async def embed(self, texts):
        if tuple(texts) != (self.text,):
            raise ValueError("MEME_EMBEDDING_TEXT_CHANGED")
        # Core loads the successful caption itself; arbitrary caller text is never sent.
        return await self.http.post("/embeddings", {
            "job_id": self.job_id, "event_id": self.context.event_id,
            "reply_revision": self.context.reply_revision,
        })
