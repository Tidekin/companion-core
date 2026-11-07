"""QZone targets reuse native Reader records; review uses the original Life queue."""

import hmac
import json

from companion import owner_authority, owner_input
from companion.contracts import CoreError
from companion.observation import expired_sources
from companion.paid_budget import sha
from companion.privacy import redact, sensitive
from companion.qzone_protocol import QZoneEvidence
from companion.source_grants import SourceGrantService
from companion.store import after

TARGET_TTL_SECONDS = 300


def _selection(store, row, grant):
    sources = store.rows(
        'SELECT e.event_id FROM exploration_evidence AS x JOIN events AS e USING (event_id) WHERE '
        "x.request_id = %s AND e.source = 'qq.onebot' AND e.actor_id = 'owner' ORDER BY e.event_id "
        'NULLS FIRST',
        (row["request_id"],),
    )
    return sha({"adapter_id": row["adapter_id"], "url": row["url"], "sources": sources,
                "grant": grant, "authority": json.loads(row["authority_json"]),
                "scope": "latest_post_text", "max_items": 1, "max_requests": 1,
                "expires_at": after(row["created_at"], TARGET_TTL_SECONDS)})


def validate_target(store, settings, clock, row):
    """Required at lease and every I/O check, including when runtime is switched OFF."""
    if not settings.qzone_reading_enabled:
        raise CoreError("QZONE_READING_DISABLED", 403)
    view = owner_authority.current(store, settings, clock, purpose="social",
                                   authority=json.loads(row["authority_json"]))
    if row["operation"] != "read" or row["goal_step_json"] != "{}" or row["redacted"]:
        raise CoreError("QZONE_TARGET_SCOPE_REQUIRED", 403)
    if after(row["created_at"], TARGET_TTL_SECONDS) <= clock():
        raise CoreError("QZONE_TARGET_EXPIRED", 403)
    grant = SourceGrantService(store, settings, clock).captured(row)
    if grant is None:
        raise CoreError("SOURCE_GRANT_REQUIRED", 403)
    proof = store.one('SELECT safe_detail_json FROM audit_log WHERE action = '
        "'qzone.target_selected' AND entity_id = %s", (row["request_id"],))
    if not proof or json.loads(proof["safe_detail_json"]) != {
        "selection_sha256": _selection(store, row, grant.snapshot()),
    }:
        raise CoreError("QZONE_TARGET_SCOPE_REQUIRED", 403)
    sources = store.rows(
        'SELECT event_id FROM exploration_evidence AS x JOIN events AS e USING (event_id) WHERE '
        "request_id = %s AND e.source = 'qq.onebot'", (row["request_id"],),
    )
    if len(sources) != 1:
        raise CoreError("QZONE_TARGET_SCOPE_REQUIRED", 403)
    owner_input.source(store, settings, view, sources[0]["event_id"], clock())


