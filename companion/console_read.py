"""Bounded owner-facing projections of the existing ledgers and source-backed records."""

import json
from datetime import datetime

from companion import capabilities, worker_loop
from companion.cognition_read import CognitionRead
from companion.contracts import CoreError
from companion.memory_context import MemorySources, check_version, read_version
from companion.privacy import redact
from companion.store import encode


def entry(identifier, kind, title, status, at=None, summary="", **detail):
    return {"id": str(identifier), "kind": kind, "title": title, "status": status,
            "occurred_at": at, "summary": summary, "detail": detail}


def collection(core, items, next_cursor=None):
    return {"items": items, "next_cursor": next_cursor, "observed_at": core.clock()}


def activity(core, before=None, limit=30):
    rows = core.store.rows(
        "SELECT audit_id,action,entity_type,entity_id,trace_id,created_at FROM audit_log "
        "WHERE (? IS NULL OR audit_id<?) ORDER BY audit_id DESC LIMIT ?",
        (before, before, limit + 1),
    )
    items = [entry(row["audit_id"], row["entity_type"], row["action"], "RECORDED",
                   row["created_at"], entity_id=row["entity_id"], trace_id=row["trace_id"])
             for row in rows[:limit]]
    return collection(core, items, rows[limit - 1]["audit_id"] if len(rows) > limit else None)


def _memory_page(core, now, ids):
    """An explicit catalog page needs complete source checks, independent of reply recall."""
    core.memory.require_owner("owner")
    sources = MemorySources(core.memory, now)
    for row in core.store.rows(
        "SELECT * FROM memories WHERE memory_id IN (SELECT value FROM json_each(?)) "
        "AND status IN ('CONFIRMED','HYPOTHESIS') AND privacy_scope='private' AND confidence>0",
        (encode(ids),),
    ):
        if sources.current(row) and (evidence := sources.evidence(row["memory_id"])):
            yield {**row, "evidence_ids": [event["event_id"] for event in evidence]}


def _memory_rows(core, now, review=False):
    """Pending means unconfirmed or conflicted; native source eligibility still applies."""
    offset, batch_size = 0, 200
    while True:
        candidates = core.store.rows(
            "SELECT m.memory_id FROM memories m WHERE m.status IN ('CONFIRMED','HYPOTHESIS') "
            "AND m.privacy_scope='private' AND m.confidence>0 "
            "AND (?=0 OR m.status='HYPOTHESIS' OR m.needs_review=1) "
            "AND EXISTS (SELECT 1 FROM memory_evidence v JOIN events e ON e.event_id=v.event_id "
            "WHERE v.memory_id=m.memory_id AND v.polarity=1 AND e.redacted=0 "
            "AND e.actor_id='owner' AND e.privacy_scope='private') "
            "ORDER BY m.needs_review,m.status='CONFIRMED' DESC,m.updated_at DESC,m.memory_id "
            "LIMIT ? OFFSET ?", (int(review), batch_size, offset),
        )
        if not candidates:
            return
        ids = [row["memory_id"] for row in candidates]
        eligible = {row["memory_id"]: row for row in _memory_page(core, now, ids)}
        for memory_id in ids:
            if memory_id in eligible:
                yield eligible[memory_id]
        if len(ids) < batch_size:
            return
        offset += len(ids)


def _pending_memory(row):
    return row["status"] == "HYPOTHESIS" or bool(row["needs_review"])


def memories(core, query="", review=False, limit=30):
    core.memory.require_owner("owner")
    version, now = read_version(core.memory), core.clock()
    selected, folded, pending_count = [], query.casefold(), 0
    for row in _memory_rows(core, now):
        pending = _pending_memory(row)
        pending_count += int(pending)
        if ((not review or pending) and folded in row["content"].casefold()
            and len(selected) < limit):
            selected.append(row["memory_id"])
    # Recheck early pages after the complete scan, without returning stale evidence.
    rows = {row["memory_id"]: row for row in _memory_page(core, now, selected)}
    check_version(core.memory, version)
    items = []
    for memory_id in selected:
        row = rows.get(memory_id)
        if (row is None or (review and not _pending_memory(row))
            or folded not in row["content"].casefold()):
            continue
        items.append(entry(row["memory_id"], "memory", row["content"], row["status"],
                           row["updated_at"], subject=row["subject"], memory_kind=row["kind"],
                           needs_review=bool(row["needs_review"]),
                           evidence_ids=row["evidence_ids"]))
    return {**collection(core, items), "meta": {"pending_count": pending_count}}


