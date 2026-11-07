"""SDK tools reuse authenticated owner sources and the original Reader queue."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field
from pydantic_ai import RunContext
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.toolsets import FunctionToolset

from companion import owner_authority, owner_input, reply_revision
from companion.agent_runtime import check_conversation
from companion.capabilities import public_source_readiness
from companion.contracts import CoreError
from companion.sources import content_target, web_hosts
from companion.store import after, encode
from companion.transport import digest


def _remember(core, job, snapshots, sources):
    core.goals.validate_sources(sources)
    reply_revision.remember_sources(core.store, job, sources)
    proof = {"kind": "sources", "purpose": "content", "event_ids": sorted(set(sources))}
    if proof not in snapshots:
        snapshots.append(proof)
    check_conversation(core, job, snapshots)


def _read_owner_source(store, settings, clock, job, event_id, *, executing):
    from companion.goals import GoalEngine

    view = owner_authority.check_job(store, settings, clock, job,
                                     purpose="content", executing=executing)
    source, _, _ = owner_input.source(store, settings, view, event_id,
                                     clock(), history=event_id != job["event_id"])
    if source["conversation_id"] != job["conversation_id"]:
        raise CoreError("CONVERSATION_READER_SOURCE_REQUIRED", 403)
    GoalEngine(store, settings, None, clock).validate_sources([event_id])
    if not reply_revision.source_active(store, event_id):
        raise CoreError("REPLY_SOURCE_RECALLED", 409)
    return source


def _owner_source(core, job, event_id):
    return _read_owner_source(core.store, core.settings, core.clock, job, event_id, executing=True)


def catalog(core, job, snapshots):
    """Bounded declarations, not a second copy of fetched documents."""
    rows = core.store.rows(
        (
            'SELECT c.item_id, c.owner_event_id, c.target_url AS '
            'url, c.read_request_id, c.state, e.occurred_at FROM '
            'content_items AS c JOIN events AS e ON e.event_id = '
            'c.owner_event_id WHERE e.conversation_id = %s AND '
            "c.kind = 'link' AND c.redacted = 0 AND e.redacted = 0 "
            'AND e.record_sequence <= (SELECT record_sequence AS '
            'rowid FROM events WHERE event_id = %s) ORDER BY '
            'e.record_sequence DESC NULLS LAST, c.slot NULLS FIRST '
            'LIMIT %s'
        ),
        (job["conversation_id"], job["event_id"], core.settings.content.max_links_per_event * 3),
    )
    valid = []
    for row in rows:
        try:
            _owner_source(core, job, row["owner_event_id"])
        except CoreError:
            continue
        if row["read_request_id"]:
            request = core.store.one('SELECT state FROM exploration_requests WHERE request_id = %s '
                'AND redacted = 0',
                                     (row["read_request_id"],))
            if request is None:
                continue
            row["state"] = request["state"]
        else:
            row["state"] = "DISCOVERED"
        valid.append(row)
    _remember(core, job, snapshots, [row["owner_event_id"] for row in valid])
    return valid


def _reading_result(store, settings, clock, job, request_id, *, executing):
    from companion.bili_video import BiliVideoService
    from companion.exploration import ExplorationService
    from companion.goals import GoalEngine
    from companion.observation import ObservationService

    # These existing services only hold references; no Core lifecycle or model is opened.
    goals = GoalEngine(store, settings, None, clock)
    observations = ObservationService(store, settings, clock)
    exploration = ExplorationService(store, settings, observations, goals, None, None, clock)
    row = store.one('SELECT exploration_requests.request_id AS request_id, '
        'exploration_requests.request_key AS request_key, exploration_requests.adapter_id AS '
        'adapter_id, exploration_requests.operation AS operation, exploration_requests.url AS url, '
        'exploration_requests.goal_step_json AS goal_step_json, exploration_requests.state AS '
        'state, exploration_requests.lease_token AS lease_token, exploration_requests.lease_until '
        'AS lease_until, exploration_requests.captured_privacy_epoch AS captured_privacy_epoch, '
        'exploration_requests.reserved_bytes AS reserved_bytes, exploration_requests.used_bytes AS '
        'used_bytes, exploration_requests.result_sha256 AS result_sha256, '
        'exploration_requests.last_error_code AS last_error_code, exploration_requests.created_at '
        'AS created_at, exploration_requests.started_at AS started_at, '
        'exploration_requests.updated_at AS updated_at, exploration_requests.redacted AS redacted, '
        'exploration_requests.reserved_requests AS reserved_requests, '
        'exploration_requests.used_requests AS used_requests, exploration_requests.source_grant_id '
        'AS source_grant_id, exploration_requests.source_grant_epoch AS source_grant_epoch, '
        'exploration_requests.authority_json AS authority_json, exploration_requests.query AS '
        'query, exploration_requests.discovery_mode AS discovery_mode FROM exploration_requests AS '
        'exploration_requests WHERE exploration_requests.request_id = %s AND '
        'exploration_requests.redacted = 0',
                         (request_id,))
    if not row or row["goal_step_json"] != "{}":
        raise CoreError("CONVERSATION_READER_SOURCE_REQUIRED", 403)
    sources = [item["event_id"] for item in store.rows(
        'SELECT event_id FROM exploration_evidence WHERE request_id = %s', (request_id,),
    )]
    owners = [event_id for event_id in sources if reply_revision.owner_input(store, event_id)]
    if not owners:
        raise CoreError("CONVERSATION_READER_SOURCE_REQUIRED", 403)
    for event_id in owners:
        _read_owner_source(store, settings, clock, job, event_id, executing=executing)
    policy = exploration.adapter(row["adapter_id"])
    captured = observations.grants.captured(row)
    if captured:
        observations.grants.require(row["adapter_id"], captured)
    elif policy.source_grant_required:
        if row["state"] == "QUEUED" and row["source_grant_id"] is None:
            # Not leased yet: no binding exists, only a valid grant must. The lease
            # binds the Reader's grant and every later check requires that binding.
            observations.grants.require_available(row["adapter_id"])
        else:
            observations.grants.require(row["adapter_id"], None)
    items = []
    if row["state"] == "DONE":
        attempt = store.one('SELECT reading_attempts.request_id AS request_id, '
            'reading_attempts.adapter_id AS adapter_id, reading_attempts.lease_token AS '
            'lease_token, reading_attempts.lease_until AS lease_until, '
            'reading_attempts.context_json AS context_json, reading_attempts.allowed_hosts_json AS '
            'allowed_hosts_json, reading_attempts.original_url_sha256 AS original_url_sha256, '
            'reading_attempts.source_grant_json AS source_grant_json, reading_attempts.state AS '
            'state, reading_attempts.result_sha256 AS result_sha256, '
            'reading_attempts.result_event_ids_json AS result_event_ids_json, '
            'reading_attempts.observed_requests AS observed_requests, '
            'reading_attempts.observed_bytes AS observed_bytes, reading_attempts.created_at AS '
            'created_at, reading_attempts.updated_at AS updated_at FROM reading_attempts AS '
            'reading_attempts WHERE reading_attempts.request_id = %s',
                                 (request_id,))
        if (not attempt or attempt["state"] != "DONE" or not attempt["result_sha256"]
            or attempt["result_sha256"] != row["result_sha256"]
            or attempt["adapter_id"] != row["adapter_id"]
            or attempt["original_url_sha256"] != hashlib.sha256(row["url"].encode()).hexdigest()):
            raise CoreError("CONVERSATION_READER_RESULT_CHANGED", 409)
        try:
            result_ids = json.loads(attempt["result_event_ids_json"])
        except (ValueError, TypeError) as error:
            raise CoreError("CONVERSATION_READER_RESULT_CHANGED", 409) from error
        if (not isinstance(result_ids, list) or len(result_ids) > 40
            or any(not isinstance(value, str) for value in result_ids)
            or len(result_ids) != len(set(result_ids))):
            raise CoreError("CONVERSATION_READER_RESULT_CHANGED", 409)
        BiliVideoService(store, settings, None, clock=clock).check_result(request_id, result_ids)
        sources = sorted(set(sources) | set(result_ids))
        for item in store.rows(
            'SELECT e.event_id, e.body_json, e.occurred_at, o.result_status, o.evidence_json, '
            'o.canonical_url FROM events AS e JOIN observations AS o USING (event_id) WHERE '
            'e.event_id IN (SELECT value COLLATE "C" FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS '
            'JSON)) AS parsed(value)) AND e.redacted = 0 AND o.redacted = 0 ORDER BY '
            'e.record_sequence NULLS FIRST', (encode(result_ids),),
        ):
            body = json.loads(item["body_json"])
            items.append({"event_id": item["event_id"], "observed_at": item["occurred_at"],
                          "coverage": item["result_status"], "title": body.get("title", ""),
                          "text": body.get("text", ""), "url": item["canonical_url"],
                          "evidence": json.loads(item["evidence_json"])})
        if len(items) != len(result_ids):
            raise CoreError("CONVERSATION_READER_RESULT_CHANGED", 409)
    goals.validate_sources(sources)
    return {"request_id": request_id, "state": row["state"], "operation": row["operation"],
            "as_of": clock(), "error_code": row["last_error_code"],
            "source_event_ids": sorted(sources), "items": items}


def _result_digest(store, result):
    receipt = store.one('SELECT adapter_id, lease_token, context_json, source_grant_json, '
        'original_url_sha256, result_sha256, result_event_ids_json FROM reading_attempts WHERE '
        'request_id = %s', (result["request_id"],))
    bindings = store.rows('SELECT event_id FROM exploration_evidence WHERE request_id = %s ORDER '
        'BY event_id NULLS FIRST', (result["request_id"],))
    return digest({"result": {key: value for key, value in result.items() if key != "as_of"},
                   "receipt": receipt, "source_bindings": bindings})


def reading_result(core, job, snapshots, request_id, *, include_pending=False):
    result = _reading_result(core.store, core.settings, core.clock, job, request_id, executing=True)
    _remember(core, job, snapshots, result["source_event_ids"])
    if result["state"] in {"QUEUED", "RUNNING"} and not include_pending:
        return None
    if result["state"] == "DONE":
        proof = {"kind": "reader", "purpose": "content", "request_id": request_id,
                 "sha256": _result_digest(core.store, result)}
        if proof not in snapshots:
            snapshots.append(proof)
    return result


def revalidate_results(store, settings, clock, job, snapshots, *, executing=False):
    """Read-only context check for active or completed jobs, including delivery guards."""
    proofs = [proof for proof in snapshots if proof["kind"] == "reader"]
    if not proofs:
        return
    reply_revision.check_job(store, job)
    for proof in proofs:
        if (proof.get("purpose") != "content" or _result_digest(store, _reading_result(
            store, settings, clock, job, proof["request_id"], executing=executing,
        )) != proof["sha256"]):
            raise CoreError("CONVERSATION_READER_RESULT_CHANGED", 409)


def check_results(core, job, snapshots):
    revalidate_results(core.store, core.settings, core.clock, job, snapshots, executing=True)


def deferred_results(core, job, snapshots, conversation):
    check_conversation(core, job, snapshots)
    pending = conversation.deferred_tool_requests
    if pending is None:
        return True, None
    check_conversation(core, job, snapshots, purpose="content")
    answers = {}
    for call in pending.calls:
        binding = core.store.one('SELECT COALESCE(voice_job_id, request_id) AS request_id, '
            'tool_name FROM agent_tool_calls WHERE job_id = %s AND tool_call_id = %s AND state = '
            "'DEFERRED'",
                                 (job["job_id"], call.tool_call_id))
        metadata = pending.metadata.get(call.tool_call_id, {})
        if not binding or not binding["request_id"] or binding["request_id"] != metadata.get(
            "request_id",
        ):
            raise CoreError("CONVERSATION_DEFERRED_BINDING_REQUIRED", 409)
        if binding["tool_name"] == "sing_song":
            from companion.agent_singing import ready_result

            answer = ready_result(core, job, snapshots, binding["request_id"])
            if answer is None:
                return False, None
            answers[call.tool_call_id] = answer
            continue
        row = core.store.one('SELECT created_at FROM exploration_requests WHERE request_id = %s',
                             (binding["request_id"],))
        expired_wait = row and after(row["created_at"], core.settings.content.wait_seconds) <= (
            core.clock())
        result = reading_result(core, job, snapshots, binding["request_id"],
                                include_pending=bool(expired_wait))
        if result is None:
            return False, None
        answers[call.tool_call_id] = result
    return True, pending.build_results(calls=answers)


def reader_tools(core, job, snapshots, persistence):
    """No provider I/O here: native handlers enqueue the original bounded Reader work."""
    def check(purpose="content"):
        check_conversation(core, job, snapshots, purpose=purpose)
        _owner_source(core, job, job["event_id"])

    def adapter(source, operation):
        for name, policy in core.settings.observation_adapters.items():
            if policy.source == source:
                ready = public_source_readiness(core.store, core.settings, core.clock,
                                                name, operation=operation)
                if ready["state"] == "READY":
                    return name
        return None

    async def reading_catalog() -> dict:
        """List recent shared link identities and original read states; titles are not full text."""
        check()
        return {"items": catalog(core, job, snapshots)}

    async def read_content(ctx: RunContext, url: Annotated[str, Field(max_length=2048)]) -> dict:
        """Read a shared URL, reusing its original request even after a follow-up or UNKNOWN.

        Args:
            url: A URL in the owner's shared-link catalog, not an invented document address.
        """
        check()
        try:
            source, canonical = content_target(url, web_hosts=web_hosts(core.settings))
        except CoreError:
            return {"state": "UNAVAILABLE", "error_code": "CONTENT_TARGET_REQUIRED"}
        item = next((row for row in catalog(core, job, snapshots) if row["url"] == canonical), None)
        if item is None:
            return {"state": "UNAVAILABLE", "error_code": "OWNER_LINK_REQUIRED"}
        request_id = item["read_request_id"]
        with core.store.transaction():
            check()
            if request_id is None:
                name = adapter(source, "read")
                if name is None:
                    return {"state": "UNAVAILABLE", "error_code": "CONTENT_READER_UNAVAILABLE"}
                request_id = core.exploration.create(
                    name, "read", item["item_id"], canonical,
                    source_event_ids=[item["owner_event_id"]],
                )
                core.store.execute('UPDATE content_items SET read_request_id = %s WHERE item_id = '
                    '%s',
                                   (request_id, item["item_id"]))
            result = reading_result(core, job, snapshots, request_id)
            metadata = persistence.bind_request(ctx.tool_call_id, request_id)
            if result is not None:
                return result
        raise CallDeferred(metadata=metadata)

    async def discover_content(ctx: RunContext, source: Literal["bilibili", "heybox"],
        query: Annotated[str | None, Field(min_length=2, max_length=100)] = None) -> dict:
        """Discover real source titles; Bilibili accepts an optional song/title search query."""
        check("exploration")
        paused = core.store.one("SELECT value FROM runtime_meta WHERE key = 'life_paused'")
        if not core.settings.exploration_enabled or (paused and paused["value"] == "1"):
            return {"state": "UNAVAILABLE", "error_code": "AUTONOMY_PAUSED"}
        name = adapter(source, "discover")
        if name is None:
            return {"state": "UNAVAILABLE", "error_code": "CONTENT_READER_UNAVAILABLE"}
        with core.store.transaction():
            check("exploration")
            request_id = core.exploration.create(
                name, "discover", "chat-tool:" + job["job_id"] + ":" + ctx.tool_call_id,
                source_event_ids=[job["event_id"]],
                query=query,
            )
            metadata = persistence.bind_request(ctx.tool_call_id, request_id)
        raise CallDeferred(metadata=metadata)

    async def analyze_shared_video(ctx: RunContext,
        url: Annotated[str, Field(max_length=2048)]) -> dict:
        """Analyze bounded preview frames and optional clips of a previously read Bili link.

        Args:
            url: A shared Bilibili URL whose original Reader result contains video metadata.
        """
        check("bili_video")
        source, canonical = content_target(url, web_hosts=web_hosts(core.settings))
        item = next((row for row in catalog(core, job, snapshots) if row["url"] == canonical), None)
        if source != "bilibili" or item is None or item["read_request_id"] is None:
            return {"state": "UNAVAILABLE", "error_code": "BILI_VIDEO_PARENT_READING_REQUIRED"}
        parent = reading_result(core, job, snapshots, item["read_request_id"])
        observations = parent["items"] if parent else []
        source_id = next((row["event_id"] for row in observations
            if row["evidence"].get("read_observation", {}).get("evidence", {}).get("video")), None)
        if source_id is None:
            return {"state": "UNAVAILABLE", "error_code": "BILI_VIDEO_METADATA_REQUIRED"}
        with core.store.transaction():
            check("bili_video")
            request_id = core.bili_video.schedule(item["item_id"], source_id, job_id=job["job_id"])
            result = reading_result(core, job, snapshots, request_id)
            metadata = persistence.bind_request(ctx.tool_call_id, request_id)
            if result is not None:
                return result
        raise CallDeferred(metadata=metadata)

    async def reading_status(request_id: str) -> dict:
        """Inspect an original Reader request; UNKNOWN never starts another attempt."""
        check()
        return reading_result(core, job, snapshots, request_id, include_pending=True)

    async def cancel_reading(request_id: str) -> dict:
        """Cancel further work on an original read; already-started I/O keeps its liability."""
        check()
        with core.store.transaction():
            result = reading_result(core, job, snapshots, request_id, include_pending=True)
            prior = result["state"]
            if prior in {"QUEUED", "RUNNING"}:
                core.store.execute("UPDATE exploration_requests SET state = 'CANCELLED', "
                    "last_error_code = 'OWNER_READING_CANCELLED', updated_at = %s WHERE request_id "
                    '= %s AND state = %s',
                                   (core.clock(), request_id, prior))
            return {"request_id": request_id,
                    "state": "CANCELLED" if prior in {"QUEUED", "RUNNING"} else prior,
                    "execution_outcome": "UNKNOWN" if prior == "RUNNING" else prior}

    handlers = [reading_catalog, read_content, discover_content, reading_status, cancel_reading]
    view = owner_authority.check_job(core.store, core.settings, core.clock, job, purpose="content")
    if core.settings.bili_video.enabled and "bili_video" in view.policy.purposes:
        handlers.append(analyze_shared_video)
    return FunctionToolset(handlers, sequential=True, max_retries=1)
