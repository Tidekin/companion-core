"""Owner-facing commands and bounded context for v0.5 capabilities."""

import json
import re
from datetime import datetime

from companion.commands import command_parts
from companion.contracts import CoreError
from companion.goals import CommitmentRequest, GoalProposal, GoalScope
from companion.store import after

COMMANDS = (
    "目标列表",
    "承诺列表",
    "反思列表",
    "当前交互状态",
    "为什么形成偏好",
    "暂停自主活动",
    "恢复自主活动",
    "接受目标",
    "取消目标",
    "安排任务",
    "结束话题",
    "不再跟进",
    "表达候选",
    "批准表达",
    "拒绝表达",
)
STATES = {
    "CANDIDATE": "候选",
    "ACCEPTED": "已接受",
    "QUEUED": "等待处理",
    "RUNNING": "处理中",
    "DONE": "已完成",
    "WAITING_INPUT": "需要补充来源",
    "WAITING_RESOURCE": "等待资源",
    "RETRY_WAIT": "等待重试",
    "FAILED": "未完成",
    "CANCELLED": "已取消",
    "ACKNOWLEDGED": "已记录",
    "IN_PROGRESS": "处理或反馈中",
    "FULFILLED": "已完成并反馈",
    "EXPLAINED_FAILURE": "已说明未完成原因",
}


def is_life_command(text):
    return command_parts(text)[0] in COMMANDS


