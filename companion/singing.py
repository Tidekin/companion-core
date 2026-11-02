"""Source and SDK-tool custody for native local singing, without provider or send I/O."""

import hashlib
import json
import re

from companion import owner_authority, owner_input, private_life, reply_revision, voice_authority
from companion.bili_video_protocol import MAX_METADATA_BYTES
from companion.contracts import CoreError
from companion.exploration import METERED_USAGE_SQL, read_periods
from companion.goals import GoalEngine
from companion.observation import ObservationService
from companion.singing_protocol import SingingRecipe
from companion.source_grants import SourceGrantService, SourceGrantSnapshot
from companion.voice_protocol import canonical_bytes, digest


def policy(settings):
    result = settings.voice.singing
    if not settings.voice.enabled or result is None:
        raise CoreError("SINGING_DISABLED", 403)
    adapter = settings.observation_adapters.get(result.source_adapter_id)
    if (not settings.content_enabled or not settings.observation_enabled or not adapter
        or adapter.source != "bilibili"):
        raise CoreError("SINGING_SOURCE_UNAVAILABLE", 403)
    return result, adapter


def _source(store, settings, clock, job, source_event_id):
    if not store.one("SELECT 1 FROM job_sources WHERE job_id=? AND event_id=?",
                     (job["job_id"], source_event_id)):
        raise CoreError("SINGING_SOURCE_NOT_SHARED", 403)
    GoalEngine(store, settings, None, clock).validate_sources([job["event_id"], source_event_id])
    event = store.one("SELECT * FROM events WHERE event_id=? AND redacted=0", (source_event_id,))
    if not event:
        raise CoreError("SINGING_SOURCE_NOT_SHARED", 403)
    urls = []
    if event["source"] == "qq.onebot":
        view = owner_authority.check_job(store, settings, clock, job, purpose="content",
                                         executing=False)
        owner_input.source(store, settings, view, source_event_id, clock(), purpose="content",
                           history=source_event_id != job["event_id"])
        if event["conversation_id"] != job["conversation_id"]:
            raise CoreError("SINGING_SOURCE_NOT_SHARED", 403)
        urls = [row["target_url"] for row in store.rows(
            "SELECT target_url FROM content_items WHERE owner_event_id=? AND kind='link' "
            "AND redacted=0", (source_event_id,))]
    elif event["source"] == "observation.bilibili":
        observation = store.one("SELECT canonical_url FROM observations WHERE event_id=? "
                                 "AND redacted=0", (source_event_id,))
        if observation:
            url = observation["canonical_url"]
            if url.startswith("https://b23.tv/"):
                from companion.bili_video import _source as validated_video_source

                try:
                    _, observed = validated_video_source(store, settings, source_event_id, clock())
                except CoreError as error:
                    raise CoreError("SINGING_EXACT_VIDEO_REQUIRED", 400) from error
                url = "https://www.bilibili.com/video/" + observed.evidence.video.bvid
            urls = [url]
    choices = []
    for url in urls:
        match = re.fullmatch(r"https://www\.bilibili\.com/video/(BV[0-9A-Za-z]{10})/?", url)
        if match:
            choices.append(match[1])
    if len(set(choices)) != 1:
        raise CoreError("SINGING_EXACT_VIDEO_REQUIRED", 400)
    return event, choices[0]


def prepare_context(store, settings, clock, job, source_event_id, part, tool_call_id):
    options, adapter = policy(settings)
    for purpose in ("voice", "content"):
        owner_authority.check_job(store, settings, clock, job, purpose=purpose, executing=True)
    voice_authority.current(store, settings, clock, authority=json.loads(job["authority_json"]))
    reply_revision.check_job(store, job)
    source, bvid = _source(store, settings, clock, job, source_event_id)
    tool = store.one("SELECT * FROM agent_tool_calls WHERE job_id=? AND tool_call_id=?",
                      (job["job_id"], tool_call_id))
    if not tool or tool["tool_name"] != "sing_song" or tool["state"] != "STARTED":
        raise CoreError("SINGING_ORIGINAL_TOOL_REQUIRED", 403)
    usage = store.one("SELECT u.state,a.authority_json FROM model_usage u "
        "JOIN model_admissions a USING(request_id) WHERE u.request_id=? AND u.job_id=?",
        (tool["model_request_id"], job["job_id"]))
    if (not usage or usage["state"] != "SETTLED"
        or json.loads(usage["authority_json"]) != json.loads(job["authority_json"])):
        raise CoreError("SINGING_SETTLED_MODEL_REQUIRED", 403)
    # Reuse native adapter enablement and exact captured grant checks; no Cookie
    # crosses into the recipe or the local GPU process.
    ObservationService(store, settings, clock).policy(options.source_adapter_id)
    grant = SourceGrantService(store, settings, clock).require_available(
        options.source_adapter_id) if adapter.source_grant_required else None
    recipe = SingingRecipe(bvid=bvid, part=part, source_event_id=source_event_id,
        source_sha256=private_life.event_digest(source), chat_job_id=job["job_id"],
        tool_call_id=tool_call_id, max_seconds=options.max_seconds,
        max_input_bytes=options.max_input_bytes)
    return {"recipe": recipe.model_dump(mode="json"), "source_grant": grant,
            "model_request_id": tool["model_request_id"], "args_sha256": tool["args_sha256"]}


