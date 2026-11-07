"""Explicit private companion scope over the native M1/life workflows.

This owns authority receipts, not a second job, memory, or delivery system.
The existing window, budget journal, source checks and Outbox remain authoritative.
"""

import json
from datetime import datetime

from companion import paid_budget, qq_source
from companion.contracts import CoreError
from companion.privacy import epoch
from companion.store import encode

FEATURES = (
    "memory",
    "diary",
    "affect",
    "consolidation",
    "reflection",
    "goals",
    "wake",
    "exploration",
    "social",
    "expression",
    "proactive",
)
SCENARIOS = {
    "chat": "memory",
    "activity": "proactive",
    "memory_extract": "memory",
    "memory_review": "memory",
    "diary": "diary",
    "reflection": "reflection",
    "content_vision": "content",
    "content_video": "content",
    "content_video_preview": "content",
    "meme_caption": "meme_semantics",
}
MODEL = "private_life_model:"
DELIVERY = "private_life_delivery:"


def enabled(settings):
    return bool(
        settings.live_text_window
        and settings.live_text_window.companion_scope == "owner_private_v1"
    )


def strict(store, settings):
    from companion import owner_authority
    from companion.live_text import active

    if settings.live_text_window is None and owner_authority.required(store, settings):
        # A stored, stopped legacy binding is historical custody only; native
        # OwnerAuthority (which refuses to activate over an unstopped window)
        # governs current work, so the retired window must not fence it.
        return False
    return active(store, settings) and not enabled(settings)


def busy(store, now, *, foreground_only=False):
    from companion.budget import is_background

    return any(
        not foreground_only or row["scenario"] not in {"gateway", "evaluation"}
        and not is_background(row["scenario"], row["job_id"])
        for row in store.rows(
            "SELECT m.scenario,m.job_id FROM model_usage m "
            "JOIN model_admissions a USING(request_id) "
            "WHERE m.state='RESERVED' AND a.lease_until>?", (now,),
        )
    )


def ensure_active(store, settings, clock):
    if enabled(settings):
        from companion.live_text import guard

        guard(store, settings, clock)


def owner_waiting(store, settings, now):
    from companion import owner_authority
    from companion.budget import chat_pending

    if owner_authority.required(store, settings):
        return owner_authority.owner_waiting(store, settings, now)
    return chat_pending(store, now) or bool(
        store.one(
            "SELECT 1 FROM runtime_meta r LEFT JOIN events e "
            "ON e.event_id=json_extract(r.value,'$.event_id') "
            "WHERE r.key LIKE 'live_text_input:%' AND e.event_id IS NULL "
            "AND json_extract(r.value,'$.window_sha256')=? LIMIT 1",
            (settings.live_text_window.plan_sha256,),
        )
    )


def ready(store, settings, clock):
    if not enabled(settings):
        return True
    try:
        ensure_active(store, settings, clock)
        return not busy(store, clock()) and not owner_waiting(store, settings, clock())
    except CoreError:
        return False


def recent_history(store, settings, job):
    from companion.dialogue_history import recent_context_sources, turn_messages

    approval = settings.live_text_window
    sources = recent_context_sources(
        store, job["conversation_id"], job["event_id"], approval.max_history_turns
    )
    while (
        sources
        and sum(
            len(message["content"].encode("utf-8"))
            for source in sources
            for message in turn_messages(source)
        )
        > approval.max_history_utf8_bytes
    ):
        sources.pop(0)
    return sources


def stop_work(store, now):
    """Revoke queued/leased native work; retain uncertainty for dispatched reads/sends."""
    for table, states in (
        ("jobs", "'QUEUED','RUNNING','RETRY_WAIT'"),
        ("outbox", "'PENDING','LEASED','RETRY_WAIT'"),
        ("diary_runs", "'PENDING','RUNNING','RETRY_WAIT'"),
        ("reflection_runs", "'RUNNING','RETRY_WAIT'"),
        ("consolidation_runs", "'SCHEDULED','LEASED','PARTIAL'"),
    ):
        store.execute(
            f"UPDATE {table} SET state='CANCELLED',lease_token=NULL,updated_at=?,"
            f"last_error_code='LIVE_WINDOW_STOPPED' WHERE state IN ({states})",
            (now,),
        )
    store.execute(
        "UPDATE exploration_requests SET state=CASE state WHEN 'QUEUED' THEN 'CANCELLED' "
        "ELSE 'UNKNOWN' END,lease_token=NULL,last_error_code='LIVE_WINDOW_STOPPED' "
        "WHERE state IN ('QUEUED','RUNNING')"
    )
    store.execute("DELETE FROM runtime_meta WHERE key='memory_extraction_lease'")