def recall(core, query, limit=10):
    """The ranking chat recall would produce for a query, with its evidence; no model call."""
    core.memory.require_owner("owner")
    explain, trace = {}, {}
    rows = core.memory.search(query, limit=limit, now=core.clock(), explain=explain, trace=trace)
    return {"query": query, "semantic_min_score": core.settings.semantic_min_score,
            "retrieval": trace,
            "items": [{"id": row["memory_id"], "content": row["content"],
                       "status": row["status"], "subject": row["subject"],
                       "memory_kind": row["kind"], "needs_review": bool(row["needs_review"]),
                       "evidence_ids": row["evidence_ids"],
                       "recall": explain.get(row["memory_id"], {})} for row in rows]}


def _source_records(core, query, owner_field, related_fields=(), *, job_field=None):
    version, now = read_version(core.memory), core.clock()
    sources, result, offset = MemorySources(core.memory, now), [], 0
    current = datetime.fromisoformat(now)
    while len(result) < 60:
        rows = core.store.rows(query + " LIMIT 60 OFFSET ?", (offset,))
        if not rows:
            break
        for row in rows:
            if not sources.event(row[owner_field], require_text=False):
                continue
            related = {row[field] for field in related_fields if row[field]}
            if job_field:
                related.update(item["event_id"] for item in core.store.rows(
                    "SELECT event_id FROM job_sources WHERE job_id=?", (row[job_field],),
                ))
            if row.get("read_request_id"):
                related.update(item["event_id"] for item in core.store.rows(
                    "SELECT event_id FROM exploration_evidence WHERE request_id=?",
                    (row["read_request_id"],),
                ))
            # Related observations can be companion-authored; every ancestor still
            # needs the same private-owner custody and lifetime as native evidence.
            if any(
                source["event_id"] is None or source["redacted"] or source["erased"]
                or source["privacy_scope"] != "private" or source["scope"] != "private"
                or source["owner_actor_id"] != "owner"
                or datetime.fromisoformat(source["ingested_at"]) < sources.cutoff
                or datetime.fromisoformat(source["ingested_at"]) > current
                or datetime.fromisoformat(source["occurred_at"]) > current
                for event_id in related for source in sources._lineage(event_id)
            ):
                continue
            result.append(row)
            if len(result) == 60:
                break
        offset += len(rows)
    check_version(core.memory, version)
    return result


def life(core):
    version = read_version(core.memory)
    items = []
    for row in core.diary.list_recent(core.settings.owner_qq_id, 14):
        items.append(entry(row["entry_id"], "diary", row["title"], "COMMITTED",
                           row["local_date"], local_date=row["local_date"], version=row["version"]))
    for row in core.goals.recent():
        items.append(entry(row["goal_id"], "goal", row["topic"], row["state"],
                           row.get("updated_at"), commitment_state=row.get("commitment_state")))
    for row in core.reflections.recent()[:20]:
        items.append(entry(row["suggestion_id"], "reflection", row["content"], row["state"],
                           row.get("created_at")))
    check_version(core.memory, version)
    return collection(core, items)


def content(core):
    rows = _source_records(core,
        "SELECT c.item_id,c.kind,c.state,c.created_at,c.updated_at,c.error_code,"
        "c.owner_event_id,c.source_event_id,c.output_event_id,c.read_request_id,"
        "c.usage_request_id,c.reserved_bytes "
        "FROM content_items c JOIN events e ON e.event_id=c.owner_event_id "
        "WHERE c.redacted=0 AND e.redacted=0 ORDER BY c.created_at DESC,c.item_id",
        "owner_event_id", ("source_event_id", "output_event_id"),
    )
    return collection(core, [entry(row["item_id"], row["kind"],
        {"link": "分享链接", "qq_image": "主人图片", "link_image": "文章图片"}.get(
            row["kind"], "内容任务"), row["state"], row["created_at"], row["error_code"] or "",
        **{key: row[key] for key in ("updated_at", "owner_event_id", "output_event_id",
                                    "read_request_id", "usage_request_id", "reserved_bytes")})
        for row in rows])


def memes(core):
    rows = _source_records(core,
        "SELECT m.job_id,m.asset_sha256,m.source_sha256,m.usage_request_id,m.created_at,"
        "m.updated_at,j.event_id,j.state,j.last_error_code "
        "FROM meme_captions m JOIN jobs j USING(job_id) "
        "JOIN events e ON e.event_id=j.event_id WHERE e.redacted=0 "
        "ORDER BY m.created_at DESC,m.job_id", "event_id", job_field="job_id",
    )
    return collection(core, [entry(row["job_id"], "meme_caption", "表情素材理解", row["state"],
        row["created_at"], row["last_error_code"] or "", asset_sha256=row["asset_sha256"],
        source_sha256=row["source_sha256"], usage_request_id=row["usage_request_id"])
        for row in rows])


