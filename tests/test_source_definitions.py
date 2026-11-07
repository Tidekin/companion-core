"""Closed platform policy and accepted-evidence regressions; no external requests."""

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from companion.config import ObservationAdapterConfig
from companion.contracts import CoreError, InboundEvent
from companion.observation import ArticleEvidence, ArticleImage, ReadingSource
from companion.source_grants import SourceGrantRegistration, SourceGrantSnapshot
from companion.sources import (
    ContentSource,
    ObservationSource,
    content_target,
    observation_sources,
    public_image_url,
    source_definition,
    source_names,
    supports,
)
from tests.conftest import run
from tests.support.source_definitions import BILI, HEYBOX, accepted_text, enable_sharing


def test_fixed_definitions_and_protocol_source_types_cannot_enable_third_platform():
    assert source_names("get") == ("bilibili", "heybox", "web", "qzone")
    assert source_names("read") == ("bilibili", "heybox", "web")
    assert source_names("source_grant") == ("bilibili", "heybox", "qzone")
    assert not any(supports("qzone", kind) for kind in ("read", "share", "discover"))
    assert source_names("reading") == ("bilibili",)
    assert source_names("article") == ("heybox",)
    assert observation_sources("share") == ("observation.bilibili", "observation.heybox")
    assert not supports("third-platform", "get")
    assert not supports("simulation", "share")
    assert not supports("web", "source_grant")
    assert not supports("web", "discover") and not supports("web", "share")
    url = "https://articles.example/story"
    with pytest.raises(CoreError, match="CONTENT_URL_NOT_ALLOWED"):
        content_target(url)
    assert content_target(url, web_hosts=("articles.example",)) == ("web", url)
    with pytest.raises(CoreError, match="SOURCE_NOT_ALLOWED"):
        source_definition("third-platform")
    with pytest.raises(FrozenInstanceError):
        source_definition("bilibili").share_label = "untrusted"
    with pytest.raises(AttributeError):
        source_definition("heybox").image_hosts.add("untrusted.example")
    for adapter in (TypeAdapter(ContentSource), TypeAdapter(ObservationSource)):
        with pytest.raises(ValidationError):
            adapter.validate_python("third-platform")
    with pytest.raises(ValidationError):
        ObservationAdapterConfig(source="third-platform", token="synthetic")
    # Existing non-content observation sources and QQ v1 remain separate closed contracts.
    assert TypeAdapter(ObservationSource).validate_python("qq_image") == "qq_image"
    assert InboundEvent.model_json_schema()["properties"]["source"]["const"] == "qq.onebot"


@pytest.mark.parametrize(
    "original,expected",
    [
        (BILI, ("bilibili", BILI)),
        ("https://m.bilibili.com:443/video/BV1xx411c7mD/?p=1", ("bilibili", BILI)),
        ("https://bilibili.com/video/av123/", ("bilibili", "https://www.bilibili.com/video/av123")),
        (
            "https://xiaoheihe.cn/a/ABCDEF123456?tracking=x",
            ("heybox", "https://www.xiaoheihe.cn/app/bbs/link/abcdef123456"),
        ),
        (
            "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=123456&tracking=x",
            ("heybox", HEYBOX),
        ),
    ],
)
def test_canonical_content_identifiers_keep_existing_platform_forms(original, expected):
    assert content_target(original) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://third.example/video/BV1xx411c7mD",
        "https://www.xiaoheihe.cn/video/BV1xx411c7mD",
        "https://www.bilibili.com/app/bbs/link/123456",
        "http://www.bilibili.com/video/BV1xx411c7mD",
        BILI + "#fragment",
        "https://www.bilibili.com:8443/video/BV1xx411c7mD",
        "https://user@www.bilibili.com/video/BV1xx411c7mD",
        "https://:password@www.bilibili.com/video/BV1xx411c7mD",
        "https://www.bilibili.com.evil.example/video/BV1xx411c7mD",
        "https://www.bilibili.com/video/%42V1xx411c7mD",
        "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=123&link_id=456",
        "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=",
        "https://www.bilibili.com/\nvideo/BV1xx411c7mD",
    ],
)
def test_unlisted_cross_platform_or_ambiguous_content_url_is_rejected(url):
    with pytest.raises(CoreError, match="CONTENT_URL_NOT_ALLOWED"):
        content_target(url)