def reserve_read(store, settings, now, max_bytes):
    day, month = read_periods(settings, now)
    counts = store.one("SELECT SUM(accounted_requests) monthly,"
        "SUM(CASE WHEN started_at>=? THEN accounted_requests ELSE 0 END) daily,"
        "SUM(CASE WHEN started_at>=? THEN accounted_bytes ELSE 0 END) bytes,"
        "SUM(CASE WHEN started_at>=? AND operation='read' THEN 1 ELSE 0 END) reads "
        f"FROM ({METERED_USAGE_SQL}) WHERE started_at>=?", (day, day, day, month))
    reserved = max_bytes + MAX_METADATA_BYTES
    if ((counts["daily"] or 0) + 4 > settings.exploration_daily_requests
        or (counts["monthly"] or 0) + 4 > settings.exploration_monthly_requests
        or (counts["bytes"] or 0) + reserved > settings.exploration_daily_bytes
        or (counts["reads"] or 0) >= settings.exploration_daily_deep_reads):
        raise CoreError("SINGING_READ_BUDGET_UNAVAILABLE", 429)
    return dict(source_budget_at=now, source_reserved_requests=4, source_reserved_bytes=reserved)


def check_source(store, settings, clock, row):
    options, adapter = policy(settings)
    try:
        context = json.loads(row["singing_context_json"])
        recipe = SingingRecipe.model_validate(context["recipe"])
    except (ValueError, KeyError, TypeError) as error:
        raise CoreError("SINGING_ORIGINAL_SOURCE_CHANGED", 403) from error
    if (set(context) != {"recipe", "source_grant", "model_request_id", "args_sha256"}
        or hashlib.sha256(canonical_bytes(recipe)).hexdigest() != row["input_sha256"]
        or digest(context) != row["source_proof_sha256"]):
        raise CoreError("SINGING_ORIGINAL_SOURCE_CHANGED", 403)
    job = store.one("SELECT j.*,e.conversation_id,e.body_json,e.reply_revision "
        "FROM jobs j JOIN events e USING(event_id) WHERE job_id=?", (recipe.chat_job_id,))
    if (not job or job["event_id"] != row["event_id"] or job["kind"] != "chat"
        or job["state"] not in {"RUNNING", "RETRY_WAIT", "SUCCEEDED"}
        or json.loads(job["authority_json"]) != json.loads(row["authority_json"])):
        raise CoreError("SINGING_ORIGINAL_SOURCE_CHANGED", 403)
    for purpose in ("voice", "content"):
        owner_authority.check_job(store, settings, clock, job, purpose=purpose, executing=False)
    reply_revision.check_job(store, job)
    source, bvid = _source(store, settings, clock, job, recipe.source_event_id)
    if bvid != recipe.bvid or private_life.event_digest(source) != recipe.source_sha256:
        raise CoreError("SINGING_ORIGINAL_SOURCE_CHANGED", 403)
    if row["state"] in {"QUEUED", "LEASED", "RUNNING"}:
        from companion.agent_reading import revalidate_results

        saved = store.one("SELECT snapshots_json FROM agent_conversations WHERE job_id=?",
                           (recipe.chat_job_id,))
        if saved:
            revalidate_results(store, settings, clock, job,
                               json.loads(saved["snapshots_json"]), executing=False)
    tool = store.one("SELECT * FROM agent_tool_calls WHERE job_id=? AND tool_call_id=?",
                     (recipe.chat_job_id, recipe.tool_call_id))
    if (not tool or tool["tool_name"] != "sing_song" or tool["voice_job_id"] != row["job_id"]
        or tool["state"] not in {"DEFERRED", "RETURNED"}
        or tool["model_request_id"] != context["model_request_id"]
        or tool["args_sha256"] != context["args_sha256"]
        or not store.one("SELECT 1 FROM model_usage WHERE request_id=? AND job_id=? "
                         "AND state='SETTLED'", (context["model_request_id"], recipe.chat_job_id))):
        raise CoreError("SINGING_ORIGINAL_TOOL_REQUIRED", 403)
    if (recipe.max_seconds > options.max_seconds
        or recipe.max_input_bytes > options.max_input_bytes):
        raise CoreError("SINGING_ORIGINAL_SOURCE_CHANGED", 403)
    if row["state"] in {"QUEUED", "LEASED", "RUNNING"}:
        if context["source_grant"] or adapter.source_grant_required:
            SourceGrantService(store, settings, clock).require(options.source_adapter_id,
                SourceGrantSnapshot.model_validate(context["source_grant"])
                if context["source_grant"] else None)
    owner = store.one("SELECT * FROM events WHERE event_id=?", (row["event_id"],))
    if not owner or owner["redacted"]:
        raise CoreError("SINGING_ORIGINAL_SOURCE_CHANGED", 403)
    return owner, recipe
