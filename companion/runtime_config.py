"""Task-pinned runtime values over the startup configuration's unchanged cold identity.

Reviewed chat expression, batch timing and image provider values are dynamic. Services
and ModelClient retain their original settings, clients and stores. No permission, source,
budget or delivery check is cached.
"""

import asyncio
import hashlib
import hmac
import json
from contextlib import nullcontext
from typing import Literal

import psycopg
from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field

from companion import operator_config, shutdown
from companion.contracts import CoreError
from companion.conversation_policy import ConversationPolicyConfig
from companion.input_debounce import DebounceTiming
from companion.media_config import ImageGenerationConfig
from companion.store import encode
from companion.style_live_config import StyleLiveConfig
from companion.video_input import VideoModels

EXPRESSION_FIELDS = frozenset({"chat_time_labels", "conversation_policy", "style_live",
                               "conversation_guidance", "image_generation"})
HOT_FIELDS = EXPRESSION_FIELDS | {"turn_taking", "input_debounce", "video_models"}
HOT_OBJECT_FIELDS = {"input_debounce": frozenset(DebounceTiming.model_fields),
                     "turn_taking": frozenset({"silence_enabled"}),
                     "conversation_policy": frozenset({"mode", "strategy"}),
                     "style_live": frozenset({"enabled"})}
REVIEW_FIELDS = frozenset({
    "owner_runtime", "model_url", "model_name", "model_key", "video_model_key", "provider",
    "input_rate", "cached_input_rate", "output_rate", "max_output_tokens", "chat_thinking",
    "chat_thinking_tokens", "segmented_reply", "token_measurement", "rich_input_scope",
    "memory_extract_max_output_tokens",
    "raw_event_retention_days", "private_files", "voice", "social_autonomy", "bili_video",
    "rem", "poke_policy", "content", "meme_catalog_owner", "meme_trusted_sources",
    "meme_gallery_source", "meme_excluded_sha256",
    "meme_cooldown_seconds", "expression_enabled", "meme_discovery_enabled",
    "meme_semantics_enabled", "meme_discovery_daily_queries",
    "meme_discovery_daily_download_bytes", "meme_discovery_daily_storage_bytes",
    "expression_selection_probability", "expression_cooldown_minutes", "expression_meme_categories",
})
PREFIX = "operator_configuration:"