def test_download_normalization_and_persisted_evidence_have_distinct_strict_limits():
    assert public_image_url("http://imgheybox.max-c.com/a.png?resize=1", "heybox") == (
        "https://imgheybox.max-c.com/a.png"
    )
    assert public_image_url("https://i1.hdslb.com/a.png", "bilibili") == (
        "https://i1.hdslb.com/a.png"
    )
    for source, url in (
        ("heybox", "https://i0.hdslb.com/a.png"),
        ("bilibili", "https://imgheybox.max-c.com/a.png"),
        ("unknown", "https://imgheybox.max-c.com/a.png"),
        ("heybox", "https://:password@imgheybox.max-c.com/a.png"),
    ):
        with pytest.raises(CoreError, match="CONTENT_IMAGE_URL_NOT_ALLOWED"):
            public_image_url(url, source)
    for url in (
        "http://imgheybox.max-c.com/a.png",
        "https://imgheybox.max-c.com/a.png?q=1",
        "https://i0.hdslb.com/a.png",
    ):
        with pytest.raises(ValidationError):
            ArticleImage(url=url, text_offset=0)
    for url in (
        "https://i1.hdslb.com/a.json",
        "https://imgheybox.max-c.com/a.json",
        "https://api.bilibili.com/x/web-interface/view?bvid=BV1xx411c7mD",
    ):
        with pytest.raises(ValidationError):
            ReadingSource(role="metadata", url=url, sha256="a" * 64, bytes=10)


def test_observation_rejects_cross_platform_url_and_evidence_before_storage(core):
    event = accepted_text(core)
    event.payload.url = HEYBOX
    with pytest.raises(CoreError, match="SOURCE_MISMATCH"):
        core.observations.append("reader", event)
    event.payload.url = BILI
    event.payload.article = ArticleEvidence(
        method="source_api",
        fetched_at=datetime.fromisoformat(core.clock()),
        response_sha256="a" * 64,
        response_bytes=10,
    )
    with pytest.raises(CoreError, match="ARTICLE_EVIDENCE_MISMATCH"):
        core.observations.append("reader", event)
    assert core.store.rows("SELECT * FROM observations") == []
    assert core.model.calls == []


@pytest.mark.parametrize("source", ["web", "simulation", "qq_image"])
def test_non_platform_adapter_cannot_register_account_grant(core, source):
    core.settings.observation_adapters["reader"] = ObservationAdapterConfig(
        source=source, token="synthetic-reader-token"
    )
    with pytest.raises(CoreError, match="SOURCE_GRANT_ADAPTER_INVALID"):
        core.observations.grants.register(
            SourceGrantRegistration(
                adapter_id="reader",
                grant_id="grant_" + "a" * 32,
                epoch=1,
                fingerprint="a" * 64,
                expires_at=datetime.fromisoformat(core.clock()) + timedelta(hours=1),
            )
        )
    assert core.store.rows("SELECT * FROM source_grants") == []


@pytest.mark.parametrize("source", ["bilibili", "heybox"])
@pytest.mark.parametrize("grant_end", ["revoke", "expire"])
def test_successful_history_can_share_after_future_read_permission_ends(
    core, source, grant_end
):
    enable_sharing(core)
    event = accepted_text(core, source)
    grant = SourceGrantSnapshot(
        grant_id="grant_" + "b" * 32,
        epoch=1,
        fingerprint="b" * 64,
        expires_at=datetime.fromisoformat(core.clock()) + timedelta(minutes=1),
    )
    core.observations.grants.register(
        SourceGrantRegistration(adapter_id="reader", **grant.snapshot())
    )
    event.source_grant = grant
    core.observations.append("reader", event)
    if grant_end == "revoke":
        core.observations.grants.revoke(grant.grant_id)
    else:
        core.clock.advance(61)
    assert core.social.consider(event.event_id)
    assert core.social.flush()
    assert not core.store.rows("SELECT * FROM outbox")
    job = core.store.one("SELECT job_id FROM jobs WHERE kind='activity' AND state='QUEUED'")
    assert job and run(core.run_once(job_id=job["job_id"]))
    bodies = [json.loads(row["content_json"])["text"] for row in
              core.store.rows('SELECT content_json FROM outbox ORDER BY record_sequence')]
    assert bodies == [core.model.activity, BILI if source == "bilibili" else HEYBOX]
    assert json.loads(core.model.calls[0][0][-1]["content"])["text"] == event.payload.text
    stored = core.store.one('SELECT body_json FROM events WHERE event_id=%s', (event.event_id,))
    assert json.loads(stored["body_json"])["text"] == event.payload.text
    assert len(core.model.calls) == 1 and core.model.calls[0][1] == "activity"


def test_owner_forget_invalidates_accepted_history_and_queued_share(core):
    enable_sharing(core)
    event = accepted_text(core)
    grant = SourceGrantSnapshot(
        grant_id="grant_" + "c" * 32,
        epoch=1,
        fingerprint="c" * 64,
        expires_at=datetime.fromisoformat(core.clock()) + timedelta(hours=1),
    )
    core.observations.grants.register(
        SourceGrantRegistration(adapter_id="reader", **grant.snapshot())
    )
    event.source_grant = grant
    core.observations.append("reader", event)
    assert core.social.consider(event.event_id)
    core.observations.grants.forget(grant.grant_id, core.ledger)
    assert core.social.consider(event.event_id) is None
    assert not core.social.flush()
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.store.one("SELECT redacted FROM observations")["redacted"] == 1


