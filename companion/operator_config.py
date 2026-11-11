"""Encrypted owner configuration versions; runtime admission lives in runtime_config."""

import base64
import hashlib
import hmac
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from urllib.parse import urlsplit

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)

from companion.contracts import CoreError, StrictModel
from companion.postgres import Database
from companion.store import encode


class ConsoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    public_origin: str = ""
    session_seconds: int = Field(default=3600, ge=300, le=86400)
    max_sessions: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def valid_origin(self):
        if not self.public_origin:
            if self.enabled:
                raise ValueError("enabled console requires an explicit HTTPS public_origin")
            return self
        url = AnyHttpUrl(self.public_origin)
        if (url.scheme != "https" or url.username is not None or url.password is not None
            or urlsplit(self.public_origin).username is not None
            or url.path not in {"", "/"} or url.query is not None or url.fragment is not None):
            raise ValueError("console requires an HTTPS origin without a path or credentials")
        port = f":{url.port}" if url.port != 443 else ""
        self.public_origin = f"https://{url.host}{port}"
        return self


class ConfigurationChange(StrictModel):
    expected_version: int = Field(ge=0)
    changes: dict = Field(default_factory=dict, max_length=100)
    restore_version: int | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def one_operation(self):
        if bool(self.changes) == (self.restore_version is not None):
            raise ValueError("provide changes or a historical version")
        return self


