"""Native caption jobs and current-turn offers over the existing private HTTP boundary."""

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlsplit

from pydantic import Field

from companion import (
    budget,
    meme_service,
    owner_authority,
    owner_input,
    private_life,
    reply_revision,
)
from companion.contracts import CoreError, StrictModel
from companion.expression import MemeOffer, query_intent, query_target
from companion.privacy import epoch
from companion.retrieval import INDEX_VERSION, encoder
from companion.store import after, encode, uid, utcnow
from companion_meme_protocol import (
    CAPTION_INSTRUCTIONS,
    OFFER_WAIT_SECONDS,
    CaptionAsset,
    CaptionContext,
    CaptionIdentity,
    CaptionOutcome,
    CaptionRequest,
    CaptionResult,
    EmbeddingBatch,
    EmbeddingSpec,
    UsageReceipt,
    caption_cache_key,
    caption_model_schema,
    caption_text,
    digest,
    parse,
    sample_frames,
    strict_json,
    validate_bytes,
)

QUERY = "meme_query:"
OFFER = "meme_offer:"


def offer_possible(text):
    """Text without a meme intent gets a null offer at best, so chat need not wait."""
    return query_intent(text[-2000:]) is not None


SPEC = EmbeddingSpec(model_version=INDEX_VERSION, dimensions=512)
MAX_ROWS, MAX_BYTES = 2048, 64 * 1024 * 1024


class AssetBody(StrictModel):
    data_base64: str = Field(min_length=1, max_length=5592408)
    asset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mime: str = Field(pattern=r"^image/(png|jpeg|gif)$")
    byte_length: int = Field(strict=True, ge=1, le=4 * 1024 * 1024)


class CaptionBody(StrictModel):
    asset: AssetBody
    context: CaptionContext
    identity: CaptionIdentity
    source_evidence: dict


class QueryBody(StrictModel):
    event_id: str = Field(pattern=r"^evt_[a-f0-9]{64}$")
    reply_revision: int = Field(strict=True, ge=1)


class EmbedBody(QueryBody):
    job_id: str = Field(pattern=r"^job_[a-f0-9]{32}$")


class OfferBody(EmbedBody):
    query_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    offer: MemeOffer | None


def identity(settings):
    return CaptionIdentity(model_version=settings.provider + "/" + settings.model_name)


def scope(store, settings, clock, *, current_request=None):
    if not settings.meme_semantics_enabled:
        raise CoreError("MEME_SEMANTICS_DISABLED", 403)
    return owner_authority.current(store, settings, clock, purpose="meme")


def current_source(store, settings, approval, event_id, revision, clock):
    reply_revision.require_current(store, event_id, revision)
    return owner_input.source(store, settings, approval, event_id, clock(), purpose="meme")


def _public_url(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or "\\" in value
        or any(ord(c) <= 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError()
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.fragment
        or url.port not in (None, 443)
    ):
        raise ValueError()
    return url


def _fetch_proof(fetch):
    if (
        set(fetch) != {"requested_url", "final_url", "hops", "body_sha256", "fetched_at"}
        or not re.fullmatch(r"[a-f0-9]{64}", fetch["body_sha256"])
        or not isinstance(fetch["hops"], list)
        or not 1 <= len(fetch["hops"]) <= 6
        or datetime.fromisoformat(fetch["fetched_at"]).utcoffset() is None
    ):
        raise ValueError()
    _public_url(fetch["requested_url"])
    _public_url(fetch["final_url"])
    for hop in fetch["hops"]:
        _public_url(hop["url"])
        if (
            set(hop) != {"url", "address", "status"}
            or not ipaddress.ip_address(hop["address"]).is_global
            or type(hop["status"]) is not int
            or hop["status"] not in {200, 301, 302, 303, 307, 308}
        ):
            raise ValueError()
    if (
        fetch["hops"][0]["url"] != fetch["requested_url"]
        or fetch["hops"][-1]["url"] != fetch["final_url"]
        or fetch["hops"][-1]["status"] != 200
    ):
        raise ValueError()


def _repository_path(value):
    if (
        not isinstance(value, str) or not 1 <= len(value) <= 512
        or "\\" in value or ":" in value or ".." in value
        or any(ord(c) < 32 for c in value)
        or any(part in {"", "."} or part.endswith((" ", ".")) for part in value.split("/"))
    ):
        raise ValueError()
    return value


def _source_files_proof(value, acquired):
    files = acquired["source_files"]
    page = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/tree/([a-f0-9]{40})",
        value["source_page"],
    )
    license_proof = re.search(
        r"; license-file=([^;]+); license-sha256=([a-f0-9]{64})$", value["usage_basis"],
    )
    if (
        not isinstance(files, dict) or set(files) != {"manifest", "license", "listing"}
        or not page or page[2] != value["revision"] or not license_proof
        or any(part in {".", ".."} for part in page[1].split("/"))
        or acquired["archive_member"] != ""
    ):
        raise ValueError()
    repo, revision = page[1], page[2]
    prefix = f"https://raw.githubusercontent.com/{repo}/{revision}/"
    asset_url = value["asset_url"]
    if not asset_url.startswith(prefix):
        raise ValueError()
    path = _repository_path(unquote(asset_url[len(prefix):]))
    parts = path.split("/")
    if (
        len(parts) < 3 or parts[-3] != "memes"
        or asset_url != prefix + quote(path)
        or acquired["discovered_asset_url"] != asset_url
    ):
        raise ValueError()
    root = "/".join(parts[:-3])
    manifest = (root + "/" if root else "") + "manifest.json"
    license_path = _repository_path(license_proof[1])
    expected = {
        "manifest": prefix + quote(manifest),
        "license": prefix + quote(license_path),
        "listing": f"https://api.github.com/repos/{repo}/contents/"
                   + quote(path.rsplit("/", 1)[0]) + "?ref=" + revision,
    }
    for role, fetch in {"image": acquired["fetch"], **files}.items():
        _fetch_proof(fetch)
        url = asset_url if role == "image" else expected[role]
        if (
            fetch["requested_url"] != url or fetch["final_url"] != url
            or len(fetch["hops"]) != 1
        ):
            raise ValueError()
    if files["license"]["body_sha256"] != license_proof[2]:
        raise ValueError()