def select_target(reading, body):
    s, cfg, clock = reading.store, reading.settings, reading.clock
    with s.transaction():
        if not cfg.qzone_reading_enabled:
            raise CoreError("QZONE_READING_DISABLED", 403)
        view = owner_authority.current(s, cfg, clock, purpose="social")
        owner_authority.current(s, cfg, clock, purpose="content", authority=view.authority)
        owner_input.source(s, cfg, view, body.source_event_id, clock())
        policy = reading.exploration.adapter(body.adapter_id)
        if policy.source != "qzone" or not policy.source_grant_required:
            raise CoreError("QZONE_ACCOUNT_ADAPTER_REQUIRED", 403)
        grants = SourceGrantService(s, cfg, clock)
        grants.require(body.adapter_id, body.source_grant)
        key = "qzone:" + body.operation_id
        previous = s.one('SELECT exploration_requests.request_id AS request_id, '
            'exploration_requests.request_key AS request_key, exploration_requests.adapter_id AS '
            'adapter_id, exploration_requests.operation AS operation, exploration_requests.url AS '
            'url, exploration_requests.goal_step_json AS goal_step_json, '
            'exploration_requests.state AS state, exploration_requests.lease_token AS lease_token, '
            'exploration_requests.lease_until AS lease_until, '
            'exploration_requests.captured_privacy_epoch AS captured_privacy_epoch, '
            'exploration_requests.reserved_bytes AS reserved_bytes, '
            'exploration_requests.used_bytes AS used_bytes, exploration_requests.result_sha256 AS '
            'result_sha256, exploration_requests.last_error_code AS last_error_code, '
            'exploration_requests.created_at AS created_at, exploration_requests.started_at AS '
            'started_at, exploration_requests.updated_at AS updated_at, '
            'exploration_requests.redacted AS redacted, exploration_requests.reserved_requests AS '
            'reserved_requests, exploration_requests.used_requests AS used_requests, '
            'exploration_requests.source_grant_id AS source_grant_id, '
            'exploration_requests.source_grant_epoch AS source_grant_epoch, '
            'exploration_requests.authority_json AS authority_json, exploration_requests.query AS '
            'query, exploration_requests.discovery_mode AS discovery_mode FROM '
            'exploration_requests AS exploration_requests WHERE exploration_requests.request_key = '
            '%s', (key,))
        if previous:
            if previous["redacted"]:
                raise CoreError("SOURCE_ERASED", 410)
            if (previous["adapter_id"] != body.adapter_id or previous["url"] != body.url
                or grants.captured(previous) != body.source_grant
                or not s.one('SELECT 1 FROM exploration_evidence WHERE request_id = %s AND '
                    'event_id = %s',
                             (previous["request_id"], body.source_event_id))):
                raise CoreError("IDEMPOTENCY_CONFLICT", 409)
            return {"request_id": previous["request_id"], "duplicate": True}
        # This explicit control action admits the target under the current authority;
        # the selected owner event is provenance, not an implicit grant from an old job.
        request_id = reading.exploration.create(body.adapter_id, "read", key, body.url)
        s.insert("exploration_evidence", request_id=request_id, event_id=body.source_event_id)
        s.execute('UPDATE exploration_requests SET source_grant_id = %s, source_grant_epoch = %s '
            'WHERE request_id = %s', (body.source_grant.grant_id, body.source_grant.epoch,
                                        request_id))
        row = s.one('SELECT exploration_requests.request_id AS request_id, '
            'exploration_requests.request_key AS request_key, exploration_requests.adapter_id AS '
            'adapter_id, exploration_requests.operation AS operation, exploration_requests.url AS '
            'url, exploration_requests.goal_step_json AS goal_step_json, '
            'exploration_requests.state AS state, exploration_requests.lease_token AS lease_token, '
            'exploration_requests.lease_until AS lease_until, '
            'exploration_requests.captured_privacy_epoch AS captured_privacy_epoch, '
            'exploration_requests.reserved_bytes AS reserved_bytes, '
            'exploration_requests.used_bytes AS used_bytes, exploration_requests.result_sha256 AS '
            'result_sha256, exploration_requests.last_error_code AS last_error_code, '
            'exploration_requests.created_at AS created_at, exploration_requests.started_at AS '
            'started_at, exploration_requests.updated_at AS updated_at, '
            'exploration_requests.redacted AS redacted, exploration_requests.reserved_requests AS '
            'reserved_requests, exploration_requests.used_requests AS used_requests, '
            'exploration_requests.source_grant_id AS source_grant_id, '
            'exploration_requests.source_grant_epoch AS source_grant_epoch, '
            'exploration_requests.authority_json AS authority_json, exploration_requests.query AS '
            'query, exploration_requests.discovery_mode AS discovery_mode FROM '
            'exploration_requests AS exploration_requests WHERE exploration_requests.request_id = '
            '%s', (request_id,))
        s.audit("qzone.target_selected", request_id, body.operation_id, clock(),
                selection_sha256=_selection(s, row, body.source_grant.snapshot()))
        return {"request_id": request_id, "duplicate": False}