# Identity, ledger locations and signed approvals are deployment custody, not browser inputs.
EXCLUDED = frozenset({
    "owner_qq_id", "bot_qq_id", "bridge_token", "diary_admin_token", "control_token",
    "database", "state_directory", "erasure_ledger", "embedding_cache_dir", "paid_model",
    "live_text_window",
    "observation_adapters", "executive", "media", "cloud_budget", "timezone", "mode",
    "diary_auto_share", "reranker_model_dir",
    "operator_hot_reload",
})
LABELS = {
    "chat_thinking": "聊天思考强度", "chat_thinking_tokens": "思考额外输出（token）",
    "chat_time_labels": "对话里标注时间",
    "input_debounce": "连发消息合并回复", "turn_taking": "话轮与主动接话",
    "conversation_policy": "对话策略",
    "conversation_guidance": "交流指导正文",
    "quote_policy": "回复时引用原消息", "style_live": "聊天时参考已批准的表达",
    "style_distill": "从评论提炼表达候选",
    "jargon": "学习主人的黑话", "meme_wiki": "梗百科（查梗与接梗）",
    "watch_ledger": "观看记录与看法",
    "rem": "夜间 REM 记录", "life_loop": "生活话题循环",
    "evaluation_enabled": "对话评测实验", "owner_runtime": "主人授权运行模式",
    "token_measurement": "Token 精确计数",
    "meme_catalog_owner": "表情目录归属", "meme_cooldown_seconds": "表情素材冷却（秒）",
    "meme_trusted_sources": "可信表情来源",
    "meme_gallery_source": "梗鲸导入清单", "meme_excluded_sha256": "隔离的表情素材",
    "model_url": "聊天模型服务地址", "model_name": "聊天模型名称", "model_key": "聊天模型密钥",
    "video_models": "视频理解模型",
    "video_model_key": "默认视频模型密钥", "provider": "模型服务类型",
    "input_rate": "输入价格（微美元／百万 token）",
    "cached_input_rate": "缓存输入价格（微美元／百万 token）",
    "output_rate": "输出价格（微美元／百万 token）", "max_output_tokens": "最大回复 token",
    "timeout_seconds": "模型请求超时（秒）", "content": "阅读与图片限制",
    "image_generation": "AI 生图（接口地址、密钥与额度）",
    "private_files": "私聊文件解析与资源限制",
    "bili_video": "B站视频抽样与片段分析",
    "console": "控制台与公网地址", "segmented_reply": "分段回复",
    "poke_policy": "拍一拍回应",
    "voice": "本地语音节点与资源限制", "social_autonomy": "QQ空间与账号资料自主行为",
    "expression_enabled": "自动选择表情", "expression_learning_enabled": "表达习惯学习",
    "expression_selection_probability": "表情选择概率",
    "expression_cooldown_minutes": "表情使用间隔（分钟）",
    "diary_enabled": "私人日记", "memory_enabled": "长期记忆",
    "exploration_enabled": "自主阅读", "proactive_enabled": "主动分享",
    "context_soft_tokens": "对话上下文软上限（token）",
    "max_recent_messages": "最近消息数量上限", "rich_input_scope": "富媒体输入范围",
    "memory_recall_limit": "记忆召回条数上限",
    "lexical_keep_rank": "关键词召回保留名次",
    "turn_recall_limit": "历史消息召回条数上限",
    "turn_recall_scan_turns": "历史消息扫描轮数上限",
    "turn_recall_rerank": "历史消息交叉编码重排",
    "memory_rerank": "记忆交叉编码重排",
    "memory_required_hits": "优先保留的记忆条数",
    "relationship_standing_limit": "每轮保留的已确认长期关系条数",
    "relationship_standing_chars": "每轮长期关系内容上限（字符）",
    "recent_protected_turns": "优先保留的最近完整对话轮数",
    "turn_recall_history_share": "历史消息召回占用比例",
    "turn_recall_second_hop": "历史消息二次检索条数",
    "recall_compute_timeout_seconds": "召回计算等待上限（秒）",
    "chat_restore_timeout_seconds": "对话恢复总等待上限（秒）",
    "diary_generate_hour": "日记整理时刻（小时）",
    "diary_min_source_events": "日记最少来源事件数",
    "diary_max_source_events": "日记最多来源事件数",
    "diary_max_output_tokens": "日记输出上限（token）",
    "diary_max_body_chars": "日记正文上限（字符）",
    "diary_catchup_days": "日记补写范围（天）",
    "diary_max_attempts": "日记最大尝试次数",
    "content_enabled": "分享内容读取", "web_reading_enabled": "指定网站读取",
    "qzone_reading_enabled": "授权读取 QQ 空间正文",
    "observation_enabled": "来源观察", "voice_enabled": "语音输入",
    "camera_enabled": "摄像头观察", "android_enabled": "Android 观察",
    "mood_enabled": "小肥鱼的动态心情",
    "affect_enabled": "互动状态调整", "affect_decay_minutes": "互动状态有效期（分钟）",
    "affect_max_evidence": "互动状态证据上限",
    "consolidation_enabled": "记忆整理", "consolidation_local_hour": "记忆整理时刻（小时）",
    "consolidation_catchup_days": "记忆补整理范围（天）",
    "reflection_enabled": "来源支持的反思",
    "reflection_daily_max_suggestions": "每日反思建议上限",
    "reflection_min_sources": "反思最少来源数",
    "reflection_max_output_tokens": "反思输出上限（token）",
    "reflection_max_daily_calls": "每日反思调用上限",
    "goals_enabled": "目标与承诺", "goals_max_active": "活动目标数量上限",
    "goal_max_attempts": "目标最大尝试次数", "wake_enabled": "自主活动调度",
    "wake_min_interval_minutes": "自主活动最小间隔（分钟）",
    "wake_max_daily_runs": "每日自主活动上限",
    "exploration_daily_candidate_limit": "每日发现候选上限",
    "exploration_daily_deep_reads": "每日深入阅读上限",
    "exploration_daily_requests": "阅读每日网络请求上限",
    "exploration_monthly_requests": "阅读每月网络请求上限",
    "exploration_max_response_bytes": "单次读取响应上限（字节）",
    "exploration_daily_bytes": "每日阅读流量上限（字节）",
    "social_enabled": "阅读内容分享", "social_semantic_threshold": "内容分享相似度阈值",
    "meme_discovery_enabled": "发现表情素材", "meme_semantics_enabled": "表情素材理解",
    "meme_caption_max_output_tokens": "表情理解输出上限（token）",
    "meme_discovery_daily_queries": "每日表情发现次数上限",
    "meme_discovery_daily_download_bytes": "每日表情下载上限（字节）",
    "meme_discovery_daily_storage_bytes": "每日表情新增存储上限（字节）",
    "expression_meme_categories": "允许的表情素材分类",
    "web_reading_hosts": "允许读取的网站",
    "semantic_min_score": "记忆相似度下限", "extraction_min_events": "记忆提取最少事件数",
    "extraction_idle_seconds": "记忆提取空闲等待（秒）",
    "memory_extract_max_output_tokens": "记忆提取输出上限（token）",
    "emerging_min_sessions": "初步偏好最少独立会话数",
    "stable_min_days": "稳定偏好最少观察天数",
    "proactive_interval_seconds": "主动分享最小间隔（秒）",
    "proactive_idle_seconds": "主动分享空闲等待（秒）",
    "proactive_daily_limit": "每日主动分享上限",
    "quiet_start": "安静时段开始（小时）", "quiet_end": "安静时段结束（小时）",
    "lease_seconds": "任务领取有效期（秒）", "max_attempts": "任务最大尝试次数",
    "raw_event_retention_days": "原始事件保留期（天）",
}


