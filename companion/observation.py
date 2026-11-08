"""Versioned, authenticated observations. External prose never carries execution authority."""

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, Field, model_validator

from companion.contracts import CoreError, StrictModel
from companion.file_evidence import DocumentEvidence
from companion.policy import ConsentService
from companion.privacy import epoch, redact
from companion.source_grants import SourceGrantService, SourceGrantSnapshot
from companion.sources import (
    ObservationSource,
    content_target,
    source_names,
    supports,
    validate_article_image_url,
    validate_reading_evidence_url,
)
from companion.store import after, encode
from companion.transport import digest


class MediaSpan(StrictModel):
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceComment(StrictModel):
    comment_id: str = Field(pattern=r"^[0-9]{1,32}$", strict=True)
    reply_to_id: str | None = Field(default=None, pattern=r"^[0-9]{1,32}$", strict=True)
    floor: int | None = Field(default=None, ge=0, le=2147483647, strict=True)
    author: str = Field(default="", max_length=80, strict=True)
    text: str = Field(default="", max_length=1000, strict=True)
    image_count: int = Field(default=0, ge=0, le=1000, strict=True)
    truncated: bool = Field(default=False, strict=True)


class CommentEvidence(StrictModel):
    items: list[SourceComment] = Field(default_factory=list, max_length=40)
    reported_total: int | None = Field(default=None, ge=0, le=2147483647, strict=True)
    partial: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def unique_comments(self):
        if len({item.comment_id for item in self.items}) != len(self.items):
            raise ValueError("Duplicate source comment")
        if any(item.reply_to_id == item.comment_id for item in self.items):
            raise ValueError("Self-referencing source comment")
        if sum(len(item.text) for item in self.items) > 6000:
            raise ValueError("Comment excerpt exceeds budget")
        if not self.partial and (
            self.reported_total != len(self.items) or any(item.truncated for item in self.items)
        ):
            raise ValueError("Complete comments require matching count and uncut text")
        return self


class ReadingSource(StrictModel):
    role: Literal["metadata", "page", "player", "subtitle", "comments", "danmaku"]
    url: str = Field(max_length=2048)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0, le=1048576)

    @model_validator(mode="after")
    def public_source(self):
        validate_reading_evidence_url(self.url)
        return self


class SubtitleSpan(StrictModel):
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Invalid subtitle span")
        return self


class DanmakuItem(StrictModel):
    at_seconds: float = Field(ge=0, le=36000, allow_inf_nan=False, strict=True)
    text: str = Field(min_length=1, max_length=300, strict=True)


class DanmakuEvidence(StrictModel):
    cid: int = Field(gt=0, le=9223372036854775807, strict=True)
    scope: Literal["current_pool_sample"] = "current_pool_sample"
    items: list[DanmakuItem] = Field(default_factory=list, max_length=120)
    partial: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def excerpt_budget(self):
        if self.partial is not True:
            raise ValueError("Danmaku is only a current-pool sample")
        if sum(len(item.text) for item in self.items) > 6000:
            raise ValueError("Danmaku excerpt exceeds budget")
        return self


class ShortLinkResolution(StrictModel):
    source_url: str = Field(max_length=2048)
    target_url: str = Field(max_length=2048)
    status: Literal[301, 302, 303, 307, 308]
    location_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bounded_video_redirect(self):
        try:
            source, target = content_target(self.source_url), content_target(self.target_url)
        except CoreError as error:
            raise ValueError("Invalid short-link resolution") from error
        if (
            source != ("bilibili", self.source_url)
            or not self.source_url.startswith("https://b23.tv/")
            or target != ("bilibili", self.target_url)
            or not self.target_url.startswith("https://www.bilibili.com/video/")
        ):
            raise ValueError("Invalid short-link resolution")
        return self


class VideoIdentity(StrictModel):
    bvid: str = Field(pattern=r"^BV[A-Za-z0-9]{10}$")
    cid: int = Field(gt=0, strict=True)
    part: Literal[1]
    duration_seconds: int = Field(gt=0, strict=True)


