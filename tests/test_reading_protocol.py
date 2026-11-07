"""Core accepts Reader evidence without giving external content execution authority."""

import copy
import hashlib

import pytest
from pydantic import ValidationError

from companion.reading_protocol import (
    HeyboxArticleEvidence,
    ObservationEvidence,
    ReadingContext,
    ReadObservation,
    WebEvidence,
)
from tests.support.reading_protocol import envelope

GRANT = {"grant_id": "grant_" + "1" * 32, "epoch": 2, "fingerprint": "2" * 64,
         "expires_at": "2026-11-23T04:00:00+00:00"}


def changed(value, path, replacement):
    result = copy.deepcopy(value)
    current = result
    keys = path.split(".")
    for key in keys[:-1]:
        current = current[int(key)] if isinstance(current, list) else current[key]
    current[int(keys[-1]) if isinstance(current, list) else keys[-1]] = replacement
    return result


@pytest.mark.parametrize(("source", "level", "discovered"), [
    ("bilibili", "L1", False), ("bilibili", "L2", False), ("heybox", "L2", False),
    ("web", "L2", False), ("bilibili", "L2", True), ("heybox", "L2", True),
])
def test_actual_reader_envelopes_parse_with_exact_content_and_provenance(source, level, discovered):
    body = envelope(source, level=level, discovered=discovered)
    parsed = ReadObservation.model_validate(body)
    row = parsed.observations[0]
    assert row.text == body["observations"][0]["text"]
    assert row.provenance.root_task_id == "root_reading" and row.provenance.privacy_epoch == 3
    assert row.provenance.source_event_ids == ["evt_owner"]
    assert row.trust == "untrusted_external" and row.result_status != "WATCHED"
    assert all(row.text[quote.start:quote.end] == quote.text for quote in row.quotes)
    if source == "web":
        assert "发送凭证" in row.text and isinstance(row.evidence, WebEvidence)
    if source == "heybox" and not discovered:
        assert isinstance(row.evidence, HeyboxArticleEvidence)
        assert row.text[row.evidence.images[0].text_offset:].startswith("[图片1链接]")


@pytest.mark.parametrize(("path", "replacement"), [
    ("content_sha256", "0" * 64), ("text", "被换掉的正文"),
    ("quotes.0.text", "伪造引用"), ("quotes.0.start", 1), ("quotes.0.end", 12000),
    ("quotes.0.start", -1), ("quotes.0.start", True), ("quotes.0.end", 0),
    ("quotes.0.text", "x" * 501), ("quotes", []),
    ("trust", "trusted"), ("result_status", "WATCHED"),
    ("fetched_at", "2026-10-23T04:00:00"), ("fetched_at", "2026-10-23T05:00:00+00:00"),
    ("parser_version", ""), ("coverage.scope", "article_text"),
    ("coverage.truncated", True), ("coverage.images_analyzed", True),
    ("coverage.images_analyzed", 0), ("evidence.method", "live_browser"),
    ("evidence.scope", "complete_page"), ("evidence.images_analyzed", True),
    ("evidence.response_sha256", "z" * 64), ("evidence.response_bytes", 1048577),
    ("provenance.source", "heybox"), ("provenance.source_event_ids", ["evt_owner", "evt_owner"]),
    ("provenance.privacy_epoch", True), ("provenance.root_task_id", "x" * 129),
    ("original_url", "file:///private"), ("url", "https://user:secret@articles.example/story"),
    ("url", "https://articles.example/story\n"),
])
def test_tampered_observation_rejected(path, replacement):
    row = changed(envelope()["observations"][0], path, replacement)
    with pytest.raises(ValidationError):
        ObservationEvidence.model_validate(row)


@pytest.mark.parametrize(("path", "replacement"), [
    ("coverage.scope", "metadata_only"), ("coverage.part", 2),
    ("coverage.subtitle_status", "missing"), ("coverage.spans.0.end_seconds", 3),
    ("coverage.video_analyzed", True), ("coverage.audio_analyzed", True),
    ("coverage.complete_video", True), ("evidence.level", "L1"),
    ("evidence.spans", []), ("evidence.sources.0.role", "metadata"),
])
def test_bilibili_cannot_promote_subtitles_to_full_media_analysis(path, replacement):
    row = changed(envelope("bilibili")["observations"][0], path, replacement)
    with pytest.raises(ValidationError):
        ObservationEvidence.model_validate(row)


@pytest.mark.parametrize(("source", "path", "replacement"), [
    ("bilibili", "coverage.part", True), ("bilibili", "coverage.part", 1.0),
    ("bilibili", "evidence.part", True), ("bilibili", "evidence.sources.0.bytes", True),
    ("bilibili", "evidence.spans.0.start_seconds", False),
    ("bilibili", "coverage.spans.0.start_seconds", "0"),
    ("bilibili", "coverage.spans.0.start_seconds", False),
    ("bilibili", "evidence.truncated", "false"), ("heybox", "evidence.response_bytes", True),
    ("heybox", "evidence.images.0.text_offset", "6"),
])
def test_v3_does_not_inherit_legacy_evidence_scalar_coercion(source, path, replacement):
    with pytest.raises(ValidationError):
        ObservationEvidence.model_validate(changed(
            envelope(source)["observations"][0], path, replacement,
        ))