def _cipher(settings):
    token = settings.control_token.get_secret_value()
    if len(token) < 24:
        raise CoreError("CONSOLE_OWNER_CREDENTIAL_REQUIRED", 503)
    key = hashlib.sha256(("companion/operator-config/v1:" + token).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _decode(row, settings):
    if not row:
        return {}
    try:
        data = json.loads(_cipher(settings).decrypt(row["encrypted_overrides"].encode()))
        if not isinstance(data, dict):
            raise ValueError("configuration shape")
        return data
    except (InvalidToken, ValueError, TypeError):
        raise CoreError("OPERATOR_CONFIGURATION_UNREADABLE", 503) from None


def latest(store):
    return store.one('SELECT operator_configuration.version AS version, '
        'operator_configuration.request_id AS request_id, operator_configuration.request_sha256 AS '
        'request_sha256, operator_configuration.encrypted_overrides AS encrypted_overrides, '
        'operator_configuration.fields_json AS fields_json, operator_configuration.created_at AS '
        'created_at FROM operator_configuration AS operator_configuration ORDER BY '
        'operator_configuration.version DESC NULLS LAST LIMIT 1')


def _raw(value, *, reveal=True):
    if isinstance(value, SecretStr):
        return value.get_secret_value() if reveal else value
    if isinstance(value, BaseModel):
        # Serialization exclusions describe the public wire shape, not effective values.
        return {name: _raw(getattr(value, name), reveal=reveal)
                for name in type(value).model_fields}
    if isinstance(value, dict):
        return {key: _raw(item, reveal=reveal) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_raw(item, reveal=reveal) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return _raw(asdict(value), reveal=reveal)
    return value


def _safe(value):
    if isinstance(value, SecretStr):
        return None
    if isinstance(value, BaseModel):
        return _safe(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def _merge(original, changes):
    if isinstance(original, SecretStr) and changes is None:
        return original  # Write-only fields are returned as null, not a request to erase a key.
    if not isinstance(original, dict) or not isinstance(changes, dict):
        return changes
    result = dict(original)
    for key, value in changes.items():
        result[key] = _merge(result.get(key), value)
    return result


def _candidate(settings, overrides):
    try:
        values = _raw(settings, reveal=False)
        return type(settings).model_validate(_merge(values, overrides))
    except (ValidationError, ValueError, TypeError):
        # Never include rejected input in an exception, even if it contains a credential.
        raise CoreError("INVALID_OPERATOR_CONFIGURATION", 400) from None


def _endpoint_changes(before, after, changes):
    # Write-only null means keep the key only while its destination is unchanged.
    # A new endpoint requires an explicit credential choice, not an invisible reuse.
    pairs = [(before.model_url, after.model_url, before.model_key,
              changes.get("model_key"), "model_url" in changes)]
    content = changes.get("content", {})
    vision = content.get("vision", {}) if isinstance(content, dict) else {}
    if isinstance(vision, dict):
        pairs.append((before.content.vision.url, after.content.vision.url,
                      before.content.vision.key, vision.get("key"), "url" in vision))
    image = changes.get("image_generation", {})
    endpoint = image.get("endpoint", {}) if isinstance(image, dict) else {}
    if isinstance(endpoint, dict):
        pairs.append((before.image_generation.endpoint.url, after.image_generation.endpoint.url,
                      before.image_generation.endpoint.key, endpoint.get("key"),
                      "url" in endpoint))
    candidate_changes = changes.get("video_models", {}).get("candidates", {})
    for name, config in candidate_changes.items():
        old = before.video_models.candidates.get(name)
        if old:
            new = after.video_models.candidates[name]
            pairs.append((old.endpoint, new.endpoint, old.key, config.get("key"),
                          "endpoint" in config))
    for old_url, new_url, old_key, supplied_key, edited in pairs:
        if not edited or not new_url:
            continue
        if urlsplit(new_url).scheme != "https":
            raise CoreError("CONFIGURATION_ENDPOINT_HTTPS_REQUIRED", 400)
        if old_url.rstrip("/") != new_url.rstrip("/") and old_key.get_secret_value():
            if not isinstance(supplied_key, str):
                raise CoreError("CONFIGURATION_ENDPOINT_KEY_REQUIRED", 400)
            if (urlsplit(old_url).netloc != urlsplit(new_url).netloc
                and supplied_key == old_key.get_secret_value()):
                raise CoreError("CONFIGURATION_ENDPOINT_KEY_REQUIRED", 400)


def load(settings):
    """Startup still applies complete versions, including restart-only fields."""
    with Database(settings.database, max_connections=1) as database, database.session() as db:
        if not db.one(
            'SELECT 1 FROM pg_catalog.pg_class WHERE relnamespace=current_schema()::regnamespace '
            "AND relname='operator_configuration' AND relkind IN ('r','p')",
        ):
            return settings
        rows = db.rows('SELECT operator_configuration.version AS version, '
            'operator_configuration.request_id AS request_id, '
            'operator_configuration.request_sha256 AS request_sha256, '
            'operator_configuration.encrypted_overrides AS encrypted_overrides, '
            'operator_configuration.fields_json AS fields_json, operator_configuration.created_at '
            'AS created_at FROM operator_configuration AS operator_configuration ORDER BY '
            'operator_configuration.version DESC NULLS LAST')
        error = None
        for record in rows:
            try:
                result = _candidate(settings, _decode(dict(record), settings))
            except CoreError as failure:
                if not settings.operator_hot_reload:
                    raise
                error = error or failure.code
                continue
            result._operator_config_version = record["version"]
            result._operator_config_base = settings._operator_config_base or encode(_raw(settings))
            result._operator_config_error = error
            return result
        if error:
            # Recover a previously observed effective snapshot only if the deployment
            # still proves its complete cold identity. No credential or policy is guessed.
            from companion import runtime_config

            try:
                observed = db.one('SELECT value FROM runtime_meta WHERE key = %s',
                                      (runtime_config.PREFIX + "worker",))
                key = json.loads(observed["value"])["snapshot_id"] if observed else None
                saved = db.one('SELECT operator_runtime_snapshots.snapshot_id AS snapshot_id, '
                    'operator_runtime_snapshots.version AS version, '
                    'operator_runtime_snapshots.base_sha256 AS base_sha256, '
                    'operator_runtime_snapshots.chat_time_labels AS chat_time_labels, '
                    'operator_runtime_snapshots.created_at AS created_at, '
                    'operator_runtime_snapshots.expression_json AS expression_json FROM '
                    'operator_runtime_snapshots AS operator_runtime_snapshots WHERE '
                    'operator_runtime_snapshots.snapshot_id = %s',
                                   (key,))
                snapshot = runtime_config.validate_record(settings, saved, lookup=lambda version:
                    db.one('SELECT operator_configuration.version AS version, '
                        'operator_configuration.request_id AS request_id, '
                        'operator_configuration.request_sha256 AS request_sha256, '
                        'operator_configuration.encrypted_overrides AS encrypted_overrides, '
                        'operator_configuration.fields_json AS fields_json, '
                        'operator_configuration.created_at AS created_at FROM '
                        'operator_configuration AS operator_configuration WHERE '
                        'operator_configuration.version = %s',
                               (version,)))
                result = _candidate(settings, runtime_config.values(settings, snapshot))
                result._operator_config_version = snapshot.version
                result._operator_config_base = (settings._operator_config_base
                                                or encode(_raw(settings)))
                result._operator_config_error = error
                return result
            except (CoreError, psycopg.Error, ValueError, TypeError, KeyError):
                pass
            raise CoreError(error, 503)
    return settings


GROUPS = {"chat_thinking": "model", "chat_thinking_tokens": "model",
          "token_measurement": "model", "chat_time_labels": "expression",
          "conversation_policy": "expression", "conversation_guidance": "expression",
          "input_debounce": "expression", "turn_taking": "expression",
          "quote_policy": "expression",
          "style_live": "expression", "style_distill": "expression", "jargon": "memory",
          "meme_wiki": "expression", "image_generation": "expression",
          "watch_ledger": "content", "rem": "autonomy", "life_loop": "autonomy"}


def _group(key):
    if key in GROUPS:
        return GROUPS[key]
    if key == "video_models":
        return "model"
    if key.startswith(("model", "provider", "input_rate", "cached_input", "output_rate")):
        return "model"
    if key.startswith(("content", "web_reading", "voice", "camera", "video")):
        return "content"
    if key.startswith(("expression", "meme", "segmented", "mood")):
        return "expression"
    if key.startswith(("memory", "extraction", "semantic", "raw_event")):
        return "memory"
    if key.startswith(("diary", "reflection", "goals", "goal_", "wake", "exploration",
                       "proactive", "social", "affect", "consolidation", "quiet")):
        return "autonomy"
    return "system"


def _field_type(schema, definitions):
    variants = schema.get("anyOf", [schema])
    nullable = any(item.get("type") == "null" for item in variants)
    variants = [item for item in variants if item.get("type") != "null"]
    selected = variants[0] if len(variants) == 1 else schema
    if "$ref" in selected:
        selected = definitions.get(selected["$ref"].rsplit("/", 1)[-1], selected)
    return selected.get("type", "string"), nullable


def view(store, settings):
    from companion import owner_authority, runtime_config

    row = latest(store)
    problem = None
    try:
        desired = _candidate(settings, _decode(row, settings))
    except CoreError as error:
        desired, problem = settings, error.code
    classification = runtime_config.classify(settings, desired)
    approval_problem = owner_authority.stored_policy_problem(store, desired)
    workers = runtime_config.workers(store, settings)
    primary = next((w for w in workers if w["worker"] == "worker"), None)
    inbox = next((w for w in workers if w["worker"] == "inbox"), None)
    batch_timing = (runtime_config._read(store, settings, inbox["snapshot_id"]).debounce
                    if inbox else settings.input_debounce.timing())
    active_version = primary["active_version"] if primary else settings._operator_config_version
    active_settings = runtime_config.effective(settings, runtime_config._read(
        store, settings, primary["snapshot_id"])) if primary else settings
    document = type(settings).model_json_schema()
    properties, definitions = document["properties"], document.get("$defs", {})
    fields = []
    for key in type(settings).model_fields:
        if key in EXCLUDED:
            continue
        value, active = getattr(desired, key), getattr(settings, key)
        if key == "input_debounce":
            active = settings.input_debounce.model_copy(update=batch_timing.model_dump())
            source = "inbox" if inbox else "startup"
        elif key in runtime_config.HOT_FIELDS and primary:
            active = getattr(active_settings, key)
            source = "worker"
        else:
            source = "startup"
        schema = properties[key]
        kind, nullable = _field_type(schema, definitions)
        if isinstance(value, SecretStr):
            kind = "secret"
        item = {"key": key, "label": LABELS.get(key, key), "group": _group(key),
                "type": kind, "value": _safe(value), "active_value": _safe(active),
                "active_source": source,
                "nullable": nullable,
                "effect": "task_boundary" if key in runtime_config.HOT_FIELDS else (
                    "restart_and_reapproval" if key in runtime_config.REVIEW_FIELDS
                    else "restart_and_window_handoff"), "schema": schema}
        if key in runtime_config.HOT_OBJECT_FIELDS:
            item["hot_members"] = sorted(runtime_config.HOT_OBJECT_FIELDS[key])
        if key == "input_debounce":
            item["effect"] = "batch_boundary"
        if "description" in schema:
            item["description"] = schema["description"]
        for bound in ("minimum", "maximum"):
            if bound in schema:
                item[bound] = schema[bound]
        if kind == "secret":
            item["configured"] = bool(value.get_secret_value())
        if key == "video_models":
            paid = settings.paid_model
            item["default_model"] = paid.video_input.model if paid and paid.video_input else None
            item["approvals"] = {name: route.approval(paid.window_id).model_dump()
                for name, route in value.candidates.items()} if paid else {}
            item["authorized"] = [name for name, route in value.candidates.items()
                if paid and paid.video_input
                and route.approval(paid.window_id) == paid.video_routes.get(name)]
        fields.append(item)
    version = row["version"] if row else 0
    history = [{"version": item["version"], "created_at": item["created_at"],
                "fields": json.loads(item["fields_json"])}
               for item in store.rows('SELECT version, created_at, fields_json FROM '
                   'operator_configuration ORDER BY version DESC NULLS LAST LIMIT 20')]
    pending_restart = bool(classification["restart_fields"] or (
        not settings.operator_hot_reload and version != settings._operator_config_version))
    pending = (version != active_version or batch_timing != desired.input_debounce.timing()
               or any(w["active_version"] != version for w in workers if w["worker"] != "inbox"))
    expected_reasons = {None, "CONFIGURATION_RESTART_REQUIRED", "CONFIGURATION_REAPPROVAL_REQUIRED",
                        "CONFIGURATION_HOT_RELOAD_DISABLED"}
    worker_error = next((w["reason"] for w in workers if w["saved_version"] == version
                         and w["reason"] not in expected_reasons), None)
    return {"version": version, "active_version": active_version,
            "startup_version": settings._operator_config_version,
            "pending_version": version if pending else None,
            "pending_restart": pending_restart, "pending_reload": pending and not pending_restart,
            "requires_reapproval": bool(classification["review_fields"] or approval_problem),
            "approval_reason": approval_problem.code if approval_problem else None,
            "hot_reload_enabled": settings.operator_hot_reload,
            "error": problem or worker_error, "workers": workers, **classification,
            "fields": fields, "history": history}


def update(store, settings, body, key, now, *, owner_check=None, clock=None):
    if set(body.changes) - (set(type(settings).model_fields) - EXCLUDED):
        raise CoreError("CONFIGURATION_FIELD_NOT_EDITABLE", 400)
    fingerprint = hmac.new(settings.control_token.get_secret_value().encode(),
                           encode(body.model_dump()).encode(), hashlib.sha256).hexdigest()
    with store.transaction():
        if owner_check is not None:
            owner_check()
        if clock is not None:
            now = clock()
        previous = store.one('SELECT request_sha256 FROM operator_configuration WHERE request_id = '
            '%s',
                             (key,))
        if previous:
            if previous["request_sha256"] != fingerprint:
                raise CoreError("IDEMPOTENCY_CONFLICT", 409)
        else:
            current = latest(store)
            version = current["version"] if current else 0
            if body.expected_version != version:
                raise CoreError("CONFIGURATION_VERSION_CHANGED", 409)
            try:
                overrides = _decode(current, settings)
                desired = _candidate(settings, overrides)
            except CoreError:
                if body.restore_version is None:
                    raise
                overrides, desired = {}, settings
            changes = body.changes
            if body.restore_version is not None:
                historical = store.one('SELECT operator_configuration.version AS version, '
                    'operator_configuration.request_id AS request_id, '
                    'operator_configuration.request_sha256 AS request_sha256, '
                    'operator_configuration.encrypted_overrides AS encrypted_overrides, '
                    'operator_configuration.fields_json AS fields_json, '
                    'operator_configuration.created_at AS created_at FROM operator_configuration '
                    'AS operator_configuration WHERE operator_configuration.version = %s',
                                       (body.restore_version,))
                if body.restore_version and historical is None:
                    raise CoreError("CONFIGURATION_VERSION_NOT_FOUND", 404)
                base = (type(settings).model_validate_json(settings._operator_config_base)
                        if settings._operator_config_base else settings)
                selected = _candidate(base, _decode(historical, settings))
                changes = {name: _raw(getattr(selected, name))
                           for name in type(settings).model_fields
                           if name not in EXCLUDED}
            validated = _candidate(desired, changes)
            _endpoint_changes(desired, validated, changes)
            overrides.update({name: _raw(getattr(validated, name)) for name in changes})
            encrypted = _cipher(settings).encrypt(encode(_raw(overrides)).encode()).decode()
            store.insert("operator_configuration", version=version + 1, request_id=key,
                         request_sha256=fingerprint, encrypted_overrides=encrypted,
                         fields_json=encode(sorted(name for name in changes if (
                             _raw(getattr(desired, name)) != _raw(getattr(validated, name))
                         ))), created_at=now)
            store.audit("configuration.saved", str(version + 1), "console", now,
                        fields=sorted(changes), effect="saved_pending_validation")
    return view(store, settings)
