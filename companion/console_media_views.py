"""Read-only, bounded owner projections; never return speech content or credentials."""

from datetime import datetime

from pydantic import BaseModel, SecretStr

from companion import paid_budget
from companion.contracts import CoreError
from companion.privacy import redact
from companion.social_autonomy import SocialAutonomy, public_source
from companion.social_autonomy_protocol import ActionReceipt, FrozenPlan, ReviewedAvatar
from companion.store import after

ACTIONS = {
    "qzone.publish": "QQ空间发布",
    "qq.profile.nickname": "昵称更新",
    "qq.profile.signature": "签名更新",
    "qq.profile.avatar": "头像更新",
}
VOICE_STATES = {
    "QUEUED": "等待节点",
    "LEASED": "节点已领取",
    "RUNNING": "正在处理",
    "READY": "音频已生成",
    "TRANSCRIBED": "转写已完成",
    "UNKNOWN": "执行结果不明",
    "EXPIRED": "已过期",
    "CANCELLED": "已取消",
    "FAILED": "处理失败",
}
PROBLEMS = {
    "SOCIAL_DAILY_ACTION_LIMIT": "已达到每日次数上限",
    "SOCIAL_ACTION_COOLDOWN": "仍在冷却期间",
    "SOCIAL_ACTION_RECONCILIATION_REQUIRED": "有投递需要先核对",
    "SOCIAL_LEGACY_RECONCILIATION_REQUIRED": "历史投递需要先核对",
    "SOCIAL_RESTORE_RECONCILIATION_REQUIRED": "恢复后需要先核对",
    "SOCIAL_AUTONOMY_PAUSED": "自主行为已暂停",
    "SOCIAL_LIVE_SCOPE_REQUIRED": "尚未获得当前窗口授权",
    "SOCIAL_ACCOUNT_BINDING_CHANGED": "账号绑定已变化",
}


def _entry(identifier, kind, title, state, at=None, summary="", **detail):
    return {
        "id": str(identifier),
        "kind": kind,
        "title": title,
        "status": state,
        "occurred_at": at,
        "summary": summary,
        "detail": detail,
    }