def budget_view(core):
    from companion import cloud_budget

    # Public adapter is kept here so the UI never reads raw price snapshots or credentials.
    value = cloud_budget.summary(core.store, core.settings, core.clock())
    return {**value, "daily_limit_micro_cny": value["config"]["daily_micro_cny"],
            "basis": value["cost_basis"]}


def _review_count(core):
    if not core.settings.memory_enabled:
        return None
    version, now = read_version(core.memory), core.clock()
    count = sum(1 for _ in _memory_rows(core, now, review=True))
    check_version(core.memory, version)
    return count


def overview(core):
    s, now = core.store, core.clock()
    counts = {
        "pending_jobs": s.one("SELECT COUNT(*) n FROM jobs WHERE state IN "
                              "('QUEUED','RUNNING','RETRY_WAIT')")["n"],
        "pending_deliveries": s.one("SELECT COUNT(*) n FROM outbox WHERE state IN "
                                    "('PENDING','LEASED','SENDING','RETRY_WAIT')")["n"],
        "unknown_calls": s.one("SELECT COUNT(*) n FROM model_usage WHERE state='UNKNOWN'")["n"],
        "review_memories": _review_count(core),
        "companion_proposals": s.one(
            "SELECT COUNT(*) n FROM capability_overrides WHERE state='PROPOSED'")["n"]
        if s.one("SELECT 1 FROM sqlite_master WHERE name='capability_overrides'") else 0,
        "pending_goals": s.one("SELECT COUNT(*) n FROM goals WHERE state='PENDING'")["n"]
        if s.one("SELECT 1 FROM sqlite_master WHERE name='goals'") else 0,
        "pending_reflections": s.one(
            "SELECT COUNT(*) n FROM reflection_suggestions WHERE state='PENDING'")["n"]
        if s.one("SELECT 1 FROM sqlite_master WHERE name='reflection_suggestions'") else 0,
    }
    worker = worker_loop.snapshot(s, datetime.fromisoformat(now))
    budget = budget_view(core)
    try:
        model = core.store.read_or_write(lambda: CognitionRead(core).daily_model_usage(
            budget["local_day"], budget["timezone"], now))
    except CoreError:
        model = None
    blocked = s.one("SELECT 1 FROM runtime_meta WHERE key IN "
                    "('model_budget_blocked','model_budget_restore_required')")
    paused = {row["key"]: row["value"] == "1" for row in s.rows(
        "SELECT key,value FROM runtime_meta WHERE key IN "
        "('proactive_paused','life_paused','diary_paused')")}
    # 主人在“管理主动行为”里切换的三个开关；available=false 表示配置文件没有开启，不能在控制台恢复
    modes = {
        "proactive": {"paused": paused.get("proactive_paused", False),
                      "available": bool(core.settings.proactive_enabled)},
        "life": {"paused": paused.get("life_paused", False), "available": True},
        "diary": {"paused": paused.get("diary_paused", False),
                  "available": bool(core.settings.diary_enabled)},
    }
    return {"schema_version": 1, "observed_at": now,
            "runtime": {"mode": core.settings.mode, "worker": worker, "modes": modes,
                        "state": "ATTENTION" if blocked or counts["unknown_calls"]
                        or budget["admission_blocked"] or worker["state"] != "RECORDED"
                        else "READY"},
            "counts": counts, "budget": budget, "model": model,
            "capabilities": capabilities.snapshot(s, core.settings, core.clock)}


def diagnostics(core):
    s, now = core.store, core.clock()
    worker = worker_loop.snapshot(s, datetime.fromisoformat(now))
    summary = ("最近一次完成后进入等待，当前存活未验证" if worker.get("stage") in
               worker_loop.WAITING_STAGES else "最近一次完整处理循环")
    items = [entry("worker", "runtime", "聊天处理进程", worker["state"],
                   worker.get("completed_at"), summary, **worker)]
    for row in s.rows("SELECT scenario,state,COUNT(*) count,SUM(billed_micro_usd) amount "
                      "FROM model_usage GROUP BY scenario,state ORDER BY scenario,state"):
        items.append(entry("usage:" + row["scenario"] + ":" + row["state"], "budget",
                           row["scenario"], row["state"], count=row["count"],
                           billed_micro_usd=row["amount"]))
    for table, key, states in (
        ("jobs", "job_id", "'FAILED','CANCELLED','RETRY_WAIT'"),
        ("outbox", "message_id", "'FAILED','DELIVERY_UNCERTAIN','RETRY_WAIT'"),
        ("exploration_requests", "request_id", "'FAILED','UNKNOWN','CANCELLED'"),
    ):
        fields = ",action_type" if table == "outbox" else ""
        for row in s.rows(f"SELECT {key} id,state,last_error_code,updated_at{fields} FROM {table} "
                          f"WHERE state IN ({states}) ORDER BY rowid DESC LIMIT 20"):
            delivery = {}
            if table == "outbox":
                from companion import delivery_reconciliation

                supported = row["state"] == "DELIVERY_UNCERTAIN" and row["action_type"] in {
                    "qq.private.reply", "qq.private.proactive", "qq.private.poke",
                }
                ready = (delivery_reconciliation.readiness(s, core.settings, row["id"], now=now)
                         if supported else {"prepared": False, "reason": None})
                delivery = {"action_type": row["action_type"],
                            "can_reconcile": supported and ready["prepared"],
                            "reconciliation_prepared": ready["prepared"]}
                if supported and not ready["prepared"]:
                    delivery["reconciliation_reason"] = (
                        "需先停止发送进程并准备原投递的核对凭证。"
                        if ready["reason"] == "DELIVERY_QUIESCENCE_REQUIRED"
                        else "原投递凭证或账本状态尚未核对一致，请查看运行记录。"
                    )
            items.append(entry(row["id"], table, row["id"], row["state"], row["updated_at"],
                               redact(row["last_error_code"] or "", core.settings),
                               **delivery))
    return collection(core, items)