@pytest.mark.parametrize("source", ["bilibili", "heybox"])
def test_accepted_text_history_without_url_needs_no_new_fetch_or_receipt(core, source):
    enable_sharing(core)
    event = accepted_text(core, source)
    event.payload.url = ""
    core.observations.append("reader", event)
    assert core.social.consider(event.event_id)
    assert core.social.flush()
    job = core.store.one("SELECT job_id FROM jobs WHERE kind='activity' AND state='QUEUED'")
    assert job and run(core.run_once(job_id=job["job_id"]))
    body = json.loads(core.store.one("SELECT content_json FROM outbox")["content_json"])["text"]
    assert body == core.model.activity
    assert core.store.rows("SELECT * FROM exploration_requests") == []
    assert len(core.model.calls) == 1 and core.model.calls[0][1] == "activity"


@pytest.mark.parametrize(
    "source,legacy_url",
    [
        ("bilibili", "https://m.bilibili.com:443/video/BV1xx411c7mD/?p=1"),
        ("heybox", "https://xiaoheihe.cn:443/a/123456?tracking=old"),
    ],
)
def test_legacy_queued_alias_url_keeps_same_accepted_content(core, source, legacy_url):
    enable_sharing(core)
    event = accepted_text(core, source)
    event.payload.url = legacy_url
    core.observations.append("reader", event)
    candidate = core.social.consider(event.event_id)
    assert candidate
    # Before normalization, queued candidates retained the accepted URL verbatim.
    core.store.execute(
        'UPDATE social_candidates SET canonical_url=%s WHERE candidate_id=%s',
        (legacy_url, candidate),
    )
    assert core.social.consider(event.event_id) == candidate
    assert core.social.flush()
    job = core.store.one("SELECT job_id FROM jobs WHERE kind='activity' AND state='QUEUED'")
    assert job and run(core.run_once(job_id=job["job_id"]))
    bodies = [json.loads(row["content_json"])["text"] for row in
              core.store.rows('SELECT content_json FROM outbox ORDER BY record_sequence')]
    assert bodies == [core.model.activity, BILI if source == "bilibili" else HEYBOX]
    assert core.store.rows("SELECT * FROM exploration_requests") == []
    assert len(core.model.calls) == 1 and core.model.calls[0][1] == "activity"


@pytest.mark.parametrize(
    "source,queued_url",
    [
        ("bilibili", HEYBOX),
        ("heybox", BILI),
        ("bilibili", "https://www.bilibili.com.evil.example/video/BV1xx411c7mD"),
        ("heybox", "https://www.xiaoheihe.cn:8443/a/123456"),
        ("bilibili", "https://www.bilibili.com/video/av999"),
    ],
)
def test_legacy_queued_url_still_rejects_invalid_or_different_source(core, source, queued_url):
    enable_sharing(core)
    event = accepted_text(core, source)
    core.observations.append("reader", event)
    candidate = core.social.consider(event.event_id)
    assert candidate
    core.store.execute(
        'UPDATE social_candidates SET canonical_url=%s WHERE candidate_id=%s',
        (queued_url, candidate),
    )
    assert not core.social.flush()
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.store.one((
        'SELECT state FROM social_candidates WHERE candidate_id=%s'
    ), (candidate,))[
        "state"
    ] == "SKIPPED"


@pytest.mark.parametrize("mutation", ["platform", "text", "url"])
def test_queued_share_rechecks_same_platform_url_and_accepted_text(core, mutation):
    enable_sharing(core)
    event = accepted_text(core)
    core.observations.append("reader", event)
    candidate = core.social.consider(event.event_id)
    assert candidate
    if mutation == "platform":
        core.store.execute(
            "UPDATE events SET source='observation.unknown' WHERE event_id=%s", (event.event_id,)
        )
    elif mutation == "url":
        core.store.execute(
            'UPDATE observations SET canonical_url=%s WHERE event_id=%s', (HEYBOX, event.event_id)
        )
    else:
        core.store.execute(
            'UPDATE events SET body_json=%s WHERE event_id=%s',
            (json.dumps({"title": "来源材料", "text": "未经读取的替换文本"}), event.event_id),
        )
    assert not core.social.flush()
    assert core.store.rows("SELECT * FROM outbox") == []
    assert (
        core.store.one('SELECT state FROM social_candidates WHERE candidate_id=%s', (candidate,))[
            "state"
        ]
        == "SKIPPED"
    )