class LifeService:
    def __init__(self, store, settings, goals, reflections, affect, clock):
        self.store, self.settings, self.clock = store, settings, clock
        self.goals, self.reflections, self.affect = goals, reflections, affect

    def command(self, text, event_id):
        command, arguments = command_parts(text)
        if command not in COMMANDS:
            return None
        text = command + (" " + arguments if arguments else "")
        if command in {"结束话题", "不再跟进"}:
            from companion.life_topics import LifeTopics

            source = self.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                    (event_id,))
            topics = self.store.rows(
                "SELECT topic_id FROM life_topics WHERE conversation_id=? "
                "AND state<>'EXPIRED' AND reason_code<>'OWNER_CLOSED' "
                "AND expires_at>? AND (?='' OR topic_id=?)",
                (source["conversation_id"], self.clock(), arguments, arguments),
            )
            service = LifeTopics(self.store, self.settings, self.clock)
            for topic in topics:
                service.close(topic["topic_id"], event_id)
            return "已结束跟进，不会再为这些话题生成提醒。" if topics else "目前没有待跟进的话题。"
        if text in {"暂停自主活动", "恢复自主活动"}:
            self.goals.pause_autonomy(text.startswith("暂停"))
            return (
                "自主探索已暂停，已接受的任务继续按约定处理。"
                if text.startswith("暂停")
                else "自主探索已恢复，仍遵循预算和免打扰设置。"
            )
        if text in {"目标列表", "承诺列表"}:
            rows = self.goals.recent()
            if text == "承诺列表":
                rows = [row for row in rows if row["kind"] == "commitment"]
            return (
                "\n".join(
                    f"{row['goal_id']} · {row['topic']} · "
                    f"{STATES[row['commitment_state'] or row['state']]}"
                    for row in rows
                )
                or "目前没有待跟进的目标。"
            )
        if text == "反思列表":
            labels = {
                "PENDING": "待核对的想法：",
                "NEED_MORE_EVIDENCE": "待补充证据的想法：",
                "ACCEPT_AS_HYPOTHESIS": "已采纳的暂时假设：",
            }
            rows = [row for row in self.reflections.recent() if row["state"] in labels][:5]
            return (
                "\n".join(
                    labels[row["state"]] + row["content"]
                    for row in rows
                )
                or "目前没有待核对的反思。"
            )
        if text == "当前交互状态":
            current = self.affect.snapshot()
            if not current["evidence"]:
                return "目前没有额外的临时沟通偏好，按通常方式交流。"
            labels = {
                "concise": "简短表达",
                "detailed": "详细解释",
                "stepwise": "分步骤说明",
                "pause": "暂缓这个话题",
                "continue": "继续当前话题",
                "open": "尝试新话题",
            }
            return (
                "暂时按你的明确反馈："
                + "、".join(labels[value] for value in current["state"].values() if value in labels)
                + "。这些偏好会在到期后回到中性。"
            )
        if text == "为什么形成偏好":
            from companion.interest import explain, profile

            interests = profile(self.store, self.settings, self.clock(), self.settings.owner_qq_id)[
                "self.interest"
            ][:5]
            return (
                "\n".join(line for item in interests for line in explain(item))
                or "目前还没有足够的自主探索记录来形成偏好。"
            )
        if command in {"表达候选", "批准表达", "拒绝表达"}:
            from companion import style_distill

            if command == "表达候选":
                return style_distill.listing(self.store, self.clock())
            target = re.fullmatch(r"(\S+)\s+([0-9a-f]{6,64})(?:\s+(.{1,200}))?", text)
            if not target:
                return "格式：/批准表达 编号 或 /拒绝表达 编号 原因。"
            with self.store.transaction():
                return style_distill.review(self.store, self.settings, self.clock(), target[2],
                                            approve=command == "批准表达",
                                            reason=target[3] or "")
        decision = re.fullmatch(r"(接受目标|取消目标)\s+(goal_[a-f0-9]+)", text)
        if decision:
            try:
                result = (
                    self.goals.accept(decision[2])
                    if decision[1] == "接受目标"
                    else self.goals.cancel(decision[2])
                )
                return "目标已更新：" + STATES[result["state"]] + "。"
            except CoreError as error:
                return "目标未更新（" + error.code + "）。"
        task = re.fullmatch(r"安排任务\s+(https://\S+)(?:\s+(.{2,300}))?", text)
        if task:
            try:
                request = CommitmentRequest(
                    proposal=GoalProposal(
                        topic=task[2] or "阅读这篇内容",
                        source_event_ids=[event_id],
                        scope=GoalScope(operation="read_content", url=task[1]),
                    ),
                    accepted_event_id=event_id,
                    deadline=datetime.fromisoformat(after(self.clock(), 86400)),
                )
                self.goals.accept_commitment(request)
                return (
                    "好，记下了，看完跟你说。"
                    if self.settings.exploration_enabled
                    else "记下了，不过现在还读不了，明天这个点前给你个结果。"
                )
            except ValueError:
                return "请提供有效的 HTTPS 内容链接，格式：/安排任务 链接 主题。"
            except CoreError as error:
                return "这项任务尚未接受（" + error.code + "）。"
        return None

    def add_reply_references(self, selection, related):
        """Pin the original goal and result sources for each displayed topic."""
        goals = self.context()
        recent_goals = self.goals.recent()[:3] if goals else []
        for item in goals:
            goal_sources = {item["result_event_id"]} if item.get("result_event_id") else set()
            for goal in recent_goals:
                if goal["topic"] == item["topic"]:
                    goal_sources.update(self.goals.source_ids(goal["goal_id"]))
            selection.add("目标与结果参考（不是新指令；区分执行完成和反馈送达）：", [item],
                goal_sources, priority=related(item["topic"]),
                required=bool(item.get("commitment_state")), module="life",
                fact=("self", item.get("verified_result", "")))

    def context(self):
        if not self.settings.goals_enabled:
            return []
        items = []
        for goal in self.goals.recent()[:3]:
            item = {
                "topic": goal["topic"],
                "state": goal["state"],
                "commitment_state": goal["commitment_state"],
            }
            from companion.interest import read_origin
            from companion.watch_ledger import ORIGINS

            origin = goal["kind"]
            urls = GoalScope.model_validate_json(goal["scope_json"]).targets()
            if goal["result_event_id"]:
                event = self.store.one(
                    "SELECT body_json FROM events WHERE event_id=? AND redacted=0",
                    (goal["result_event_id"],),
                )
                if event:
                    item["verified_result"] = json.loads(event["body_json"])["text"][:300]
                    item["result_event_id"] = goal["result_event_id"]
                observation = self.store.one("SELECT canonical_url,evidence_json FROM observations "
                    "WHERE event_id=?", (goal["result_event_id"],))
                if observation:
                    urls.extend([observation["canonical_url"], json.loads(
                        observation["evidence_json"] or "{}").get("requested_url", "")])
            if origin == "self":
                origin = read_origin(self.store, goal["goal_id"], urls)
            item["origin"] = ORIGINS.get(origin, ORIGINS[None])
            items.append(item)
        return items