def _instant(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _secrets(value):
    if isinstance(value, SecretStr):
        return {value.get_secret_value()} - {""}
    if isinstance(value, BaseModel):
        value = {name: getattr(value, name) for name in type(value).model_fields}
    if isinstance(value, dict):
        return {secret for item in value.values() for secret in _secrets(item)}
    if isinstance(value, (list, tuple)):
        return {secret for item in value for secret in _secrets(item)}
    return set()


def _safe(core, value):
    secrets = sorted(_secrets(core.settings), key=len, reverse=True)

    def clean(item):
        if isinstance(item, str):
            item = redact(item, core.settings)
            for secret in secrets:
                item = item.replace(secret, "[已隐藏]")
            return item
        if isinstance(item, dict):
            return {key: clean(part) for key, part in item.items()}
        if isinstance(item, list):
            return [clean(part) for part in item]
        return item

    return clean(value)


def voice(core):
    now, cfg, store = core.clock(), core.settings.voice, core.store
    freshness = max(15, cfg.lease_ttl_seconds * 2)
    items, recent = [], 0
    for node_id, node in sorted(cfg.nodes.items()):
        observed = store.one('SELECT voice_node_state.node_id AS node_id, '
            'voice_node_state.active_job_id AS active_job_id, voice_node_state.resource_state AS '
            'resource_state, voice_node_state.last_seen_at AS last_seen_at, '
            'voice_node_state.last_error_code AS last_error_code, voice_node_state.last_claim_seq '
            'AS last_claim_seq, voice_node_state.claim_request_sha256 AS claim_request_sha256, '
            'voice_node_state.claim_response_json AS claim_response_json, '
            'voice_node_state.claim_expires_at AS claim_expires_at, voice_node_state.claim_job_id '
            'AS claim_job_id, voice_node_state.recovery_required AS recovery_required, '
            'voice_node_state.recovered_incarnation AS recovered_incarnation FROM voice_node_state '
            'AS voice_node_state WHERE voice_node_state.node_id = %s', (node_id,)) or {}
        seen = _instant(observed.get("last_seen_at"))
        age = (_instant(now) - seen).total_seconds() if seen else None
        fresh = age is not None and 0 <= age <= freshness
        recent += int(fresh)
        resource = observed.get("resource_state")
        state = (
            "DISABLED"
            if not cfg.enabled or not node.enabled
            else "UNOBSERVED"
            if seen is None
            else "OFFLINE"
            if not fresh
            else "QUARANTINED"
            if resource == "QUARANTINED"
            else "BUSY"
            if observed.get("active_job_id") or resource == "RUNNING"
            else "ONLINE"
        )
        verified = (
            store.one(
                'SELECT manifest_sha256, lease_issued_at FROM voice_jobs WHERE node_id = %s AND '
                'NOT lease_issued_at IS NULL ORDER BY lease_issued_at DESC NULLS LAST LIMIT 1',
                (node_id,),
            )
            or {}
        )
        label = {
            "DISABLED": "未启用",
            "UNOBSERVED": "尚无联系记录",
            "OFFLINE": "联系记录已过期",
            "QUARANTINED": "资源待核对",
            "BUSY": "有未结束任务",
            "ONLINE": "近期有联系",
        }[state]
        items.append(
            _entry(
                "node:" + node_id,
                "voice_node",
                node_id,
                state,
                observed.get("last_seen_at"),
                label,
                state_label=label,
                last_seen_at=observed.get("last_seen_at"),
                resource_state=resource,
                active_job_id=observed.get("active_job_id"),
                capabilities=list(node.capabilities),
                expected_manifest_sha256=node.manifest_sha256,
                verified_manifest_sha256=verified.get("manifest_sha256"),
                manifest_verified_at=verified.get("lease_issued_at"),
                manifest_state=(
                    "UNOBSERVED"
                    if not verified
                    else "MATCHED"
                    if verified["manifest_sha256"] == node.manifest_sha256
                    else "CHANGED"
                ),
                limits={
                    "tts_chars": node.max_tts_chars,
                    "tts_seconds": node.max_tts_seconds,
                    "tts_output_bytes": node.max_tts_output_bytes,
                    "asr_input_bytes": node.max_asr_input_bytes,
                    "asr_seconds": node.max_asr_seconds,
                    "asr_segments": node.max_asr_segments,
                    "asr_chars": node.max_asr_chars,
                },
                last_error_code=observed.get("last_error_code"),
            )
        )
    rows = store.rows(
        'SELECT v.job_id, v.operation, v.state, v.node_id, v.source_partial, v.input_bytes, '
        'v.input_duration_seconds, v.result_bytes, v.created_at, v.updated_at, v.deadline_at, '
        'v.result_expires_at, v.last_error_code, v.message_id, o.state AS delivery_state FROM '
        'voice_jobs AS v LEFT JOIN outbox AS o USING (message_id) ORDER BY v.created_at DESC NULLS '
        'LAST, v.job_id NULLS FIRST LIMIT 60'
    )
    for row in rows:
        state = (
            "DELIVERY_UNCERTAIN" if row["delivery_state"] == "DELIVERY_UNCERTAIN" else row["state"]
        )
        label = "投递结果不明" if state == "DELIVERY_UNCERTAIN" else VOICE_STATES.get(state, state)
        operation = "语音合成" if row["operation"] == "tts" else "语音转写"
        summary = (
            "原消息投递结果需要核对。"
            if state == "DELIVERY_UNCERTAIN"
            else "处理结果仅供当前对话使用。"
            if row["operation"] == "asr"
            else "音频与原文字共用同一条消息投递记录。"
        )
        items.append(
            _entry(
                row["job_id"],
                "voice_job",
                operation,
                state,
                row["created_at"],
                summary,
                state_label=label,
                operation=row["operation"],
                task_state=row["state"],
                node_id=row["node_id"],
                partial=bool(row["source_partial"]),
                input_bytes=row["input_bytes"],
                input_duration_seconds=row["input_duration_seconds"],
                result_bytes=row["result_bytes"],
                message_id=row["message_id"],
                delivery_state=row["delivery_state"],
                deadline_at=row["deadline_at"],
                result_expires_at=row["result_expires_at"],
                last_error_code=row["last_error_code"],
            )
        )
    counts = store.one(
        "SELECT COUNT(*) AS total, CAST(SUM(CAST(v.state IN ('QUEUED', 'LEASED', 'RUNNING') AS "
        "INT)) AS BIGINT) AS pending, CAST(SUM(CAST(o.state = 'DELIVERY_UNCERTAIN' AS INT)) AS "
        'BIGINT) AS uncertain FROM voice_jobs AS v LEFT JOIN outbox AS o USING (message_id)'
    )
    return _safe(
        core,
        {
            "items": items,
            "next_cursor": None,
            "observed_at": now,
            "meta": {
                "enabled": cfg.enabled,
                "freshness_seconds": freshness,
                "nodes_recent": recent,
                "nodes_total": len(cfg.nodes),
                "pending_jobs": counts["pending"] or 0,
                "uncertain_deliveries": counts["uncertain"] or 0,
                "total_jobs": counts["total"],
                "displayed_jobs": len(rows),
                "reply_policy": cfg.reply.model_dump(mode="json"),
                "asr_policy": cfg.asr.model_dump(mode="json"),
            },
        },
    )


def _receipt(row):
    try:
        receipt = ActionReceipt.model_validate_json(row["receipt_json"] or "{}")
        if (
            receipt.message_id != row["message_id"]
            or receipt.action_type != row["action_type"]
            or receipt.payload_sha256 != row["payload_sha256"]
            or paid_budget.sha(receipt.model_dump(mode="json")) != row["receipt_sha256"]
            or (row["lease_token"] and row["lease_token"] != receipt.lease_token)
        ):
            return None
        return receipt
    except (ValueError, TypeError):
        return None


def _plans(core, now):
    rows = core.store.rows(
        'SELECT p.plan_id, p.state, p.plan_json, p.payload_sha256, p.redacted, p.created_at, '
        'p.expires_at, p.last_error_code, r.message_id, r.action_type, r.dispatched_at, '
        'r.receipt_json, r.receipt_sha256, o.state AS delivery_state, o.lease_token, j.job_id AS '
        'retry_job_id, j.state AS retry_state, j.next_attempt_at FROM social_autonomy_plans AS p '
        'LEFT JOIN social_autonomy_reservations AS r USING (plan_id) LEFT JOIN outbox AS o USING '
        '(message_id) LEFT JOIN social_autonomy_jobs AS retry ON retry.plan_id = p.plan_id LEFT '
        'JOIN jobs AS j ON j.job_id = retry.job_id ORDER BY p.created_at DESC NULLS LAST, '
        'p.plan_id NULLS FIRST LIMIT 60'
    )
    items = []
    for row in rows:
        action, summary, sources = row["action_type"], "", []
        state, label = (
            row["state"],
            {
                "READY": "计划已准备",
                "PLANNING": "正在规划",
                "SKIPPED": "本轮保持不变",
                "REJECTED": "计划未通过",
                "CANCELLED": "计划已取消",
            }[row["state"]],
        )
        valid_plan = None
        if not row["redacted"] and row["state"] == "READY":
            try:
                valid_plan = FrozenPlan.model_validate_json(row["plan_json"])
                if (
                    valid_plan.payload_sha256 != row["payload_sha256"]
                    or paid_budget.sha(
                        valid_plan.model_dump(mode="json", exclude={"payload_sha256"})
                    )
                    != row["payload_sha256"]
                ):
                    raise ValueError()
                action = valid_plan.action_type
                proposal = valid_plan.proposal.model_dump()
                summary = str(
                    proposal.get("text") or proposal.get("value") or "从已审核素材中选择头像"
                )
                summary = summary[:160] + ("…" if len(summary) > 160 else "")
            except (ValueError, TypeError):
                valid_plan, state, label = None, "INVALID", "计划凭据不完整"
        if row["redacted"]:
            summary, label = "来源已撤回，计划内容已清除。", "计划已撤销"
        for source in core.store.rows(
            "SELECT event_id FROM social_autonomy_sources WHERE plan_id = %s AND role = 'content' "
            'ORDER BY event_id NULLS FIRST LIMIT 20',
            (row["plan_id"],),
        ):
            try:
                value = public_source(core.store, core.settings, source["event_id"], now)
                sources.append(
                    {
                        "title": value["title"][:160],
                        "url": value["url"],
                        "coverage": value["coverage"],
                        "status": "FETCHED",
                    }
                )
            except CoreError:
                sources.append({"title": "来源当前不可用", "status": "UNAVAILABLE"})
        receipt = _receipt(row) if row["message_id"] else None
        receipt_state = (
            receipt.result
            if receipt
            else ("INVALID" if row["receipt_json"] not in (None, "{}", "") else "UNOBSERVED")
        )
        if row["delivery_state"] == "SENT":
            state, label = (
                ("SUCCEEDED", "平台已接受")
                if receipt and receipt.result == "SUCCEEDED"
                else ("UNVERIFIED", "送达记录待核对")
            )
        elif row["delivery_state"] in {"DELIVERY_UNCERTAIN", "SENDING"}:
            state, label = (
                row["delivery_state"],
                ("平台结果不明" if row["delivery_state"] == "DELIVERY_UNCERTAIN" else "已开始发送"),
            )
        elif receipt and receipt.result == "REJECTED":
            state, label = "REJECTED", "平台已拒绝"
        elif row["delivery_state"]:
            state = row["delivery_state"]
            label = {
                "PENDING": "等待投递",
                "LEASED": "投递已领取",
                "RETRY_WAIT": "等待重试",
                "FAILED": "投递失败",
                "CANCELLED": "投递已取消",
            }.get(state, label)
        elif not row["redacted"] and row["state"] == "REJECTED" and row["retry_state"] in {
            "QUEUED", "RETRY_WAIT", "RUNNING",
        }:
            state = row["retry_state"]
            label = "正在重新检查计划" if state == "RUNNING" else "等待重新检查预算"
        items.append(
            _entry(
                row["plan_id"],
                "account_plan",
                ACTIONS.get(action, "账号行为计划"),
                state,
                row["created_at"],
                summary,
                state_label=label,
                action_type=action,
                plan_state=row["state"],
                delivery_state=row["delivery_state"],
                message_id=row["message_id"],
                receipt_state=receipt_state,
                native_retcode=receipt.native_retcode if receipt else None,
                resource_id=receipt.resource_id if receipt else None,
                receipt_sha256=receipt.native_receipt_sha256 if receipt else None,
                dispatched_at=row["dispatched_at"],
                expires_at=row["expires_at"],
                last_error_code=row["last_error_code"],
                retry_job_id=row["retry_job_id"],
                retry_state=row["retry_state"],
                next_attempt_at=row["next_attempt_at"],
                sources=sources,
            )
        )
    return items


def _assets(core, now):
    items = []
    for row in core.store.rows(
        'SELECT social_autonomy_assets.asset_id AS asset_id, social_autonomy_assets.asset_sha256 '
        'AS asset_sha256, social_autonomy_assets.provenance_json AS provenance_json, '
        'social_autonomy_assets.provenance_sha256 AS provenance_sha256, '
        'social_autonomy_assets.created_at AS created_at, social_autonomy_assets.expires_at AS '
        'expires_at, social_autonomy_assets.revoked_at AS revoked_at FROM social_autonomy_assets '
        'AS social_autonomy_assets ORDER BY social_autonomy_assets.created_at DESC NULLS LAST '
        'LIMIT 20'
    ):
        try:
            asset = ReviewedAvatar.model_validate_json(row["provenance_json"])
            valid = (
                paid_budget.sha(asset.model_dump(mode="json")) == row["provenance_sha256"]
                and asset.asset_sha256 == row["asset_sha256"]
            )
            state = (
                "REVOKED"
                if row["revoked_at"]
                else "INVALID"
                if not valid
                else "EXPIRED"
                if _instant(asset.expires_at) <= _instant(now)
                else "REVIEW"
                if asset.review_policy_sha256 != core.settings.social_autonomy.asset_policy_sha256
                else "REVIEWED"
            )
            items.append(
                _entry(
                    row["asset_id"],
                    "account_asset",
                    "头像素材 · " + asset.description[:60],
                    state,
                    asset.reviewed_at,
                    "审核与来源许可记录；是否可使用仍受当前策略约束。",
                    source_page=asset.source_page,
                    source_url=asset.source_url,
                    license_label=asset.license_label,
                    usage_conditions=asset.usage_conditions[:1000],
                    purpose="账号头像",
                    dimensions=f"{asset.width} × {asset.height}",
                    size_bytes=asset.size_bytes,
                    expires_at=asset.expires_at,
                    revoked_at=row["revoked_at"],
                    asset_sha256=row["asset_sha256"],
                    review_receipt_sha256=asset.review_receipt_sha256,
                )
            )
        except (ValueError, TypeError):
            items.append(
                _entry(
                    row["asset_id"],
                    "account_asset",
                    "头像素材",
                    "INVALID",
                    row["created_at"],
                    "审核凭据不完整。",
                )
            )
    return items


def _profiles(core, now):
    items = []
    binding = paid_budget.sha([core.settings.bot_qq_id, core.settings.owner_qq_id])
    for field, label in (("nickname", "昵称"), ("signature", "签名"), ("avatar", "头像")):
        row = core.store.one(
            'SELECT social_autonomy_profiles.binding_sha256 AS binding_sha256, '
            'social_autonomy_profiles.field AS field, social_autonomy_profiles.value AS value, '
            'social_autonomy_profiles.observed_at AS observed_at, '
            'social_autonomy_profiles.receipt_sha256 AS receipt_sha256 FROM '
            'social_autonomy_profiles AS social_autonomy_profiles WHERE '
            'social_autonomy_profiles.binding_sha256 = %s AND social_autonomy_profiles.field = %s',
            (binding, field),
        )
        latest = core.store.one(
            'SELECT MAX(COALESCE(dispatched_at, reserved_at)) AS at FROM '
            'social_autonomy_reservations WHERE action_type = %s AND released_at IS NULL',
            ("qq.profile." + field,),
        )["at"]
        observed = _instant(row["observed_at"]) if row else None
        fresh = bool(
            observed
            and _instant(after(now, -core.settings.social_autonomy.profile_baseline_seconds))
            <= observed
            <= _instant(now)
            and (not latest or _instant(latest) < observed)
        )
        state = "FRESH" if fresh else "STALE" if row else "UNOBSERVED"
        value = ("已记录头像摘要" if field == "avatar" else row["value"][:320]) if row else None
        items.append(
            _entry(
                "profile:" + field,
                "account_profile",
                label + "观测",
                state,
                row["observed_at"] if row else None,
                "观测时间指账号接口读取时间，协议可能返回缓存；修改结果仍以实际回执为准。",
                field=field,
                baseline_value=value,
                observed_at=row["observed_at"] if row else None,
                freshness_seconds=core.settings.social_autonomy.profile_baseline_seconds,
                freshness_state=state,
                last_action_at=latest,
                receipt_sha256=row["receipt_sha256"] if row else None,
            )
        )
    return items


def account_actions(core):
    now, cfg = core.clock(), core.settings.social_autonomy
    module, limits = SocialAutonomy(core.store, core.settings, clock=core.clock), []
    policy_problem = None
    if cfg.enabled:
        try:
            module._policy()  # Pure policy reads, without authorization admission or clock writes.
        except CoreError as error:
            policy_problem = error.code
    for action, title in ACTIONS.items():
        profile = action != "qzone.publish"
        enabled = cfg.enabled and getattr(
            cfg, action.rsplit(".", 1)[-1] + "_enabled" if profile else "qzone_enabled"
        )
        cooldown = cfg.profile_cooldown_seconds if profile else cfg.qzone_cooldown_seconds
        problem = policy_problem
        if not problem and enabled:
            try:
                module._capacity(action, core.settings.timezone, now)
            except CoreError as error:
                problem = error.code
        latest = core.store.one(
            'SELECT MAX(COALESCE(r.dispatched_at, r.reserved_at)) AS at FROM '
            'social_autonomy_reservations AS r JOIN outbox AS o USING (message_id) WHERE '
            "r.action_type = %s AND r.released_at IS NULL AND NOT (o.state IN ('CANCELLED', "
            "'FAILED') AND r.dispatched_at IS NULL AND o.attempt_count = 0)",
            (action,),
        )["at"]
        limits.append(
            {
                "action_type": action,
                "title": title,
                "enabled": enabled,
                "daily_limit": cfg.profile_daily_limit if profile else cfg.qzone_daily_limit,
                "shared_profile_limit": profile,
                "cooldown_seconds": cooldown,
                "next_after": after(latest, cooldown) if latest else None,
                "state": "DISABLED" if not enabled else "BLOCKED" if problem else "AVAILABLE",
                "reason": "未启用"
                if not enabled
                else PROBLEMS.get(problem, "等待核对当前限频状态")
                if problem
                else "当前限频未阻止新计划",
                "reason_code": problem,
            }
        )
    counts = core.store.one(
        "SELECT COUNT(*) AS total, CAST(SUM(CAST(state IN ('PENDING', 'LEASED', 'RETRY_WAIT') AS "
        "INT)) AS BIGINT) AS pending, CAST(SUM(CAST(state IN ('SENDING', 'DELIVERY_UNCERTAIN') AS "
        "INT)) AS BIGINT) AS uncertain FROM outbox WHERE delivery_lane = 'qq_account'"
    )
    return _safe(
        core,
        {
            "items": _profiles(core, now) + _plans(core, now) + _assets(core, now),
            "next_cursor": None,
            "observed_at": now,
            "meta": {
                "enabled": cfg.enabled,
                "policy_problem": PROBLEMS.get(policy_problem),
                "limits": limits,
                "total_deliveries": counts["total"],
                "pending_deliveries": counts["pending"] or 0,
                "uncertain_deliveries": counts["uncertain"] or 0,
                "profile_freshness_seconds": cfg.profile_baseline_seconds,
            },
        },
    )