def _proof(value, asset_sha, source_sha):
    """Validate the authenticated Bridge's completed acquisition/reviewer witness.

    The Bridge owns trusted repository/license rules; neither captions nor search
    metadata can issue this witness. Core verifies its immutable byte bindings.
    """
    try:
        if (
            len(encode(value).encode()) > 32768
            or set(value)
            != {
                "schema_version",
                "source_page",
                "asset_url",
                "license",
                "usage_basis",
                "fetched_at",
                "revision",
                "acquisition",
            }
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or digest(value) != source_sha
        ):
            raise ValueError()
        for key, limit in (("license", 128), ("usage_basis", 512), ("revision", 160)):
            if not isinstance(value[key], str) or not 1 <= len(value[key].strip()) <= limit:
                raise ValueError()
        if value["license"].casefold() in {"unknown", "unlicensed", "none"}:
            raise ValueError()
        _public_url(value["source_page"])
        _public_url(value["asset_url"])
        acquired = value["acquisition"]
        if (
            set(acquired) - {"source_files", "gallery_selection"}
            != {
                "source_page",
                "discovered_asset_url",
                "revision",
                "fetch",
                "image_sha256",
                "archive_member",
            }
            or acquired["image_sha256"] != asset_sha
            or acquired["source_page"] != value["source_page"]
            or acquired["revision"] != value["revision"]
        ):
            raise ValueError()
        _public_url(acquired["discovered_asset_url"])
        fetch = acquired["fetch"]
        _fetch_proof(fetch)
        if (
            fetch["requested_url"] != value["asset_url"]
            or fetch["fetched_at"] != value["fetched_at"]
        ):
            raise ValueError()
        member = acquired["archive_member"]
        if member:
            if (
                not isinstance(member, str)
                or len(member) > 1024
                or "\\" in member
                or member.startswith("/")
                or any(x in {"", ".", ".."} for x in member.split("/"))
                or not re.fullmatch(r"[a-f0-9]{40}", value["revision"])
                or "; license-file=" not in value["usage_basis"]
                or not re.search(r"; license-sha256=[a-f0-9]{64}$", value["usage_basis"])
            ):
                raise ValueError()
        elif fetch["body_sha256"] != asset_sha:
            raise ValueError()
        if "source_files" in acquired:
            _source_files_proof(value, acquired)
        if "gallery_selection" in acquired:
            from companion_meme_sources.gallery import proof

            _fetch_proof(acquired["gallery_selection"]["terms"])
            proof(value, acquired)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise CoreError("MEME_SOURCE_EVIDENCE_INVALID", 403) from error


def _parent(store, settings, clock, context):
    parent = store.one(
        "SELECT * FROM jobs WHERE job_id=? AND kind='meme_discovery'", (context.discovery_job_id,)
    )
    reservation = store.one(
        "SELECT * FROM meme_discovery_reservations WHERE job_id=?", (context.discovery_job_id,)
    )
    value = meme_service._metadata(store, context.discovery_job_id)
    if (
        not parent
        or parent["state"] != "SUCCEEDED"
        or not reservation
        or reservation["state"] != "SETTLED"
        or reservation["attempt_key"] != context.attempt_key
        or value["event_id"] != context.event_id
        or value["reply_revision"] != context.reply_revision
    ):
        raise CoreError("MEME_CAPTION_DISCOVERY_REQUIRED", 403)
    owner_authority.check_job(store, settings, clock, parent, purpose="meme")


def check_caption_job(store, settings, approval, job, *, clock=utcnow):
    owner_authority.check_job(store, settings, clock, job, purpose="meme")
    row = store.one("SELECT * FROM meme_captions WHERE job_id=?", (job["job_id"],))
    if (
        not row
        or job["kind"] != "meme_caption"
        or not row["asset_data"]
        or row["privacy_epoch"] != epoch(store)
        or parse(CaptionIdentity, row["identity_json"]) != identity(settings)
        or row["cache_key"] != caption_cache_key(row["asset_sha256"], identity(settings))
    ):
        raise CoreError("MEME_CAPTION_SOURCE_CHANGED", 403)
    context = parse(CaptionContext, row["context_json"])
    event, _, _ = current_source(
        store, settings, approval, context.event_id, context.reply_revision, clock
    )
    if job["event_id"] != context.event_id:
        raise CoreError("MEME_CAPTION_SOURCE_CHANGED", 403)
    _parent(store, settings, clock, context)
    _proof(strict_json(row["source_json"]), row["asset_sha256"], row["source_sha256"])
    if hashlib.sha256(row["asset_data"]).hexdigest() != row["asset_sha256"]:
        raise CoreError("MEME_CAPTION_SOURCE_CHANGED", 403)
    return event