class SubtitleText(SubtitleSpan):
    text: str = Field(min_length=1, max_length=500)
    partial: bool = Field(strict=True)


class ReadingEvidence(StrictModel):
    level: Literal["L1", "L2"]
    method: Literal["public_api", "account_api", "static_page", "browser_render"]
    fetched_at: AwareDatetime
    part: Literal[1] = 1
    subtitle_status: Literal["available", "missing", "restricted", "unavailable", "budget_limited"]
    language: str = Field(default="", max_length=32)
    spans: list[SubtitleSpan] = Field(default_factory=list, max_length=120)
    sources: list[ReadingSource] = Field(min_length=1, max_length=8)
    truncated: bool = False
    comments: CommentEvidence | None = Field(default=None, exclude_if=lambda value: value is None)
    danmaku: DanmakuEvidence | None = Field(default=None, exclude_if=lambda value: value is None)
    discussion_status: dict[
        Literal["comments", "danmaku"],
        Literal["available", "unavailable", "restricted", "budget_limited"],
    ] = Field(default_factory=dict, exclude_if=lambda value: not value)
    resolution: ShortLinkResolution | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    video: VideoIdentity | None = Field(default=None, exclude_if=lambda value: value is None)
    duration_source: Literal["view_first_part"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    subtitles: list[SubtitleText] = Field(
        default_factory=list, max_length=120, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def consistent(self):
        available = self.subtitle_status == "available"
        if (self.video is None) != (self.duration_source is None):
            raise ValueError("Video identity requires its metadata duration source")
        if self.subtitles and (
            not available or sum(len(row.text) for row in self.subtitles) > 6000
        ):
            raise ValueError("Subtitle text requires bounded available subtitle evidence")
        if self.subtitles and any(
            not any(
                span.start_seconds <= row.start_seconds and row.end_seconds <= span.end_seconds
                for span in self.spans
            )
            for row in self.subtitles
        ):
            raise ValueError("Subtitle text must belong to the declared coverage")
        if (self.level == "L2") != available or bool(self.spans) != available:
            raise ValueError("Reading level requires matching subtitle evidence")
        if available and not any(row.role == "subtitle" for row in self.sources):
            raise ValueError("Missing subtitle source")
        for name in ("comments", "danmaku"):
            captured = getattr(self, name) is not None
            if captured and not any(row.role == name for row in self.sources):
                raise ValueError("Missing discussion source")
            if (self.discussion_status.get(name) == "available") != captured:
                raise ValueError("Discussion status does not match evidence")
        return self


class ArticleImage(StrictModel):
    url: str = Field(max_length=2048)
    text_offset: int = Field(ge=0, le=12000)

    @model_validator(mode="after")
    def source_image(self):
        validate_article_image_url(self.url)
        return self


class ArticleEvidence(StrictModel):
    method: Literal["public_html", "source_api"]
    fetched_at: AwareDatetime
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_bytes: int = Field(gt=0, le=1048576)
    images: list[ArticleImage] = Field(default_factory=list, max_length=32)
    truncated: bool = False
    comments: CommentEvidence | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def image_order(self):
        if len({row.url for row in self.images}) != len(self.images):
            raise ValueError("Duplicate article image")
        if [row.text_offset for row in self.images] != sorted(
            row.text_offset for row in self.images
        ):
            raise ValueError("Invalid article image order")
        return self


class ObservationPayload(StrictModel):
    title: str = Field(default="", max_length=300)
    text: str = Field(default="", max_length=12000)
    url: str = Field(default="", max_length=2048)
    requested_url: str = Field(default="", max_length=2048)
    duration_seconds: float | None = Field(default=None, gt=0, le=36000, allow_inf_nan=False)
    analyzed_spans: list[MediaSpan] = Field(default_factory=list, max_length=120)
    media_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    processor_id: str | None = Field(default=None, min_length=1, max_length=128)
    error_code: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{1,64}$")
    reading: ReadingEvidence | None = None
    article: ArticleEvidence | None = None
    document: DocumentEvidence | None = Field(default=None, exclude_if=lambda value: value is None)


class ObservationEvent(StrictModel):
    schema_version: Literal[2]
    event_id: str = Field(min_length=8, max_length=128)
    event_type: Literal["content.observed"]
    source: ObservationSource
    source_kind: Literal[
        "title", "text", "video_analysis", "image_analysis", "audio_transcript", "tool_result"
    ]
    provider_event_id: str = Field(min_length=1, max_length=256)
    occurred_at: AwareDatetime
    trace_id: str = Field(min_length=8, max_length=128)
    privacy_scope: Literal["private"] = "private"
    result_status: Literal["DISCOVERED", "FETCHED", "WATCHED", "FAILED", "UNKNOWN"]
    source_event_ids: list[str] = Field(default_factory=list, max_length=256)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    privacy_epoch: int = Field(ge=0)
    source_grant: SourceGrantSnapshot | None = None
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    consent_id: str | None = Field(default=None, min_length=1, max_length=128)
    raw_media_retention: Literal["discard"] = "discard"
    payload: ObservationPayload


def dependent_events(store, event_ids):
    """Complete transitive closure, also usable while restoring pre-v0.5 databases."""
    result = set(event_ids)
    if store.one(
        "SELECT 1 FROM pg_catalog.pg_class WHERE relnamespace=current_schema()::regnamespa"
        "ce AND relname='event_dependencies' AND relkind IN ('r','p')"
    ):
        pending = list(result)
        while pending:
            for row in store.rows(
                "SELECT event_id FROM event_dependencies WHERE source_event_id = %s",
                (pending.pop(),),
            ):
                if row["event_id"] not in result:
                    result.add(row["event_id"])
                    pending.append(row["event_id"])
    return result


def expired_sources(store, event_ids, cutoff):
    """Check upstream evidence even when the derived observation itself is recent."""
    if not event_ids:
        return []
    lineage = 'SELECT value COLLATE "C" FROM json_array_elements_text(%s::json) AS ids(value)'
    if store.one(
        "SELECT 1 FROM pg_catalog.pg_class WHERE relnamespace=current_schema()::regnamespa"
        "ce AND relname='event_dependencies' AND relkind IN ('r','p')"
    ):
        lineage += (
            " UNION SELECT d.source_event_id FROM event_dependencies d "
            "JOIN sources s ON d.event_id=s.event_id"
        )
    return [
        row["event_id"]
        for row in store.rows(
            "WITH RECURSIVE sources(event_id) AS ("
            + lineage
            + (
                ") SELECT e.event_id FROM events e JOIN sources s "
                "USING(event_id) WHERE e.redacted=1 OR e.ingested_at<%s"
            ),
            (encode(event_ids), cutoff),
        )
    ]


def erase_observations(store, event_ids):
    if not store.one(
        "SELECT 1 FROM pg_catalog.pg_class WHERE "
        "relnamespace=current_schema()::regnamespace AND relname='observations' AND relkind IN "
        "('r','p')"
    ):
        return
    for event_id in event_ids:
        store.execute(
            "UPDATE observations SET source_sha256 = '', canonical_url = '', device_id = NULL, "
            "session_id = NULL, evidence_json = '{}', redacted = 1 WHERE event_id = %s",
            (event_id,),
        )
        store.execute(
            "DELETE FROM event_dependencies WHERE event_id = %s OR source_event_id = %s",
            (event_id, event_id),
        )


class ObservationService:
    def __init__(self, store, settings, clock):
        self.store, self.settings, self.clock = store, settings, clock
        self.consents = ConsentService(store, settings, clock)
        self.grants = SourceGrantService(store, settings, clock)

    def policy(self, adapter_id):
        adapter = self.settings.observation_adapters.get(adapter_id)
        if not self.settings.observation_enabled or adapter is None:
            raise CoreError("OBSERVATION_DISABLED", 403)
        return adapter

    def validate(self, observation, adapter):
        if observation.source != adapter.source:
            raise CoreError("OBSERVATION_SOURCE_MISMATCH", 403)
        if observation.source == "simulation" and self.settings.mode != "demo":
            raise CoreError("SIMULATION_NOT_ALLOWED_IN_LIVE", 403)
        payload = observation.payload
        if hashlib.sha256(payload.text.encode()).hexdigest() != observation.source_sha256:
            raise CoreError("OBSERVATION_DIGEST_MISMATCH", 400)
        source_texts = [payload.title, payload.text, payload.url, payload.requested_url]
        if payload.document is not None:
            source_texts.append(payload.document.filename)
        for evidence in (payload.reading, payload.article):
            if evidence is not None and evidence.comments is not None:
                source_texts.extend(
                    value
                    for comment in evidence.comments.items
                    for value in (comment.text, comment.author)
                )
        if payload.reading is not None and payload.reading.danmaku is not None:
            source_texts.extend(item.text for item in payload.reading.danmaku.items)
        if any(redact(value, self.settings) != value for value in source_texts):
            raise CoreError("OBSERVATION_CONTAINS_CREDENTIALS", 400)
        for value in (payload.url, payload.requested_url):
            if not value:
                continue
            try:
                url = urlsplit(value)
            except ValueError as error:
                raise CoreError("INVALID_OBSERVATION_URL", 400) from error
            if url.scheme != "https" or not url.hostname or url.username or url.fragment:
                raise CoreError("INVALID_OBSERVATION_URL", 400)
            if observation.source in source_names("read"):
                try:
                    source, _ = content_target(
                        value,
                        web_hosts=self.settings.web_reading_hosts
                        if self.settings.web_reading_enabled
                        else (),
                    )
                except CoreError:
                    raise CoreError("INVALID_OBSERVATION_URL", 400) from None
                if source != observation.source:
                    raise CoreError("OBSERVATION_SOURCE_MISMATCH", 403)
        occurred = observation.occurred_at.astimezone(UTC).isoformat(timespec="microseconds")
        if (
            not after(self.clock(), -86400 * self.settings.raw_event_retention_days)
            <= occurred
            <= after(self.clock(), 300)
        ):
            raise CoreError("OBSERVATION_TIME_OUT_OF_RANGE", 400)
        status, kind = observation.result_status, observation.source_kind
        if payload.document is not None:
            try:
                payload.document.validate_text(payload.text)
            except ValueError:
                raise CoreError("DOCUMENT_EVIDENCE_MISMATCH", 400) from None
            no_text = (
                status == "UNKNOWN"
                and not payload.text
                and payload.document.partial
                and payload.error_code == "PRIVATE_FILE_TEXT_UNAVAILABLE"
            )
            if (
                observation.source != "qq_file"
                or (status != "FETCHED" and not no_text)
                or kind != "text"
                or payload.url
                or payload.requested_url
                or payload.reading
                or payload.article
                or payload.document.file_sha256 != payload.media_sha256
                or payload.processor_id != payload.document.parser_version
            ):
                raise CoreError("DOCUMENT_EVIDENCE_MISMATCH", 400)
        elif observation.source == "qq_file":
            raise CoreError("DOCUMENT_EVIDENCE_REQUIRED", 400)
        if payload.article is not None:
            fetched = payload.article.fetched_at.astimezone(UTC).isoformat(timespec="microseconds")
            if (
                not supports(observation.source, "article")
                or status != "FETCHED"
                or kind != "text"
                or any(image.text_offset >= len(payload.text) for image in payload.article.images)
                or not after(self.clock(), -86400 * self.settings.raw_event_retention_days)
                <= fetched
                <= after(self.clock(), 300)
            ):
                raise CoreError("ARTICLE_EVIDENCE_MISMATCH", 400)
        if payload.reading is not None:
            fetched = payload.reading.fetched_at.astimezone(UTC).isoformat(timespec="microseconds")
            if (
                not supports(observation.source, "reading")
                or status != "FETCHED"
                or kind != "text"
                or not after(self.clock(), -86400 * self.settings.raw_event_retention_days)
                <= fetched
                <= after(self.clock(), 300)
            ):
                raise CoreError("READING_EVIDENCE_MISMATCH", 400)
        if status == "FETCHED" and observation.source == "bilibili":
            resolution = payload.reading.resolution if payload.reading else None
            short = payload.url.startswith("https://b23.tv/")
            if short != bool(resolution) or resolution and resolution.source_url != payload.url:
                raise CoreError("READING_RESOLUTION_MISMATCH", 400)
        kinds = {
            "qq_image": {"image_analysis"},
            "qq_file": {"text"},
            "voice": {"audio_transcript"},
            "camera": {"image_analysis", "video_analysis"},
            "android": {"text", "image_analysis"},
            "dsh": {"tool_result"},
            "windows": {"tool_result"},
        }
        if observation.source in kinds and kind not in kinds[observation.source]:
            raise CoreError("OBSERVATION_SOURCE_KIND_MISMATCH", 400)
        if status in {"FAILED", "UNKNOWN"}:
            if payload.text or payload.analyzed_spans:
                raise CoreError("FAILED_OBSERVATION_CANNOT_CLAIM_CONTENT", 400)
        elif not payload.text.strip():
            raise CoreError("OBSERVATION_CONTENT_REQUIRED", 400)
        elif status == "DISCOVERED":
            if kind != "title" or payload.text != payload.title:
                raise CoreError("DISCOVERY_IS_TITLE_ONLY", 400)
        elif status == "FETCHED":
            if kind in {"title", "video_analysis"}:
                raise CoreError("OBSERVATION_LEVEL_MISMATCH", 400)
        elif status == "WATCHED":
            if (
                not adapter.media_analysis
                or kind != "video_analysis"
                or not payload.duration_seconds
            ):
                raise CoreError("MEDIA_ANALYSIS_EVIDENCE_REQUIRED", 400)
            covered = 0.0
            for span in sorted(payload.analyzed_spans, key=lambda item: item.start_seconds):
                if span.start_seconds > covered or span.end_seconds <= span.start_seconds:
                    raise CoreError("MEDIA_ANALYSIS_INCOMPLETE", 400)
                covered = max(covered, span.end_seconds)
            if covered < payload.duration_seconds:
                raise CoreError("MEDIA_ANALYSIS_INCOMPLETE", 400)
        if kind in {"image_analysis", "audio_transcript", "video_analysis"} and status not in {
            "FAILED",
            "UNKNOWN",
        }:
            if not adapter.media_analysis or not payload.media_sha256 or not payload.processor_id:
                raise CoreError("MEDIA_ANALYSIS_EVIDENCE_REQUIRED", 400)
        required = {
            "voice": "audio.transcribe",
            "camera": "camera.observe",
            "android": "android.observe",
        }
        if observation.source in required:
            self.consents.require(
                observation.consent_id,
                required[observation.source],
                observation.device_id,
                observation.session_id,
                occurred,
            )
        if observation.source == "qq_image":
            if len(observation.source_event_ids) != 1:
                raise CoreError("OWNER_SHARED_IMAGE_REQUIRED", 403)
            event = self.store.one(
                "SELECT body_json FROM events WHERE event_id = %s AND source = 'qq.onebot' AND "
                "actor_id = 'owner' AND redacted = 0",
                (observation.source_event_ids[0],),
            )
            if not event or not any(
                item.get("kind") == "image"
                for item in json.loads(event["body_json"]).get("shared_content", [])
            ):
                raise CoreError("OWNER_SHARED_IMAGE_REQUIRED", 403)
        if observation.source == "qq_file":
            if len(observation.source_event_ids) != 1:
                raise CoreError("OWNER_SHARED_FILE_REQUIRED", 403)
            event = self.store.one(
                "SELECT e.body_json FROM events AS e JOIN conversations AS c USING "
                "(conversation_id) WHERE e.event_id = %s AND e.source = 'qq.onebot' AND e.actor_id "
                "= 'owner' AND e.redacted = 0 AND c.scope = 'private' AND c.owner_actor_id = "
                "'owner'",
                (observation.source_event_ids[0],),
            )
            from companion.qq_files import manifest

            document = payload.document
            originals = manifest(json.loads(event["body_json"])) if event else []
            matching = [
                item
                for item in originals
                if (item["file_id"], item["filename"], item["source_sha256"])
                == (document.file_id, document.filename, document.source_sha256)
            ]
            if len(matching) != 1:
                raise CoreError("OWNER_SHARED_FILE_REQUIRED", 403)
            row = self.store.one(
                "SELECT media_sha256, state, captured_privacy_epoch FROM content_items WHERE "
                "owner_event_id = %s AND kind = 'qq_file' AND slot = %s AND redacted = 0",
                (observation.source_event_ids[0], matching[0]["slot"]),
            )
            if (
                not row
                or row["media_sha256"] != document.file_sha256
                or row["state"] != "RUNNING"
                or row["captured_privacy_epoch"] != epoch(self.store)
            ):
                raise CoreError("PRIVATE_FILE_RESULT_FENCED", 409)
        # Tool receipts enter through the executor, added in the tool capability phase.
        if observation.source in {"dsh", "windows"}:
            raise CoreError("TOOL_RECEIPT_REQUIRED", 403)
        return occurred

    def append(self, adapter_id, observation, *, reading=False):
        adapter = self.policy(adapter_id)
        if adapter.source in {"web", "qzone"} and not reading:
            raise CoreError("READER_PROTOCOL_REQUIRED", 409)
        now, s = self.clock(), self.store
        receipt = observation.model_dump(
            mode="json", exclude={"event_id", "trace_id", "privacy_epoch"}
        )
        if receipt["source_grant"] is None:
            receipt.pop("source_grant")  # Preserve pre-upgrade idempotency fingerprints.
        for field in ("reading", "article"):
            if receipt["payload"][field] is None:
                receipt["payload"].pop(field)
        fingerprint = digest(receipt)
        source, external_id = (
            "observation." + adapter.source,
            adapter_id + ":" + observation.provider_event_id,
        )

        def replay():
            previous = s.one(
                "SELECT event_id,source,source_event_id,fingerprint,redacted FROM events "
                "WHERE (source = %s AND source_event_id = %s) OR event_id = %s",
                (source, external_id, observation.event_id),
            )
            if previous:
                if (
                    previous["fingerprint"] != fingerprint
                    or previous["source"] != source
                    or previous["source_event_id"] != external_id
                ):
                    raise CoreError("IDEMPOTENCY_CONFLICT", 409)
                if previous["redacted"]:
                    raise CoreError("SOURCE_ERASED", 410)
                return {"event_id": previous["event_id"], "duplicate": True}
            return None

        with nullcontext() if s.in_transaction else s.transaction():
            if observation.privacy_epoch != epoch(s):
                raise CoreError("PRIVACY_CHANGED", 409)
            occurred = self.validate(observation, adapter)
            account_read = (
                observation.payload.reading and observation.payload.reading.method == "account_api"
            )
            if observation.source_grant or adapter.source_grant_required or account_read:
                self.grants.require(adapter_id, observation.source_grant)
            duplicate = replay()
            if duplicate:
                return duplicate
            for source_id in set(observation.source_event_ids):
                event = s.one(
                    "SELECT events.event_id AS event_id, events.schema_version AS "
                    "schema_version, events.source AS source, events.source_event_id AS "
                    "source_event_id, events.conversation_id AS conversation_id, events.actor_id "
                    "AS actor_id, events.event_type AS event_type, events.occurred_at AS "
                    "occurred_at, events.ingested_at AS ingested_at, events.privacy_scope AS "
                    "privacy_scope, events.body_json AS body_json, events.trace_id AS trace_id, "
                    "events.idempotency_key AS idempotency_key, events.fingerprint AS fingerprint, "
                    "events.redacted AS redacted, events.memory_processed AS memory_processed, "
                    "events.reply_revision AS reply_revision FROM events AS events WHERE "
                    "events.event_id = %s AND events.redacted = 0",
                    (source_id,),
                )
                if (
                    not event
                    or json.loads(event["body_json"]).get("diary_command")
                    or (
                        event["actor_id"] != "owner"
                        and not s.one(
                            "SELECT 1 FROM observations WHERE event_id = %s AND redacted = 0",
                            (source_id,),
                        )
                    )
                ):
                    raise CoreError("INVALID_OBSERVATION_PROVENANCE", 400)
            conversation_id = "conv_internal_observations"
            s.execute(
                "INSERT INTO conversations (conversation_id,platform,platform_session_key,"
                "scope,owner_actor_id,created_at) VALUES (%s,'companion','observations',"
                "'private','owner',%s) ON CONFLICT DO NOTHING",
                (conversation_id, now),
            )
            # Serialize new observation replay keys without blocking FK key-share
            # locks used by other event producers in this existing conversation.
            s.one("SELECT conversation_id FROM conversations WHERE conversation_id=%s "
                  "FOR NO KEY UPDATE", (conversation_id,))
            duplicate = replay()
            if duplicate:
                return duplicate
            s.insert(
                "events",
                event_id=observation.event_id,
                schema_version=1,
                source=source,
                source_event_id=external_id,
                conversation_id=conversation_id,
                actor_id="companion",
                event_type="content.observed",
                occurred_at=occurred,
                ingested_at=now,
                privacy_scope="private",
                body_json=encode(
                    {
                        "text": observation.payload.text,
                        "title": observation.payload.title,
                        "result_status": observation.result_status,
                    }
                ),
                trace_id=observation.trace_id,
                fingerprint=fingerprint,
                memory_processed=1,
            )
            s.insert(
                "observations",
                event_id=observation.event_id,
                adapter_id=adapter_id,
                source_kind=observation.source_kind,
                result_status=observation.result_status,
                source_sha256=observation.source_sha256,
                canonical_url=observation.payload.url,
                device_id=observation.device_id,
                session_id=observation.session_id,
                consent_id=observation.consent_id,
                evidence_json=encode(
                    observation.payload.model_dump(mode="json", exclude={"text", "title", "url"})
                ),
                captured_privacy_epoch=observation.privacy_epoch,
                created_at=now,
            )
            for source_id in set(observation.source_event_ids):
                s.insert(
                    "event_dependencies", event_id=observation.event_id, source_event_id=source_id
                )
            if observation.source_grant:
                s.insert(
                    "source_grant_events",
                    event_id=observation.event_id,
                    grant_id=observation.source_grant.grant_id,
                    grant_epoch=observation.source_grant.epoch,
                )
            s.audit(
                "observation.accepted",
                observation.event_id,
                observation.trace_id,
                now,
                status=observation.result_status,
            )
        return {"event_id": observation.event_id, "duplicate": False}

    def recent(self, limit=20):
        return self.store.rows(
            "SELECT o.event_id, o.result_status, o.source_kind, o.canonical_url, e.source, "
            "e.body_json FROM observations AS o JOIN events AS e USING (event_id) WHERE o.redacted "
            "= 0 AND e.redacted = 0 ORDER BY e.record_sequence DESC NULLS LAST LIMIT %s",
            (limit,),
        )