def event_digest(event):
    return paid_budget.sha(
        {
            key: event[key]
            for key in (
                "event_id",
                "source",
                "source_event_id",
                "conversation_id",
                "actor_id",
                "event_type",
                "occurred_at",
                "privacy_scope",
                "body_json",
                "redacted",
            )
        }
    )


def check_job(store, settings, approval, job):
    from companion.reply_revision import check_job as check_reply_job

    check_reply_job(store, job)
    if job["kind"] == "social_plan":
        from companion.social_plan_jobs import validate_binding

        return validate_binding(store, settings, job)
    if job["kind"] == "meme_caption":
        from companion.meme_core import check_caption_job

        return check_caption_job(store, settings, approval, job)
    if job["kind"] == "poke":
        from companion import poke

        poke.check_job(store, settings, approval, job)
        return store.one("SELECT * FROM events WHERE event_id=?", (job["event_id"],))
    event = store.one(
        "SELECT e.*,c.scope,c.owner_actor_id FROM events e JOIN conversations c "
        "USING(conversation_id) WHERE e.event_id=?",
        (job["event_id"],),
    )
    if (
        not event
        or event["redacted"]
        or event["privacy_scope"] != "private"
        or event["scope"] != "private"
        or event["owner_actor_id"] != "owner"
    ):
        raise CoreError("COMPANION_PRIVATE_SOURCE_REQUIRED", 403)
    if job["kind"] == "chat":
        qq_source.source(store, settings, approval, job["event_id"], allow_commands=True)
    elif not (
        job["kind"] == "activity"
        and event["source"] == "system.timer"
        and event["actor_id"] == "system"
        and event["event_type"] == "activity.tick"
        and approval.starts_at <= datetime.fromisoformat(event["occurred_at"]) < approval.ends_at
    ):
        raise CoreError("COMPANION_JOB_SCOPE_REQUIRED", 403)
    return event


def admit_model(store, settings, clock, job_id, scenario):
    from companion import live_text, notices

    approval, _, attempts = live_text.guard(store, settings, clock)
    job = store.one("SELECT j.*,e.body_json FROM jobs j JOIN events e USING(event_id) "
                    "WHERE job_id=?", (job_id,)) if job_id else None
    life_shadow = scenario == "activity" and notices.shadow(job)
    if life_shadow:
        notices.validate(store, settings, job, clock())
    feature = ("goals" if scenario == "activity" and notices.required(store, job_id)
               else SCENARIOS.get(scenario))
    allowed_feature = (
        settings.poke_policy.enabled if scenario == "qq_poke" else
        settings.social_autonomy.enabled if scenario == "social_autonomy" else
        settings.watch_ledger.enabled if scenario == "watch_review" else
        settings.style_distill.enabled if scenario == "style_distill" else
        settings.jargon.enabled if scenario == "jargon" else
        feature is not None and getattr(settings, feature + "_enabled")
    )
    if not allowed_feature and not life_shadow:
        raise CoreError("COMPANION_MODEL_SCOPE_REQUIRED", 403)
    if attempts >= approval.max_model_attempts:
        raise CoreError("LIVE_TEXT_MODEL_ATTEMPT_CAP", 429)
    if job_id is not None:
        job = store.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if not job:
            raise CoreError("COMPANION_JOB_SCOPE_REQUIRED", 403)
        check_job(store, settings, approval, job)
        allowed = {
            "chat": {"chat", "memory_review", "content_vision", "content_video",
                     "content_video_preview"},
            "activity": {"activity", "memory_review"},
            "meme_caption": {"meme_caption"},
            "poke": {"qq_poke"},
            "social_plan": {"social_autonomy"},
        }
        if scenario not in allowed.get(job["kind"], set()):
            raise CoreError("COMPANION_MODEL_SCOPE_REQUIRED", 403)
    elif scenario not in {"memory_extract", "memory_review", "diary", "reflection",
                          "social_autonomy", "watch_review", "style_distill", "jargon"}:
        raise CoreError("COMPANION_BACKGROUND_SCOPE_REQUIRED", 403)