def erase_captions(store, event_ids):
    if not event_ids:
        return
    for job in store.rows(
        "SELECT DISTINCT j.event_id FROM jobs j JOIN job_sources s USING(job_id) "
        "WHERE s.event_id IN (SELECT value FROM json_each(?))",
        (encode(sorted(event_ids)),),
    ):
        store.execute("DELETE FROM runtime_meta WHERE key=?", (OFFER + job["event_id"],))
    rows = store.rows(
        "WITH RECURSIVE erased(job_id) AS (SELECT job_id FROM jobs WHERE event_id IN "
        "(SELECT value FROM json_each(?)) UNION SELECT c.job_id FROM meme_captions c "
        "JOIN erased e ON c.reuse_job_id=e.job_id) SELECT job_id FROM erased",
        (encode(sorted(event_ids)),),
    )
    for row in rows:
        store.execute(
            "UPDATE meme_captions SET asset_data=NULL,result_json=NULL,context_json='{}',"
            "source_json='{}',request_json='{}' WHERE job_id=?",
            (row["job_id"],),
        )
        store.execute(
            "UPDATE jobs SET state='CANCELLED',lease_token=NULL,"
            "last_error_code='MEME_CAPTION_SOURCE_ERASED' WHERE job_id=? "
            "AND kind='meme_caption'",
            (row["job_id"],),
        )
    for event_id in event_ids:
        store.execute(
            "DELETE FROM runtime_meta WHERE key IN (?,?)", (QUERY + event_id, OFFER + event_id)
        )