def _table_exists(store, name):
    return bool(store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def decisions(core, limit=40):
    """Recorded decisions with their trigger, options, outcome and the bounded facts they used.

    Only native decision records are read. Ordinary replies and proactive chat do not keep a
    per-turn decision trace yet, and the model's reasoning is not requested, so neither is shown.
    """
    s, items = core.store, []
    for row in s.rows(
        "SELECT w.*,g.topic FROM wake_decisions w LEFT JOIN goals g "
        "ON g.goal_id=w.goal_id AND g.redacted=0 "
        "ORDER BY w.created_at DESC LIMIT ?", (limit,),
    ):
        snapshot = json.loads(row["budget_snapshot"] or "{}")
        items.append(entry(row["decision_id"], "wake", "自主活动调度", row["decision"],
                           row["created_at"], row["reason_code"],
                           trigger="event" if row["trigger_event_id"] else "schedule",
                           options=["EXPLORE", "OBSERVE", "SKIP"],
                           daily_runs=snapshot.get("daily_runs"), limit=snapshot.get("limit"),
                           goal=row["topic"]))
    for row in s.rows(
        "SELECT c.candidate_id,c.state,c.reason_code,c.created_at,c.message_id,o.state AS sent "
        "FROM social_candidates c LEFT JOIN outbox o USING(message_id) "
        "WHERE c.state<>'ERASED' ORDER BY c.created_at DESC LIMIT ?", (limit,),
    ):
        items.append(entry(row["candidate_id"], "share", "主动分享候选", row["state"],
                           row["created_at"], row["reason_code"] or "",
                           options=["SHARE", "SKIP"], delivery_state=row["sent"]))
    if _table_exists(s, "social_autonomy_plans"):
        for row in s.rows(
            "SELECT plan_id,state,last_error_code,created_at FROM social_autonomy_plans "
            "WHERE redacted=0 ORDER BY created_at DESC LIMIT ?", (limit,),
        ):
            items.append(entry(row["plan_id"], "account", "QQ 资料改动", row["state"],
                               row["created_at"], row["last_error_code"] or "",
                               options=["PLAN", "SKIP"]))
    for row in s.rows(
        "SELECT r.run_id,r.state,r.last_error_code,r.local_date,r.created_at,"
        "(SELECT COUNT(*) FROM reflection_suggestions x WHERE x.run_id=r.run_id "
        "AND x.state<>'REDACTED') suggestions,"
        "(SELECT COUNT(*) FROM reflection_evidence e WHERE e.run_id=r.run_id) sources "
        "FROM reflection_runs r ORDER BY r.created_at DESC LIMIT ?", (limit,),
    ):
        adjustments = s.one(
            "SELECT COUNT(*) n FROM capability_overrides c JOIN reflection_suggestions x "
            "USING(suggestion_id) WHERE x.run_id=?", (row["run_id"],),
        )["n"] if _table_exists(s, "capability_overrides") else 0
        items.append(entry(row["run_id"], "reflection", "每日反思", row["state"],
                           row["created_at"], row["last_error_code"] or "",
                           options=["SUGGEST", "SKIP"], local_date=row["local_date"],
                           suggestions=row["suggestions"], sources=row["sources"],
                           adjustments=adjustments))
    items.sort(key=lambda item: item["occurred_at"] or "", reverse=True)
    return {**collection(core, items[:limit]),
            "reasoning": {"state": "NOT_REQUESTED",
                          "provider_thinking": (
                              "chat:" + core.settings.thinking_effort("chat")
                              if core.settings.thinking_effort("chat")
                              else "disabled" if core.settings.provider == "deepseek"
                              else "unknown")}}