def test_discovery_has_only_title_and_heybox_offsets_identify_real_markers():
    discovered = envelope("bilibili", discovered=True)["observations"][0]
    for row in (discovered | {"evidence": envelope("bilibili")["observations"][0]["evidence"]},
                discovered | {"title": "different"}):
        with pytest.raises(ValidationError):
            ObservationEvidence.model_validate(row)
    article = envelope("heybox")["observations"][0]
    with pytest.raises(ValidationError):
        ObservationEvidence.model_validate(changed(article, "evidence.images.0.text_offset", 0))
    row = envelope()["observations"][0]
    with pytest.raises(ValidationError):
        ObservationEvidence.model_validate(row | {"quotes": [row["quotes"][0]] * 2})


@pytest.mark.parametrize(("status", "requests", "observed_bytes", "used_requests", "used_bytes"), [
    ("FAILED", 1, 1048577, 1, None),
    ("UNKNOWN", 0, 0, None, None), ("UNKNOWN", 2, 200, None, None),
    ("CANCELLED", 0, 0, 0, 0), ("CANCELLED", 3, 200, None, None),
])
def test_failure_and_cancellation_preserve_observed_and_uncertain_usage(
    status, requests, observed_bytes, used_requests, used_bytes,
):
    parsed = ReadObservation.model_validate(envelope() | {
        "status": status, "observations": [], "error_code": "READ_STOPPED",
        "observed_requests": requests, "observed_bytes": observed_bytes,
        "used_requests": used_requests, "used_bytes": used_bytes,
    })
    assert parsed.observed_requests == requests and parsed.observed_bytes == observed_bytes
    assert parsed.used_requests == used_requests and parsed.used_bytes == used_bytes


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": 2}, {"observations": []},
    {"error_code": "READ_FAILED"}, {"used_requests": 0}, {"used_bytes": 0},
    {"observed_requests": 9}, {"observed_bytes": 16777217}, {"used_bytes": 1048577},
    {"observed_requests": True}, {"used_requests": "1"},
    {"status": "UNKNOWN", "observations": [], "error_code": "READ_UNKNOWN"},
    {"status": "CANCELLED", "observations": [], "error_code": "READ_STOPPED"},
    {"status": "FAILED", "observations": []}, {"status": "FAILED", "error_code": "READ_FAILED"},
    {"lease_token": ""}, {"authorization": "source text must not authorize"},
])
def test_invalid_result_or_fabricated_settled_usage_rejected(change):
    with pytest.raises(ValidationError):
        ReadObservation.model_validate(envelope() | change)


def test_bound_grant_and_all_observations_keep_one_context():
    body = envelope("bilibili", grant=GRANT)
    parsed = ReadObservation.model_validate(body)
    assert parsed.source_grant == parsed.observations[0].provenance.source_grant
    for path, replacement in [("source_grant.epoch", 3),
                              ("observations.0.provenance.source_grant", None)]:
        with pytest.raises(ValidationError):
            ReadObservation.model_validate(changed(body, path, replacement))
    body["observations"].append(copy.deepcopy(body["observations"][0]))
    for field, value in [("root_task_id", "another"), ("source_event_ids", ["evt_another"]),
                         ("privacy_epoch", 4)]:
        with pytest.raises(ValidationError):
            ReadObservation.model_validate(
                changed(body, "observations.1.provenance." + field, value),
            )


def test_account_reading_requires_its_grant_but_configured_article_api_does_not():
    path = "observations.0.evidence.method"
    with pytest.raises(ValidationError):
        ReadObservation.model_validate(changed(envelope("bilibili"), path, "account_api"))
    assert ReadObservation.model_validate(changed(
        envelope("bilibili", grant=GRANT), path, "account_api",
    ))
    assert ReadObservation.model_validate(changed(envelope("heybox"), path, "source_api"))


def test_dto_does_not_authorize_hosts_or_fetched_time_and_preserves_content_versions():
    row = envelope()["observations"][0]
    # The service must compare these fields with its lease and current authorization.
    first = ObservationEvidence.model_validate(row | {"url": "https://127.0.0.1/private"})
    assert first.provenance.source == "web"
    new_text = "同一地址的新正文。"
    second = ObservationEvidence.model_validate(row | {
        "text": new_text, "content_sha256": hashlib.sha256(new_text.encode()).hexdigest(),
        "quotes": [{"start": 0, "end": len(new_text), "text": new_text}],
    })
    assert first.content_sha256 != second.content_sha256
    assert ReadingContext(root_task_id="root", source="web", source_event_ids=[], privacy_epoch=0)