def freeze_model(store, settings, request_id):
    if not enabled(settings):
        return
    row = store.one(
        "SELECT a.*,m.job_id,m.scenario FROM model_admissions a "
        "JOIN model_usage m USING(request_id) WHERE request_id=?",
        (request_id,),
    )
    value = {
        "plan_sha256": settings.live_text_window.plan_sha256,
        "privacy_epoch": epoch(store),
        "job_id": row["job_id"],
        "scenario": row["scenario"],
        "payload_sha256": row["payload_sha256"],
    }
    if row["job_id"]:
        job = store.one("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],))
        event = check_job(store, settings, settings.live_text_window, job)
        value["event_sha256"] = event_digest(event)
    store.insert("runtime_meta", key=MODEL + request_id, value=encode(value))


def check_model(store, settings, clock, request_id):
    from companion import live_text, qq_images

    row = store.one(
        "SELECT a.*,m.job_id,m.scenario,m.state FROM model_admissions a "
        "JOIN model_usage m USING(request_id) WHERE request_id=?",
        (request_id,),
    )
    if row and row["job_id"]:
        from companion.reply_revision import check_job as check_reply_job

        check_reply_job(store, store.one("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)))
    approval, _, _ = live_text.guard(store, settings, clock, current_request=request_id)
    record = live_text._meta(store, MODEL + request_id)
    retained_sources = bool(row and row["job_id"] and store.one(
        "SELECT 1 FROM jobs WHERE job_id=? AND context_tracked=1", (row["job_id"],),
    ))
    if (
        not row
        or not record
        or row["state"] != "RESERVED"
        or (record["privacy_epoch"] != epoch(store) and not (
            retained_sources and type(record.get("privacy_epoch")) is int
            and 0 <= record["privacy_epoch"] < epoch(store)))
        or record["plan_sha256"] != approval.plan_sha256
        or record["payload_sha256"] != row["payload_sha256"]
        or record["job_id"] != row["job_id"]
        or record["scenario"] != row["scenario"]
    ):
        raise CoreError("COMPANION_MODEL_SOURCE_CHANGED", 503)
    lease = row["lease_until"]
    if row["scenario"] == "social_autonomy":
        from companion.social_autonomy import SocialAutonomy

        SocialAutonomy(store, settings, clock=clock).validate_model(request_id)
    if row["job_id"]:
        job = store.one("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],))
        event = check_job(store, settings, approval, job)
        if event_digest(event) != record["event_sha256"]:
            raise CoreError("COMPANION_MODEL_SOURCE_CHANGED", 503)
        from companion.bili_video import model_binding

        reader_binding = (model_binding(store, settings, clock, request_id,
            job_id=row["job_id"], scenario=row["scenario"],
            logical_request_id=row["logical_request_id"])
            if row["scenario"] in {"content_video", "content_video_preview"} else None)
        if row["scenario"] == "content_video_preview" and reader_binding is None:
            raise CoreError("BILI_VIDEO_MODEL_BINDING_REQUIRED", 403)
        if reader_binding is not None:
            lease = min(lease, reader_binding["lease_until"])
        elif row["scenario"] in {"content_vision", "content_video"}:
            qq_images.frozen(store, settings, approval, job)
            source_request_id = request_id
            if row["scenario"] == "content_video":
                from companion import qq_video

                video, _, source_request_id = qq_video.current(store, settings, approval, job)
                if (video["request_id"] != request_id
                    or video["payload_sha256"] != row["payload_sha256"]):
                    raise CoreError("LIVE_VIDEO_SOURCE_CHANGED", 403)
            image, _, _ = qq_images.lease(
                store, settings, approval, clock, job["event_id"], request_id=source_request_id
            )
            lease = min(lease, image["lease_until"])
        else:
            if job["state"] != "RUNNING":
                raise CoreError("LIVE_TEXT_MODEL_JOB_FENCED", 409)
            lease = min(lease, job["lease_until"])
    if datetime.fromisoformat(lease) <= datetime.fromisoformat(clock()):
        raise CoreError("LIVE_TEXT_MODEL_JOB_FENCED", 409)
    return {"lease_until": lease}


def record_replies(store, settings, clock, job, message_ids):
    from companion import live_text

    approval, _, _ = live_text.guard(store, settings, clock)
    check_job(store, settings, approval, job)
    remaining = live_text.remaining_send_slots(store, approval)
    if remaining is not None and len(message_ids) > remaining:
        raise CoreError("LIVE_TEXT_SEND_ATTEMPT_CAP", 429)
    if job["kind"] == "poke":
        from companion import poke

        for message_id in message_ids:
            poke.check_delivery(store, settings, approval, message_id)
    else:
        live_text._reply_material(store, approval, message_ids[0])
    usage = store.rows(
        "SELECT request_id,state FROM model_usage WHERE job_id=? ORDER BY rowid", (job["job_id"],)
    )
    if any(row["state"] != "SETTLED" for row in usage):
        raise CoreError("LIVE_TEXT_SETTLED_REPLY_REQUIRED", 403)
    for message_id in message_ids:
        row = store.one("SELECT * FROM outbox WHERE message_id=?", (message_id,))
        value = delivery_material(store, settings, row)
        value["request_ids"] = [item["request_id"] for item in usage]
        live_text._put(store, DELIVERY + message_id, value)


def delivery_material(store, settings, row):
    from companion import notices

    event = store.one("SELECT * FROM events WHERE event_id=?", (row["origin_event_id"],))
    notice = notices.delivery_job(store, row["message_id"])
    return {
        "plan_sha256": settings.live_text_window.plan_sha256,
        "privacy_epoch": epoch(store),
        "message_id": row["message_id"],
        "event_id": row["origin_event_id"],
        "event_sha256": event_digest(event),
        "content_sha256": row["content_sha256"],
        "delivery_key": row["delivery_key"],
        "action_type": row["action_type"],
        **({"window_notice": "commitment"} if notice
           and notices.required(store, notice["job_id"]) else {}),
    }


def settled_usage_unchanged(store, request_id, plan_sha256):
    from companion.live_text import _meta

    row = store.one(
        "SELECT a.payload_sha256,m.state,m.job_id,m.scenario FROM model_admissions a "
        "JOIN model_usage m USING(request_id) WHERE request_id=?",
        (request_id,),
    )
    frozen = _meta(store, MODEL + request_id)
    return bool(
        row
        and frozen
        and row["state"] == "SETTLED"
        and frozen["plan_sha256"] == plan_sha256
        and all(frozen[key] == row[key] for key in ("job_id", "scenario", "payload_sha256"))
    )


def check_delivery(store, settings, clock, message_id, check=None):
    from companion import live_text, notices
    from companion.social import proactive_allowed, share_delivery_problem

    approval, _, _ = live_text.guard(store, settings, clock, current_delivery=message_id)
    row = store.one(
        "SELECT o.*,c.scope,c.owner_actor_id FROM outbox o "
        "JOIN conversations c USING(conversation_id) WHERE message_id=?",
        (message_id,),
    )
    if (
        not row
        or row["scope"] != "private"
        or row["owner_actor_id"] != "owner"
        or row["action_type"] not in {"qq.private.reply", "qq.private.proactive", "qq.private.poke"}
        or row["content_sha256"] != paid_budget.sha(json.loads(row["content_json"]))
    ):
        raise CoreError("COMPANION_PRIVATE_DELIVERY_REQUIRED", 403)
    poke_delivery = bool(store.one("SELECT 1 FROM poke_batches WHERE response_message_id=?",
                                   (message_id,)))
    if poke_delivery or row["action_type"] == "qq.private.poke":
        from companion import poke

        poke.check_delivery(store, settings, approval, message_id)
    if problem := notices.delivery_problem(store, settings, message_id, clock()):
        raise CoreError(problem, 403)
    expected = delivery_material(store, settings, row)
    record = live_text._meta(store, DELIVERY + message_id)
    if record is None and row["action_type"] == "qq.private.proactive":
        remaining = live_text.remaining_send_slots(store, approval)
        if remaining is not None and remaining < 1:
            raise CoreError("LIVE_TEXT_SEND_ATTEMPT_CAP", 429)
        candidate = store.one("SELECT * FROM social_candidates WHERE message_id=?", (message_id,))
        if (
            not candidate
            or share_delivery_problem(store, settings, message_id, clock())
            or not approval.starts_at
            <= datetime.fromisoformat(row["created_at"])
            < approval.ends_at
        ):
            raise CoreError("COMPANION_SHARE_SOURCE_REQUIRED", 403)
        record = expected
        live_text._put(store, DELIVERY + message_id, record)
    if (
        not record
        or any(record.get(key) != value for key, value in expected.items())
        or any(
            not settled_usage_unchanged(store, rid, approval.plan_sha256)
            for rid in record.get("request_ids", [])
        )
    ):
        raise CoreError("COMPANION_DELIVERY_SOURCE_CHANGED", 403)
    if row["action_type"] == "qq.private.reply" and not poke_delivery:
        live_text._reply_material(store, approval, message_id)
    if row["action_type"] == "qq.private.proactive" and not proactive_allowed(
        store, settings, clock()
    ):
        raise CoreError("PROACTIVE_SEND_PAUSED", 409)
    content = json.loads(row["content_json"])
    digest_matches = (check.content_sha256 == row["content_sha256"]
                      if check and row["action_type"] == "qq.private.poke"
                      else check and check.reply_sha256 == paid_budget.sha(content.get("text")))
    if check and (
        check.window_sha256 != approval.plan_sha256
        or check.event_id != row["origin_event_id"]
        or not digest_matches
        or check.lease_token != row["lease_token"]
    ):
        raise CoreError("LIVE_TEXT_SEND_CHECK_CHANGED", 403)
    return row