class ExpressionValues(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    conversation_policy: ConversationPolicyConfig
    style_live_enabled: bool
    conversation_guidance: str | None = Field(max_length=2000)


class StoredExpression(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    format_version: Literal[2, 3, 4, 5]
    silence_enabled: bool | None = None
    debounce: DebounceTiming | None = None
    conversation_policy: ConversationPolicyConfig
    style_live_enabled: bool
    encrypted_guidance: str | None
    # Format 3: the complete image provider choice, including its key, encrypted.
    encrypted_image_generation: str | None = None
    encrypted_video_models: str | None = None


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: int = Field(ge=0)
    base_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chat_time_labels: bool
    format_version: Literal[1, 2, 3, 4, 5]
    silence_enabled: bool
    debounce: DebounceTiming
    expression: ExpressionValues
    image_generation: ImageGenerationConfig | None = None
    video_models: VideoModels | None = None


def _digest(settings, value):
    return hmac.new(settings.control_token.get_secret_value().encode(),
                    ("companion/runtime-config/v1:" + encode(value)).encode(),
                    hashlib.sha256).hexdigest()


def _cold(settings, format_version=4):
    values = operator_config._raw(settings)
    # Defaults predate gallery/isolation support in retained signed snapshots.
    for name in ("meme_gallery_source", "meme_excluded_sha256"):
        if not values.get(name):
            values.pop(name, None)
    # Unused video options did not exist in prior signed cold identities.
    values.pop("video_models", None)
    if values.get("paid_model") and not values["paid_model"].get("video_routes"):
        values["paid_model"].pop("video_routes", None)
    hot = HOT_FIELDS - {"video_models"}
    if format_version == 1:
        hot = {"chat_time_labels", "conversation_guidance"}
    elif format_version < 4:
        hot = EXPRESSION_FIELDS
    for key in hot | {"operator_hot_reload"}:
        if key in HOT_OBJECT_FIELDS:
            values[key] = {name: value for name, value in values[key].items()
                           if name not in HOT_OBJECT_FIELDS[key]}
            if values[key]:
                continue
        values.pop(key)
    base = json.loads(settings._operator_config_base) if settings._operator_config_base else (
        operator_config._raw(settings))
    if format_version == 1:
        values["deployment_chat_time_labels"] = base["chat_time_labels"]
    else:
        values["deployment_expression"] = {key: base.get(key) for key in sorted(hot)}
    return _digest(settings, values)


def _expression(settings):
    return ExpressionValues(conversation_policy=settings.conversation_policy,
        style_live_enabled=settings.style_live.enabled,
        conversation_guidance=settings.conversation_guidance)


def _snapshot(settings, version=None, *, candidate=None):
    candidate = candidate or settings
    # The startup provider needs no snapshot copy; only a Console change is pinned.
    image = (candidate.image_generation
             if candidate.image_generation != settings.image_generation else None)
    videos = candidate.video_models if candidate.video_models.candidates else None
    return Snapshot(version=settings._operator_config_version if version is None else version,
                    base_sha256=_cold(settings),
                    chat_time_labels=candidate.chat_time_labels,
                    format_version=5 if videos else 4, video_models=videos,
                    silence_enabled=candidate.turn_taking.silence_enabled,
                    debounce=candidate.input_debounce.timing(),
                    expression=_expression(candidate), image_generation=image)


def identity(settings, snapshot):
    value = snapshot.model_dump()
    if snapshot.format_version < 5:
        if value.pop("video_models") is not None:
            raise CoreError("CONFIGURATION_SNAPSHOT_INVALID", 409)
    else:
        value["video_models"] = operator_config._raw(snapshot.video_models)
    if snapshot.format_version < 4:
        value.pop("silence_enabled")
        value.pop("debounce")
    if snapshot.format_version == 1:
        # Original evidence is never re-signed. Its cold hash authenticates these
        # reconstructed expression values instead of a new payload field.
        value = {key: value[key] for key in ("version", "base_sha256", "chat_time_labels")}
    elif snapshot.format_version == 2:
        if value.pop("image_generation") is not None:
            raise CoreError("CONFIGURATION_SNAPSHOT_INVALID", 409)
    else:
        # The HMAC covers the actual credential, so a key change is a new snapshot.
        value["image_generation"] = operator_config._raw(snapshot.image_generation)
    return _digest(settings, value)


def values(settings, snapshot):
    expression = snapshot.expression
    result = {"chat_time_labels": snapshot.chat_time_labels,
              "video_models": snapshot.video_models or VideoModels(),
              "input_debounce": settings.input_debounce.model_copy(
                  update=snapshot.debounce.model_dump()),
              "turn_taking": settings.turn_taking.model_copy(update={
                  "silence_enabled": snapshot.silence_enabled}),
              "conversation_policy": expression.conversation_policy,
              "style_live": StyleLiveConfig(enabled=expression.style_live_enabled),
              "conversation_guidance": expression.conversation_guidance}
    if snapshot.image_generation is not None:
        result["image_generation"] = snapshot.image_generation
    return result


def effective(settings, snapshot):
    if snapshot is None:
        return settings
    return settings.model_copy(update=values(settings, snapshot))


def classify(before, after):
    old, new = operator_config._raw(before), operator_config._raw(after)
    changed = sorted(key for key in old if old[key] != new[key])
    restart = set(changed) - HOT_FIELDS
    for key, leaves in HOT_OBJECT_FIELDS.items():
        if {k: v for k, v in old[key].items() if k not in leaves} != {
            k: v for k, v in new[key].items() if k not in leaves
        }:
            restart.add(key)
    return {"changed_fields": changed,
            "restart_fields": sorted(restart),
            "review_fields": sorted(set(changed) & REVIEW_FIELDS)}


def _persist(store, settings, snapshot, now):
    key = identity(settings, snapshot)
    if store.one('SELECT 1 FROM operator_runtime_snapshots WHERE snapshot_id = %s', (key,)):
        _read(store, settings, key)
        return key
    conflict = store.one('SELECT snapshot_id FROM operator_runtime_snapshots WHERE version = %s '
        'AND base_sha256 = %s AND snapshot_id <> %s',
                         (snapshot.version, snapshot.base_sha256, key))
    if conflict:
        raise CoreError("CONFIGURATION_VERSION_CONFLICT", 409)
    expression, image = snapshot.expression, snapshot.image_generation
    videos = snapshot.video_models
    stored = StoredExpression(format_version=max(snapshot.format_version, 2),
        silence_enabled=snapshot.silence_enabled if snapshot.format_version >= 4 else None,
        debounce=snapshot.debounce if snapshot.format_version >= 4 else None,
        conversation_policy=expression.conversation_policy,
        style_live_enabled=expression.style_live_enabled,
        encrypted_guidance=operator_config._cipher(settings).encrypt(
            expression.conversation_guidance.encode()).decode()
        if expression.conversation_guidance is not None else None,
        encrypted_image_generation=operator_config._cipher(settings).encrypt(
            encode(operator_config._raw(image)).encode()).decode()
        if image is not None else None,
        encrypted_video_models=operator_config._cipher(settings).encrypt(
            encode(operator_config._raw(videos)).encode()).decode() if videos is not None else None)
    store.execute(
        "INSERT INTO operator_runtime_snapshots "
        "(snapshot_id,version,base_sha256,chat_time_labels,created_at,expression_json) "
        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(snapshot_id) DO NOTHING",
        (key, snapshot.version, snapshot.base_sha256, int(snapshot.chat_time_labels), now,
         stored.model_dump_json() if snapshot.format_version >= 2 else None),
    )
    # Another worker may have committed this exact identity after our first read.
    # Validate its signed record and retain its original encryption and timestamp.
    _read(store, settings, key)
    return key


def _read(store, settings, key):
    row = store.one('SELECT operator_runtime_snapshots.snapshot_id AS snapshot_id, '
        'operator_runtime_snapshots.version AS version, operator_runtime_snapshots.base_sha256 AS '
        'base_sha256, operator_runtime_snapshots.chat_time_labels AS chat_time_labels, '
        'operator_runtime_snapshots.created_at AS created_at, '
        'operator_runtime_snapshots.expression_json AS expression_json FROM '
        'operator_runtime_snapshots AS operator_runtime_snapshots WHERE '
        'operator_runtime_snapshots.snapshot_id = %s', (key,))
    return _validated_snapshot(store, settings, row)


def _validated_snapshot(store, settings, row):
    return validate_record(settings, row, lookup=lambda version: store.one(
        'SELECT operator_configuration.version AS version, operator_configuration.request_id AS '
        'request_id, operator_configuration.request_sha256 AS request_sha256, '
        'operator_configuration.encrypted_overrides AS encrypted_overrides, '
        'operator_configuration.fields_json AS fields_json, operator_configuration.created_at AS '
        'created_at FROM operator_configuration AS operator_configuration WHERE '
        'operator_configuration.version = %s', (version,)))


def validate_record(settings, row, *, lookup=None):
    """Reconstruct only signed safe leaves under the same validated startup identity."""
    if not row:
        raise CoreError("CONFIGURATION_SNAPSHOT_MISSING", 409)
    row = dict(row)
    key, legacy = row["snapshot_id"], row.get("expression_json") is None
    image = videos = None
    try:
        stored = None if legacy else StoredExpression.model_validate_json(row["expression_json"])
        version = stored.format_version if stored else 1
        original = settings
        cold = _cold(settings, version)
        if version < 4 and cold != row["base_sha256"]:
            # Recover newly hot leaves from the original version. The old cold HMAC
            # still has to verify with today's credentials, budget and permissions.
            base = (type(settings).model_validate_json(settings._operator_config_base)
                    if settings._operator_config_base else settings)
            record = lookup(row["version"]) if lookup and row["version"] else None
            if row["version"] and record is None:
                raise CoreError("CONFIGURATION_STARTUP_IDENTITY_CHANGED", 409)
            historical = operator_config._candidate(base, operator_config._decode(record, settings))
            times = historical.input_debounce.timing().model_dump()
            updates = {
                "input_debounce": settings.input_debounce.model_copy(update=times),
                "turn_taking": settings.turn_taking.model_copy(update={
                    "silence_enabled": historical.turn_taking.silence_enabled}),
            }
            if legacy:
                updates.update(conversation_policy=historical.conversation_policy,
                               style_live=historical.style_live)
            original = settings.model_copy(update=updates)
            cold = _cold(original, version)
        silence = original.turn_taking.silence_enabled
        timing = original.input_debounce.timing()
        if legacy:
            expression = _expression(original).model_copy(update={"conversation_guidance": None})
        else:
            guidance = (operator_config._cipher(settings).decrypt(
                stored.encrypted_guidance.encode()).decode()
                if stored.encrypted_guidance is not None else None)
            expression = ExpressionValues(conversation_policy=stored.conversation_policy,
                style_live_enabled=stored.style_live_enabled, conversation_guidance=guidance)
            image = (ImageGenerationConfig.model_validate_json(operator_config._cipher(settings)
                     .decrypt(stored.encrypted_image_generation.encode()))
                     if stored.encrypted_image_generation is not None else None)
            if stored.encrypted_video_models is not None:
                videos = VideoModels.model_validate_json(operator_config._cipher(settings)
                    .decrypt(stored.encrypted_video_models.encode()))
                videos.selected_route(settings.paid_model)
            if (version == 5) != (videos is not None):
                raise ValueError
            if version < 4 and (version == 3) != (image is not None):
                raise ValueError
            if (version >= 4) != (stored.silence_enabled is not None):
                raise ValueError
            if (version >= 4) != (stored.debounce is not None):
                raise ValueError
            if version >= 4:
                silence, timing = stored.silence_enabled, stored.debounce
        snapshot = Snapshot(version=row["version"], base_sha256=row["base_sha256"],
                            chat_time_labels=bool(row["chat_time_labels"]),
                            format_version=version, silence_enabled=silence, debounce=timing,
                            expression=expression, image_generation=image, video_models=videos)
        if row["chat_time_labels"] not in (0, 1) or not hmac.compare_digest(
            identity(settings, snapshot), key,
        ):
            raise ValueError
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        raise CoreError("CONFIGURATION_SNAPSHOT_INVALID", 409) from None
    if snapshot.base_sha256 != cold:
        raise CoreError("CONFIGURATION_STARTUP_IDENTITY_CHANGED", 409)
    return snapshot


def boundary(store, settings, clock, *, worker=None):
    """Publish a complete validated version only between native units, transactionally."""
    worker = worker or getattr(store, "configuration_worker", "runtime")
    key = PREFIX + worker

    def publish():
        now, current = clock(), _snapshot(settings)
        prior = store.one('SELECT value FROM runtime_meta WHERE key = %s', (key,))
        previous = {}
        if prior:
            try:
                previous = json.loads(prior["value"])
                current = _read(store, settings, previous["snapshot_id"])
            except (CoreError, KeyError, ValueError, TypeError):
                previous = {}  # New startup identities cannot inherit the old process state.
        saved = operator_config.latest(store)
        version = saved["version"] if saved else 0
        reason, classification = settings._operator_config_error, {}
        try:
            if not settings.operator_hot_reload:
                current = _snapshot(settings)
                reason = "CONFIGURATION_HOT_RELOAD_DISABLED" if version != current.version else None
            elif saved:
                candidate = operator_config._candidate(
                    settings, operator_config._decode(saved, settings))
                classification = classify(settings, candidate)
                if classification["restart_fields"]:
                    reason = ("CONFIGURATION_REAPPROVAL_REQUIRED" if classification["review_fields"]
                              else "CONFIGURATION_RESTART_REQUIRED")
                else:
                    # A safe leaf cannot change any startup field. Keep the concrete
                    # domain fingerprints as an additional guard against future coupling.
                    from companion import owner_authority, paid_budget

                    if owner_authority.capability_hashes(settings) != (
                        owner_authority.capability_hashes(candidate)
                    ) or settings.paid_model and paid_budget.snapshot(settings) != (
                        paid_budget.snapshot(candidate)
                    ):
                        raise CoreError("CONFIGURATION_SECURITY_IDENTITY_CHANGED", 409)
                    if version != current.version and owner_authority.required(store, settings):
                        owner_authority.current(store, settings, clock)
                    proposed = _snapshot(settings, version, candidate=candidate)
                    _persist(store, settings, proposed, now)
                    current, reason = proposed, None
        except CoreError as error:
            if store.in_read_snapshot:
                raise  # A refusal seen only in a snapshot is decided again under the lock.
            reason = error.code
        snapshot_id = _persist(store, settings, current, now)
        state = {"worker": worker, "startup_version": settings._operator_config_version,
                 "active_version": current.version, "snapshot_id": snapshot_id,
                 "saved_version": version,
                 "pending_version": version if version != current.version else None,
                 "reason": reason, "hot_reload_enabled": settings.operator_hot_reload,
                 "observed_at": now, **classification}
        if {k: v for k, v in state.items() if k != "observed_at"} != {
            k: v for k, v in previous.items() if k != "observed_at"
        }:
            store.execute('INSERT INTO runtime_meta (key, value) VALUES (%s, %s) ON CONFLICT(key) '
                'DO UPDATE SET value = excluded.value, record_sequence = '
                'DEFAULT', (key, encode(state)))
        return current

    if store.in_write_transaction:
        return publish()
    # Workers pass here before every native unit; an unchanged state needs no writer lock.
    return store.read_or_write(publish)


async def worker_boundary(store, settings, clock):
    """Wait out PostgreSQL lock contention before starting a native worker unit.

    Only the configuration transaction is retried, after rollback. Business work,
    provider calls and their uncertainty remain outside this retry boundary.
    """
    while not shutdown.requested():
        try:
            boundary(store, settings, clock)
            return True
        except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected,
                    psycopg.errors.SerializationFailure):
            if store.in_transaction:
                raise
            await asyncio.sleep(0.1)
    return False


def workers(store, settings):
    result = []
    for row in store.rows('SELECT value FROM runtime_meta WHERE key LIKE %s ORDER BY key NULLS '
        'FIRST',
                          (PREFIX + "%",)):
        try:
            state = json.loads(row["value"])
            snapshot = _read(store, settings, state["snapshot_id"])
            if snapshot.version != state["active_version"]:
                raise ValueError
            result.append(state)
        except (CoreError, KeyError, ValueError, TypeError):
            # Status from a different deployment is not evidence of this one's activation.
            continue
    return result


_TASK_SQL = (
    'SELECT to_jsonb(binding) AS binding, to_jsonb(snapshot) AS snapshot '
    'FROM operator_task_configuration AS binding '
    'LEFT JOIN operator_runtime_snapshots AS snapshot '
    'ON snapshot.snapshot_id = binding.snapshot_id WHERE binding.task_key = %s'
)


def get(store, settings, task_key):
    row = store.one(
        _TASK_SQL, (task_key,))
    if row is None:
        return None
    return _bound_snapshot(store, settings, task_key, row["binding"], row["snapshot"])


def _bound_snapshot(store, settings, task_key, row, snapshot):
    if row is None:
        return None
    if not isinstance(row["binding_sha256"], str) or not hmac.compare_digest(
        row["binding_sha256"], _digest(settings, [task_key, row["snapshot_id"]]),
    ):
        raise CoreError("CONFIGURATION_TASK_BINDING_INVALID", 409)
    return _validated_snapshot(store, settings, snapshot)


def bind(store, settings, clock, task_key, *, snapshot=None):
    with nullcontext() if store.in_write_transaction else store.transaction():
        existing = get(store, settings, task_key)
        if existing is not None:
            if snapshot is not None and existing != snapshot:
                raise CoreError("CONFIGURATION_TASK_VERSION_CHANGED", 409)
            return existing
        if not settings.operator_hot_reload and snapshot is None:
            return None
        if task_key.startswith("job:") and store.one(
            'SELECT 1 FROM model_usage WHERE job_id = %s UNION ALL SELECT 1 FROM '
            'agent_conversations WHERE job_id = %s LIMIT 1',
            (task_key[4:], task_key[4:]),
        ):
            raise CoreError("CONFIGURATION_TASK_BINDING_REQUIRED", 409)
        snapshot = snapshot or boundary(store, settings, clock)
        key = _persist(store, settings, snapshot, clock())
        store.insert("operator_task_configuration", task_key=task_key, snapshot_id=key,
                     binding_sha256=_digest(settings, [task_key, key]), created_at=clock())
        return snapshot


def check(store, settings, task_key):
    return _checked_task(store, settings, task_key, store.one(_TASK_SQL, (task_key,)))


def _checked_task(store, settings, task_key, loaded):
    """Validate a binding and its signed snapshot loaded at the current guard boundary."""
    snapshot = (_bound_snapshot(store, settings, task_key, loaded["binding"], loaded["snapshot"])
                if loaded is not None else None)
    if snapshot is None and settings.operator_hot_reload:
        raise CoreError("CONFIGURATION_TASK_BINDING_REQUIRED", 409)
    return snapshot


def for_context(store, settings, job, snapshots):
    snapshot = check(store, settings, "job:" + job["job_id"])
    if snapshot is None:
        return settings
    if snapshots is not None:
        marker = {"kind": "runtime_configuration", "snapshot_id": identity(settings, snapshot)}
        entries = [item for item in snapshots if item["kind"] == marker["kind"]]
        if entries and entries != [marker]:
            raise CoreError("CONFIGURATION_CONTEXT_CHANGED", 409)
        if not entries:
            snapshots.append(marker)
    return effective(settings, snapshot)


def video_settings(store, settings, task_key, scenario, *, clock=None):
    """Resolve transport and pricing for a pinned task; authority keeps its startup contract."""
    from companion.video_input import SCENARIOS

    if scenario not in SCENARIOS or not settings.paid_model or not settings.paid_model.video_routes:
        return settings
    snapshot = (bind(store, settings, clock, task_key) if clock is not None
                else check(store, settings, task_key))
    models = (snapshot.video_models or VideoModels()) if snapshot else settings.video_models
    try:
        selected = models.selected_route(settings.paid_model)
    except ValueError:
        raise CoreError("VIDEO_MODEL_APPROVAL_REQUIRED", 403) from None
    if selected is None:
        return settings
    route = settings.paid_model.video_input.model_copy(update=selected.model_dump(exclude={"key"}))
    return settings.model_copy(update={"paid_model": settings.paid_model.model_copy(
        update={"video_input": route}), "video_model_key": selected.key, "video_models": models})


def check_request(store, settings, request_id):
    row = store.one(
        'SELECT usage.job_id, to_jsonb(task) AS task, to_jsonb(task_snapshot) AS task_snapshot, '
        'to_jsonb(request) AS request, to_jsonb(request_snapshot) AS request_snapshot '
        'FROM model_usage AS usage '
        'LEFT JOIN operator_task_configuration AS task ON task.task_key = \'job:\' || usage.job_id '
        'LEFT JOIN operator_runtime_snapshots AS task_snapshot '
        'ON task_snapshot.snapshot_id = task.snapshot_id '
        'LEFT JOIN operator_task_configuration AS request ON request.task_key = \'model:\' || %s '
        'LEFT JOIN operator_runtime_snapshots AS request_snapshot '
        'ON request_snapshot.snapshot_id = request.snapshot_id WHERE usage.request_id = %s',
        (request_id, request_id))
    if row is None or row["job_id"] is None:
        return
    snapshots = []
    for name, key in (("task", "job:" + row["job_id"]), ("request", "model:" + request_id)):
        snapshot = _bound_snapshot(store, settings, key, row[name], row[name + "_snapshot"])
        if snapshot is None and settings.operator_hot_reload:
            raise CoreError("CONFIGURATION_TASK_BINDING_REQUIRED", 409)
        snapshots.append(snapshot)
    task, request = snapshots
    if task != request:
        raise CoreError("CONFIGURATION_REQUEST_CHANGED", 409)