def review_source(store, settings, event_id, now):
    """Retained authorized history can be reviewed after read expiry, never shared."""
    row = store.one('SELECT o.event_id AS event_id, o.adapter_id AS adapter_id, o.source_kind AS '
        'source_kind, o.result_status AS result_status, o.source_sha256 AS source_sha256, '
        'o.canonical_url AS canonical_url, o.device_id AS device_id, o.session_id AS session_id, '
        'o.consent_id AS consent_id, o.evidence_json AS evidence_json, o.captured_privacy_epoch AS '
        'captured_privacy_epoch, o.redacted AS redacted, o.created_at AS created_at, e.body_json '
        'AS body_json, e.ingested_at AS ingested_at FROM observations AS o JOIN events AS e ON '
        "o.event_id = e.event_id WHERE o.event_id = %s AND e.source = 'observation.qzone' AND "
        "e.redacted = 0 AND o.redacted = 0 AND o.source_kind = 'text' AND o.result_status = "
        "'FETCHED'", (event_id,))
    if not row or expired_sources(store, [event_id], after(
        now, -86400 * settings.raw_event_retention_days,
    )):
        raise CoreError("QZONE_REVIEW_SOURCE_REQUIRED", 403)
    from companion.reading_protocol import ObservationEvidence

    try:
        body = json.loads(row["body_json"])
        observed = ObservationEvidence.model_validate({
            **json.loads(row["evidence_json"])["read_observation"],
            "text": body["text"], "title": body["title"],
        })
    except (ValueError, KeyError, TypeError):
        raise CoreError("QZONE_REVIEW_SOURCE_REQUIRED", 403) from None
    grant = observed.provenance.source_grant
    if (observed.provenance.source != "qzone" or not isinstance(observed.evidence, QZoneEvidence)
        or observed.url != row["canonical_url"] or observed.content_sha256 != row["source_sha256"]
        or grant is None or not store.one(
            'SELECT 1 FROM source_grant_events WHERE event_id = %s AND grant_id = %s AND '
            'grant_epoch = %s',
            (event_id, grant.grant_id, grant.epoch))):
        raise CoreError("QZONE_REVIEW_SOURCE_REQUIRED", 403)
    text = body["text"]
    if sensitive(text) or redact(text, settings) != text:
        raise CoreError("QZONE_REVIEW_SOURCE_REQUIRED", 403)
    event = store.one('SELECT event_id, source, body_json, occurred_at, ingested_at, '
        'conversation_id, redacted FROM events WHERE event_id = %s', (event_id,))
    return {"event_id": event_id, "excerpt": text[:500], "url": observed.url,
            "evidence_level": "FETCHED", "source_sha256": sha(event),
            "author_uin": observed.evidence.author_uin, "tid": observed.evidence.tid,
            "published_at": observed.evidence.published_at.isoformat()}


def register_routes(app, control_auth, reading_service, settings):
    from fastapi import Response

    from companion.qzone_protocol import QZoneTarget
    from companion.qzone_publication_api import register_routes as register_publication_routes

    def control_check(request):
        credential = request.headers.get("authorization", "")

        def check(store):
            token = settings.control_token.get_secret_value()
            if not token or not hmac.compare_digest(credential, "Bearer " + token):
                raise CoreError("OWNER_REQUIRED", 403)

        return check

    register_publication_routes(app, settings, "/internal/v2/qzone/publications", control_auth,
                                control_check)

    @app.post("/internal/v2/qzone/targets", dependencies=control_auth, status_code=202)
    async def select(body: QZoneTarget):
        clock = app.state.core.clock
        return await app.state.api_store.run(lambda store: select_target(
            reading_service(store, settings, clock), body,
        ))

    @app.get("/internal/v2/qzone/targets", dependencies=control_auth)
    async def targets(response: Response):
        response.headers["Cache-Control"] = "no-store"
        rows = app.state.core.store.rows(
            'SELECT x.request_id, x.adapter_id, x.url, x.state, x.source_grant_id, '
            'x.source_grant_epoch, x.created_at, x.last_error_code, g.expires_at AS '
            'grant_expires_at, a.result_event_ids_json FROM exploration_requests AS x LEFT JOIN '
            'reading_attempts AS a USING (request_id) JOIN source_grant_revisions AS g ON '
            'g.grant_id = x.source_grant_id AND g.epoch = x.source_grant_epoch WHERE x.request_key '
            "LIKE 'qzone:%%' AND x.redacted = 0 ORDER BY x.record_sequence DESC NULLS LAST LIMIT "
            '30')
        return {"items": [{**{k: v for k, v in r.items() if k not in {
            "grant_expires_at", "result_event_ids_json"}},
            "event_ids": json.loads(r["result_event_ids_json"] or "[]"),
            "expires_at": min(after(r["created_at"], TARGET_TTL_SECONDS), r["grant_expires_at"])}
            for r in rows]}

    @app.get("/internal/v2/qzone/sources/{event_id}", dependencies=control_auth)
    async def source(event_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        core = app.state.core
        owner_authority.current(core.store, settings, core.clock, purpose="social")
        return review_source(core.store, settings, event_id, core.clock())