class MemeCaptionService:
    def __init__(self, store, settings, model, clock):
        from companion.meme_index import MemeIndex

        self.store, self.settings, self.model, self.clock = store, settings, model, clock
        self.index = MemeIndex()
        from companion.meme_discovery import MemeDiscovery

        self.discovery = MemeDiscovery(self)

    def profile(self):
        scope(self.store, self.settings, self.clock)
        return {
            "identity": identity(self.settings).model_dump(mode="json"),
            "embedding": SPEC.model_dump(mode="json"),
        }

    def submit(self, body):
        self._bridge_catalog()
        approval = scope(self.store, self.settings, self.clock)
        data, request = self._request(body)
        with self.store.transaction():
            return self._enqueue(body, data, request, approval)

    def _request(self, body):
        if body.identity != identity(self.settings):
            raise CoreError("MEME_CAPTION_PROFILE_CHANGED", 409)
        try:
            data = base64.b64decode(body.asset.data_base64, validate=True)
            media = validate_bytes(data)
            if (
                not media
                or media.sha256 != body.asset.asset_sha256
                or media.mime_type != body.asset.mime
                or len(data) != body.asset.byte_length
                or base64.b64encode(data).decode() != body.asset.data_base64
            ):
                raise ValueError()
        except (ValueError, binascii.Error) as error:
            raise CoreError("MEME_ASSET_INVALID", 400) from error
        request = CaptionRequest(
            CaptionAsset(
                data,
                body.asset.asset_sha256,
                body.asset.source_evidence_sha256,
                body.asset.mime,
                body.asset.byte_length,
            ),
            body.context,
            body.identity,
        )
        _proof(
            body.source_evidence, request.asset.asset_sha256, request.asset.source_evidence_sha256
        )
        return data, request

    def enqueue_native(self, body, custody):
        from companion import meme_assets
        from companion.meme_retrieval import media

        if not self.store.db.in_transaction:
            raise RuntimeError("MEME_CAPTION_ENQUEUE_REQUIRES_TRANSACTION")
        approval = meme_service._native_scope(self.store, self.settings, self.clock)
        data, request = self._request(body)
        if (media(self.settings, data) is None
            or meme_assets.original_custody(self.store, self.settings,
                                            body.context.event_id) != custody):
            raise CoreError("MEME_ASSET_SOURCE_CHANGED", 403)
        return self._enqueue(body, data, request, approval, custody=custody)

    def _enqueue(self, body, data, request, approval, *, custody=None):
        s, now = self.store, self.clock()
        if not s.db.in_transaction:
            raise RuntimeError("MEME_CAPTION_ENQUEUE_REQUIRES_TRANSACTION")
        current_source(
            s, self.settings, approval, body.context.event_id, body.context.reply_revision,
            self.clock,
        )
        _parent(s, self.settings, self.clock, body.context)
        prior = s.one(
            "SELECT job_id FROM meme_captions WHERE request_sha256=?", (request.sha256,)
        )
        if prior:
            if custody is not None:
                self._reuse_compatible(prior["job_id"], custody)
            return self.outcome(prior["job_id"])
        used = s.one(
            "SELECT COUNT(*) AS n,COALESCE(SUM(length(asset_data)),0) AS size "
            "FROM meme_captions"
        )
        if used["n"] >= MAX_ROWS or used["size"] + len(data) > MAX_BYTES:
            raise CoreError("MEME_CAPTION_STORAGE_LIMIT", 429)
        cached = s.rows(
            "SELECT c.* FROM meme_captions c JOIN jobs j USING(job_id) "
            "WHERE cache_key=? "
            + ("AND c.asset_data IS NOT NULL " if custody is None else "")
            + "AND (c.result_json IS NOT NULL OR j.state IN ('QUEUED','RUNNING') "
            "OR EXISTS (SELECT 1 FROM model_usage u WHERE "
            "u.request_id=c.usage_request_id OR u.job_id=c.job_id)) ORDER BY c.rowid",
            (request.cache_key,),
        )
        if custody is not None:
            for candidate in cached:
                self._reuse_compatible(candidate["job_id"], custody)
        prior = None
        for candidate in cached:
            if custody is None and not candidate["result_json"] and not s.one(
                "SELECT 1 FROM model_usage WHERE request_id=? OR job_id=?",
                (candidate["usage_request_id"], candidate["job_id"]),
            ):
                try:
                    check_caption_job(
                        s,
                        self.settings,
                        approval,
                        s.one("SELECT * FROM jobs WHERE job_id=?", (candidate["job_id"],)),
                        clock=self.clock,
                    )
                except CoreError:
                    continue
            prior = candidate
            break
        job_id = uid("job")
        s.insert(
            "jobs",
            job_id=job_id,
            event_id=body.context.event_id,
            kind="meme_caption",
            state="QUEUED",
            idempotency_key="meme_caption:" + request.sha256,
            max_attempts=1,
            context_tracked=1,
            created_at=now,
            updated_at=now,
        )
        owner_authority.bind_job(s, self.settings, self.clock, job_id, purpose="meme")
        s.insert("job_sources", job_id=job_id, event_id=body.context.event_id)
        s.insert(
            "meme_captions",
            job_id=job_id,
            cache_key=request.cache_key,
            request_sha256=request.sha256,
            asset_sha256=request.asset.asset_sha256,
            source_sha256=request.asset.source_evidence_sha256,
            identity_json=body.identity.model_dump_json(),
            context_json=body.context.model_dump_json(),
            source_json=encode(body.source_evidence),
            request_json=encode(
                body.model_dump(mode="json", exclude={"asset": {"data_base64"}})
            ),
            asset_data=data,
            result_json=prior["result_json"] if prior else None,
            usage_request_id=prior["usage_request_id"] if prior else None,
            reuse_job_id=prior["job_id"] if prior else None,
            plan_sha256="",
            privacy_epoch=epoch(s),
            created_at=now,
            updated_at=now,
        )
        if prior and prior["result_json"]:
            s.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?", (job_id,))
        return self.outcome(job_id)

    def _reuse_compatible(self, job_id, custody):
        """Native onboarding cannot bypass a different capture or paid obligation."""
        from companion import meme_assets

        s = self.store
        try:
            original = s.one("SELECT * FROM meme_captions WHERE job_id=?", (job_id,))
            proof = strict_json(original["source_json"])
            meta = {k: proof[k] for k in (
                "source_page", "asset_url", "license", "usage_basis", "fetched_at", "revision"
            )}
            meta["acquisition_json"] = encode(proof["acquisition"])
            item = {"asset_sha256": original["asset_sha256"],
                    "source_sha256": original["source_sha256"], "active": True, "metadata": meta}
            sources, withdrawn = meme_assets.provenance(s, self.settings, custody, item,
                original["asset_data"], caption_job_id=job_id)
            if not sources or withdrawn:
                raise ValueError("MEME_CAPTURE_INCOMPATIBLE")
            lineage, seen, current = [], set(), job_id
            while current:
                if current in seen or len(seen) >= MAX_ROWS:
                    raise ValueError("MEME_ASSET_LINEAGE_INVALID")
                seen.add(current)
                row = s.one("SELECT c.*,j.state,j.authority_json FROM meme_captions c "
                            "JOIN jobs j USING(job_id) WHERE job_id=?", (current,))
                if (not row
                    or parse(CaptionIdentity, row["identity_json"]) != identity(self.settings)
                    or row["cache_key"] != original["cache_key"]):
                    raise ValueError("MEME_CAPTION_PROFILE_CHANGED")
                lineage.append(row)
                current = row["reuse_job_id"]
            root = lineage[-1]
            if any(s.one("SELECT 1 FROM model_usage WHERE job_id=?", (row["job_id"],))
                   for row in lineage[:-1]):
                raise ValueError("MEME_CAPTION_LINEAGE_PAID_CONFLICT")
            usage = s.rows("SELECT * FROM model_usage WHERE job_id=? OR request_id=?",
                           (root["job_id"], root["usage_request_id"]))
            if root["result_json"]:
                admission = s.one("SELECT * FROM model_admissions WHERE request_id=?",
                                  (root["usage_request_id"],))
                if (root["state"] != "SUCCEEDED" or len(usage) != 1
                    or usage[0]["state"] != "SETTLED" or usage[0]["job_id"] != root["job_id"]
                    or usage[0]["request_id"] != root["usage_request_id"] or not admission
                    or admission["authority_json"] != root["authority_json"]):
                    raise ValueError("MEME_CAPTION_UNSETTLED")
                parse(CaptionResult, root["result_json"])
                for row in lineage:
                    if row["result_json"] and (row["usage_request_id"] != root["usage_request_id"]
                        or digest(strict_json(row["result_json"]))
                        != digest(strict_json(root["result_json"]))):
                        raise ValueError("MEME_CAPTION_SOURCE_CHANGED")
            elif (root["state"] not in {"QUEUED", "RUNNING"}
                  or usage and (len(usage) != 1 or usage[0]["state"] != "RESERVED"
                                or root["state"] != "RUNNING"
                                or usage[0]["request_id"] != root["usage_request_id"]
                                or usage[0]["job_id"] != root["job_id"])):
                raise ValueError("MEME_CAPTION_UNSETTLED")
        except (CoreError, ValueError, KeyError, TypeError, IndexError) as error:
            raise CoreError("MEME_CAPTION_CUSTODY_UNAVAILABLE", 409) from error

    def outcome(self, job_id):
        row = self.store.one(
            "SELECT c.*,j.state FROM meme_captions c JOIN jobs j USING(job_id) WHERE job_id=?",
            (job_id,),
        )
        if not row:
            raise CoreError("MEME_CAPTION_JOB_MISSING", 404)
        model_identity = parse(CaptionIdentity, row["identity_json"])
        usage = (
            self.store.one(
                "SELECT * FROM model_usage WHERE request_id=?", (row["usage_request_id"],)
            )
            if row["usage_request_id"]
            else None
        )
        receipt = (
            UsageReceipt(
                usage_request_id=usage["request_id"],
                state=usage["state"],
                input_tokens=usage["input_tokens"] if not usage["is_estimate"] else None,
                output_tokens=usage["output_tokens"] if not usage["is_estimate"] else None,
                billed_micro_usd=usage["billed_micro_usd"] if usage["state"] == "SETTLED" else None,
            )
            if usage
            else None
        )
        state = row["state"]
        if state == "RUNNING" and not usage:
            state = "QUEUED"
        if state == "FAILED" and usage and usage["state"] == "UNKNOWN":
            state = "UNKNOWN"
        result = parse(CaptionResult, row["result_json"]) if state == "SUCCEEDED" else None
        return CaptionOutcome(
            schema_version=1,
            job_id=job_id,
            state=state,
            asset_sha256=row["asset_sha256"],
            source_evidence_sha256=row["source_sha256"],
            request_sha256=row["request_sha256"],
            caption_model_version=model_identity.model_version,
            prompt_version=model_identity.prompt_version,
            result=result,
            usage=receipt,
            cache_hit=bool(row["reuse_job_id"] and result),
        ).model_dump(mode="json")

    def _check(self, job, request_id=None):
        approval = scope(self.store, self.settings, self.clock, current_request=request_id)
        check_caption_job(self.store, self.settings, approval, job, clock=self.clock)
        actual = self.store.one("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],))
        if (
            actual["state"] != "RUNNING"
            or actual["lease_token"] != job["lease_token"]
            or actual["lease_until"] <= self.clock()
        ):
            raise CoreError("MEME_CAPTION_LEASE_EXPIRED", 409)

    def _prior_chain(self, job, prior):
        """Validate the complete old graph before waiting, copying or detaching it."""
        s = self.store
        selected = s.one("SELECT * FROM meme_captions WHERE job_id=?", (job["job_id"],))
        if not prior or not selected:
            raise CoreError("MEME_CAPTION_SOURCE_CHANGED", 403)
        lineage, seen, current = [], set(), prior["job_id"]
        try:
            while current:
                if current in seen or len(seen) >= MAX_ROWS:
                    raise ValueError("MEME_ASSET_LINEAGE_INVALID")
                seen.add(current)
                row = s.one("SELECT c.*,j.state,j.event_id,j.authority_json FROM meme_captions c "
                            "JOIN jobs j USING(job_id) WHERE job_id=? AND j.kind='meme_caption'",
                            (current,))
                if (not row or not row["asset_data"]
                    or row["asset_sha256"] != selected["asset_sha256"]
                    or row["cache_key"] != selected["cache_key"]
                    or parse(CaptionIdentity, row["identity_json"])
                    != parse(CaptionIdentity, selected["identity_json"])
                    or hashlib.sha256(row["asset_data"]).hexdigest() != row["asset_sha256"]):
                    raise ValueError("MEME_CAPTION_SOURCE_CHANGED")
                _proof(strict_json(row["source_json"]), row["asset_sha256"], row["source_sha256"])
                lineage.append(row)
                current = row["reuse_job_id"]
            by_id = {row["job_id"]: row for row in lineage}
            for row in lineage:
                own = s.rows("SELECT request_id FROM model_usage WHERE job_id=?", (row["job_id"],))
                paid = s.one("SELECT job_id FROM model_usage WHERE request_id=?",
                             (row["usage_request_id"],))
                if (len(own) > 1 or own and own[0]["request_id"] != row["usage_request_id"]
                    or paid and paid["job_id"] not in seen
                    or row["result_json"] and not paid):
                    raise ValueError("MEME_CAPTION_USAGE_UNATTRIBUTED")
                if row["result_json"]:
                    parse(CaptionResult, row["result_json"])
                if row["reuse_job_id"]:
                    parent = by_id[row["reuse_job_id"]]
                    if (row["usage_request_id"] and parent["usage_request_id"]
                        and row["usage_request_id"] != parent["usage_request_id"]):
                        raise ValueError("MEME_CAPTION_USAGE_UNATTRIBUTED")
            return lineage
        except (CoreError, ValueError, KeyError, TypeError, StopIteration) as error:
            raise CoreError("MEME_CAPTION_SOURCE_CHANGED", 403) from error

    def _repoint(self, job, prior):
        from companion.meme_assets import extend_reuse_provenance

        s = self.store
        if not s.db.in_transaction:
            raise RuntimeError("MEME_CAPTION_REPOINT_REQUIRES_TRANSACTION")
        lineage = self._prior_chain(job, prior)
        if any(row["result_json"] or s.one(
            "SELECT 1 FROM model_usage WHERE job_id=? OR request_id=?",
            (row["job_id"], row["usage_request_id"]),
        ) for row in lineage):
            raise CoreError("MEME_CAPTION_SOURCE_CHANGED", 403)
        from companion import meme_assets

        original = lineage[0]
        proof = strict_json(original["source_json"])
        meta = {k: proof[k] for k in (
            "source_page", "asset_url", "license", "usage_basis", "fetched_at", "revision"
        )}
        meta["acquisition_json"] = encode(proof["acquisition"])
        try:
            custody = meme_assets.original_custody(s, self.settings, original["event_id"])
            sources, withdrawn = meme_assets.provenance(s, self.settings, custody,
                {"asset_sha256": original["asset_sha256"],
                 "source_sha256": original["source_sha256"], "active": True, "metadata": meta},
                original["asset_data"], caption_job_id=original["job_id"])
            if not sources or withdrawn:
                raise ValueError("MEME_CAPTION_SOURCE_CHANGED")
        except (CoreError, ValueError, KeyError, TypeError) as error:
            raise CoreError("MEME_CAPTION_SOURCE_CHANGED", 403) from error
        affected = {job["job_id"]}
        affected.update(r["job_id"] for r in s.rows(
            "SELECT c.job_id FROM meme_captions c JOIN jobs j USING(job_id) "
            "WHERE c.reuse_job_id=? AND j.state='QUEUED'", (prior["job_id"],),
        ))
        frontier = set(affected)
        while frontier:
            children = {r["job_id"] for r in s.rows(
                "SELECT job_id FROM meme_captions WHERE reuse_job_id IN "
                "(SELECT value FROM json_each(?))", (encode(sorted(frontier)),),
            )}
            if children & affected or len(affected) + len(children) > MAX_ROWS:
                raise CoreError("MEME_ASSET_SOURCE_CHANGED", 403)
            affected.update(children)
            frontier = children
        s.execute("UPDATE meme_captions SET reuse_job_id=NULL,usage_request_id=NULL "
                  "WHERE job_id=?", (job["job_id"],))
        s.execute("UPDATE meme_captions SET reuse_job_id=? WHERE reuse_job_id=? "
                  "AND job_id<>? AND job_id IN (SELECT job_id FROM jobs WHERE state='QUEUED')",
                  (job["job_id"], prior["job_id"], job["job_id"]))
        extend_reuse_provenance(s, self.settings, affected)

    async def run_once(self, worker_id):
        s, now = self.store, self.clock()
        if not self.settings.meme_semantics_enabled:
            return False
        if (getattr(self.settings, "meme_catalog_owner", "bridge") == "core"
            and self.settings.meme_discovery_enabled and self.settings.expression_enabled
            and self.settings.paid_model and self.settings.paid_model.meme_input
            and (self.settings.meme_trusted_sources or self.settings.meme_gallery_source)
            and not s.one("SELECT 1 FROM jobs j JOIN meme_captions c USING(job_id) "
                          "WHERE j.state='QUEUED' LIMIT 1")):
            if await self.discovery.run_once(worker_id):
                return True
        for row in s.rows(
            "SELECT j.job_id,c.usage_request_id FROM jobs j JOIN meme_captions c "
            "USING(job_id) WHERE j.state='RUNNING' AND j.lease_until<=?",
            (now,),
        ):
            if row["usage_request_id"]:
                budget.settle(s, self.settings, row["usage_request_id"], None)
            with s.transaction():
                s.execute(
                    "UPDATE jobs SET state='FAILED',lease_token=NULL,"
                    "last_error_code='MEME_CAPTION_UNKNOWN',updated_at=? WHERE job_id=?",
                    (now, row["job_id"]),
                )
        job = None
        try:
            with s.transaction():
                if (
                    private_life.busy(s, now)
                    or s.one(
                        "SELECT 1 FROM outbox WHERE state IN ('PENDING','LEASED','SENDING') LIMIT 1"
                    )
                ):
                    return False
                job = s.one(
                    "SELECT j.* FROM jobs j JOIN meme_captions c USING(job_id) "
                    "WHERE j.state='QUEUED' "
                    "AND (j.next_attempt_at IS NULL OR j.next_attempt_at<=?) "
                    "ORDER BY j.rowid LIMIT 1", (now,),
                )
                if not job:
                    return False
                try:
                    approval = scope(s, self.settings, self.clock)
                    check_caption_job(s, self.settings, approval, job, clock=self.clock)
                except CoreError as error:
                    s.execute(
                        "UPDATE jobs SET state='CANCELLED',last_error_code=?,updated_at=? "
                        "WHERE job_id=?",
                        (error.code, now, job["job_id"]),
                    )
                    return True
                if private_life.owner_waiting(s, self.settings, now):
                    return False
                row = s.one("SELECT * FROM meme_captions WHERE job_id=?", (job["job_id"],))
                if row["reuse_job_id"]:
                    prior = s.one(
                        "SELECT c.*,j.state FROM meme_captions c JOIN jobs j USING(job_id) "
                        "WHERE job_id=?",
                        (row["reuse_job_id"],),
                    )
                    self._prior_chain(job, prior)
                    if prior["state"] in {"QUEUED", "RUNNING"}:
                        return False
                    paid = s.one(
                        "SELECT request_id FROM model_usage WHERE job_id=? OR request_id=?",
                        (prior["job_id"], prior["usage_request_id"]),
                    )
                    if paid and paid["request_id"] != prior["usage_request_id"]:
                        raise CoreError("MEME_CAPTION_SOURCE_CHANGED", 403)
                    if prior["result_json"] or paid:
                        s.execute(
                            "UPDATE meme_captions SET result_json=?,usage_request_id=?,"
                            "updated_at=? "
                            "WHERE job_id=?",
                            (prior["result_json"], prior["usage_request_id"], now, job["job_id"]),
                        )
                        s.execute(
                            "UPDATE jobs SET state=?,updated_at=? WHERE job_id=?",
                            ("SUCCEEDED" if prior["result_json"] else "FAILED", now, job["job_id"]),
                        )
                        return True
                    # Full current ancestry changes atomically with every affected asset's
                    # attribution. Original paid responsibility cannot authorize takeover.
                    self._repoint(job, prior)
                job["lease_token"] = uid("lease")
                s.execute(
                    "UPDATE jobs SET state='RUNNING',lease_owner=?,lease_token=?,lease_until=?,"
                    "attempts=1,updated_at=? WHERE job_id=?",
                    (
                        worker_id,
                        job["lease_token"],
                        after(now, self.settings.lease_seconds),
                        now,
                        job["job_id"],
                    ),
                )
        except CoreError as error:
            if job is None:
                raise
            # The failed transaction restored both graph and attribution. Cancel only
            # this still-unconsumed request; retain all original bodies and obligations.
            with s.transaction():
                s.execute(
                    "UPDATE jobs SET state='CANCELLED',last_error_code=?,updated_at=? "
                    "WHERE job_id=? AND state='QUEUED' AND NOT EXISTS "
                    "(SELECT 1 FROM model_usage WHERE job_id=jobs.job_id)",
                    (error.code, now, job["job_id"]),
                )
            return True
        request_id = None
        try:
            sampling, frames = sample_frames(
                row["asset_data"], SimpleNamespace(validate_bytes=validate_bytes)
            )
            text = CAPTION_INSTRUCTIONS + "\n输出 JSON schema: " + encode(caption_model_schema())
            text += "\n系统采样范围（只判断已给帧）: " + sampling.model_dump_json()
            parts = [{"type": "text", "text": text}] + [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(frame).decode()
                    },
                }
                for frame in frames
            ]
            messages = [{"role": "user", "content": parts}]
            self._check(job)
            # Bind the durable request before admission commits. A crash between
            # reserve and dispatch must still expose the original paid receipt.
            request_id = uid("model")
            s.execute(
                "UPDATE meme_captions SET usage_request_id=? WHERE job_id=?",
                (request_id, job["job_id"]),
            )
            budget.reserve(
                s,
                self.settings,
                job["job_id"],
                "meme_caption",
                messages,
                self.clock(),
                clock=self.clock,
                request_id=request_id,
            )
            response = await budget.complete(
                self.model(),
                s,
                self.settings,
                request_id,
                messages,
                "meme_caption",
                self.clock,
                source_check=lambda: self._check(job, request_id),
            )
            raw = strict_json(response.text)
            if not isinstance(raw, dict) or "sampled_frames" in raw:
                raise ValueError("MEME_CAPTION_MODEL_INVALID")
            result = parse(
                CaptionResult, {**raw, "sampled_frames": sampling.model_dump(mode="json")}
            )
            with s.transaction():
                self._check(job)
                s.execute(
                    "UPDATE meme_captions SET result_json=?,updated_at=? WHERE job_id=?",
                    (result.model_dump_json(), self.clock(), job["job_id"]),
                )
                s.execute(
                    "UPDATE jobs SET state='SUCCEEDED',lease_token=NULL,updated_at=? "
                    "WHERE job_id=?",
                    (self.clock(), job["job_id"]),
                )
        except Exception as error:
            if request_id:
                budget.settle(s, self.settings, request_id, None)
            code = error.code if isinstance(error, CoreError) else "MEME_CAPTION_INVALID"
            if budget.capacity_wait(s, code, request_id=request_id):
                s.execute(
                    "UPDATE jobs SET state='QUEUED',lease_token=NULL,attempts=0,"
                    "next_attempt_at=?,last_error_code=?,updated_at=? "
                    "WHERE job_id=? AND state='RUNNING' AND lease_token=?",
                    (after(self.clock(), 1), code, self.clock(), job["job_id"], job["lease_token"]),
                )
                return True
            s.execute(
                "UPDATE jobs SET state='FAILED',lease_token=NULL,last_error_code=?,updated_at=? "
                "WHERE job_id=? AND state='RUNNING' AND lease_token=?",
                (code, self.clock(), job["job_id"], job["lease_token"]),
            )
        return True

    def _embedding(self, text, *, query=False):
        model = encoder(str(Path(self.settings.embedding_cache_dir).resolve()), local_only=True)
        vectors = model.query_embed(text) if query else model.passage_embed([text])
        return EmbeddingBatch(
            model_version=SPEC.model_version,
            dimensions=SPEC.dimensions,
            vectors=tuple(tuple(float(n) for n in vector) for vector in vectors),
            local=True,
        )

    async def embed(self, body):
        self._bridge_catalog()
        approval = scope(self.store, self.settings, self.clock)
        current_source(self.store, self.settings, approval, body.event_id, body.reply_revision,
                       self.clock)
        row = self._usable(body.job_id)
        result = await asyncio.to_thread(
            self._embedding, caption_text(parse(CaptionResult, row["result_json"]))
        )
        approval = scope(self.store, self.settings, self.clock)
        current_source(self.store, self.settings, approval, body.event_id, body.reply_revision,
                       self.clock)
        self._usable(body.job_id)
        return result.model_dump(mode="json")

    def _usable(self, job_id):
        row = self.store.one(
            "SELECT c.*,j.event_id,j.state FROM meme_captions c "
            "JOIN jobs j USING(job_id) WHERE job_id=?",
            (job_id,),
        )
        if (
            not row
            or row["state"] != "SUCCEEDED"
            or not row["result_json"]
            or not row["asset_data"]
            or not reply_revision.source_active(self.store, row["event_id"])
            or parse(CaptionIdentity, row["identity_json"]) != identity(self.settings)
            or row["cache_key"] != caption_cache_key(row["asset_sha256"], identity(self.settings))
        ):
            raise CoreError("MEME_CAPTION_UNAVAILABLE", 409)
        _proof(strict_json(row["source_json"]), row["asset_sha256"], row["source_sha256"])
        return row

    async def query(self, body):
        self._bridge_catalog()
        s = self.store
        approval = scope(s, self.settings, self.clock)
        event, value, job = current_source(
            s, self.settings, approval, body.event_id, body.reply_revision, self.clock
        )
        if job["state"] not in {"QUEUED", "RETRY_WAIT"}:
            raise CoreError("MEME_OFFER_TOO_LATE", 409)
        saved = s.one("SELECT value FROM runtime_meta WHERE key=?", (QUERY + body.event_id,))
        if saved:
            return json.loads(saved["value"])
        text = value["text"][-2000:]
        intent = query_intent(text)
        target = query_target(text)
        query = {"text": text, "intent": intent, "target": target}
        result = {
            "job_id": job["job_id"],
            **body.model_dump(),
            "query": query,
            "query_sha256": digest([job["job_id"], body.model_dump(), query]),
            "embedding": None,
        }
        if intent:
            try:
                embedded = await asyncio.to_thread(self._embedding, text, query=True)
                result["embedding"] = embedded.model_dump(mode="json")
            except Exception:
                pass  # Missing local weights retain verified-caption lexical retrieval.
        with s.transaction():
            approval = scope(s, self.settings, self.clock)
            _, _, current = current_source(
                s, self.settings, approval, body.event_id, body.reply_revision, self.clock
            )
            if current["state"] not in {"QUEUED", "RETRY_WAIT"}:
                raise CoreError("MEME_OFFER_TOO_LATE", 409)
            saved = s.one("SELECT value FROM runtime_meta WHERE key=?", (QUERY + body.event_id,))
            if saved:
                return json.loads(saved["value"])
            s.insert("runtime_meta", key=QUERY + body.event_id, value=encode(result))
        return result

    def offer(self, body):
        self._bridge_catalog()
        s = self.store
        with s.transaction():
            approval = scope(s, self.settings, self.clock)
            _, _, job = current_source(
                s, self.settings, approval, body.event_id, body.reply_revision, self.clock
            )
            saved = s.one("SELECT value FROM runtime_meta WHERE key=?", (QUERY + body.event_id,))
            query = json.loads(saved["value"]) if saved else {}
            if (
                body.job_id != job["job_id"]
                or query.get("query_sha256") != body.query_sha256
                or job["state"] not in {"QUEUED", "RETRY_WAIT"}
            ):
                raise CoreError("MEME_OFFER_TOO_LATE", 409)
            value = body.model_dump(mode="json")
            prior = s.one("SELECT value FROM runtime_meta WHERE key=?", (OFFER + body.event_id,))
            if prior and json.loads(prior["value"]) != value:
                raise CoreError("MEME_OFFER_CONFLICT", 409)
            if body.offer:
                if (
                    body.offer.offer_key != "meme:" + body.event_id
                    or body.offer.intent != query["query"]["intent"]
                ):
                    raise CoreError("MEME_OFFER_BINDING_INVALID", 409)
                for candidate in body.offer.candidates:
                    row = s.one(
                        "SELECT job_id FROM meme_captions WHERE asset_sha256=? "
                        "AND result_json IS NOT NULL ORDER BY rowid DESC LIMIT 1",
                        (candidate.candidate_id,),
                    )
                    if not row:
                        raise CoreError("MEME_CAPTION_UNAVAILABLE", 409)
                    for ancestor in s.rows(
                        "WITH RECURSIVE lineage(job_id,reuse_job_id) AS ("
                        "SELECT job_id,reuse_job_id FROM meme_captions WHERE job_id=? UNION "
                        "SELECT c.job_id,c.reuse_job_id FROM meme_captions c JOIN lineage l "
                        "ON c.job_id=l.reuse_job_id) SELECT job_id FROM lineage",
                        (row["job_id"],),
                    ):
                        source = self._usable(ancestor["job_id"])
                        s.execute(
                            "INSERT OR IGNORE INTO job_sources(job_id,event_id) VALUES (?,?)",
                            (job["job_id"], source["event_id"]),
                        )
            if not prior:
                s.insert("runtime_meta", key=OFFER + body.event_id, value=encode(value))
            return {
                "job_id": job["job_id"],
                "event_id": body.event_id,
                "reply_revision": body.reply_revision,
                "accepted": True,
            }

    def waiting_offer(self, job, now):
        if (
            getattr(self.settings, "meme_catalog_owner", "bridge") == "core"
            or
            not self.settings.meme_semantics_enabled
            or job["kind"] != "chat"
        ):
            return False
        try:
            owner_authority.check_job(self.store, self.settings, lambda: now, job, purpose="meme")
        except CoreError:
            return False
        body = json.loads(job["body_json"])
        if body.get("control_command") or body.get("diary_command") or not body.get("text"):
            return False
        if not offer_possible(body["text"]):
            return False
        return now < after(job["created_at"], OFFER_WAIT_SECONDS) and not self.store.one(
            "SELECT 1 FROM runtime_meta WHERE key=?", (OFFER + job["event_id"],)
        )

    def _bridge_catalog(self):
        if getattr(self.settings, "meme_catalog_owner", "bridge") == "core":
            raise CoreError("MEME_CORE_CATALOG_OWNED", 403)

    async def prepare_offer(self, job):
        from companion.meme_retrieval import MemeRetrieval, core_owned

        if (core_owned(self.settings) and job["kind"] == "chat"
            and not reply_revision.control_job(self.store, job)):
            retrieval = MemeRetrieval(self)
            if not retrieval.optional_disabled(job):
                await retrieval.prepare(job)

    def check_offer(self, job):
        from companion.meme_retrieval import MemeRetrieval, core_owned

        if (core_owned(self.settings) and job["kind"] == "chat"
            and not reply_revision.control_job(self.store, job)):
            retrieval = MemeRetrieval(self)
            if not retrieval.optional_disabled(job):
                retrieval.check(job)
