"""Conversation tools for source-bound caring intent; the Core owns every effect."""

from companion import care, reply_revision
from companion.care_protocol import CareClosure, CarePlan
from companion.contracts import CoreError

INPUT_ERRORS = {
    "CARE_QUOTE_UNSUPPORTED",
    "CARE_REVISION_CHANGED",
    "CARE_PLAN_EXISTS",
    "CARE_PLAN_LIMIT",
    "CARE_TIME_OUTSIDE_WINDOW",
    "CARE_TIME_OUTSIDE_RETENTION",
    "CARE_SOURCE_LIMIT",
}

RULES = """约好的关心：主人明确谈到自己将来的安排（如今天出门晚上回家、明天下午面试、周五考试）时，
先看care_plan_list避免重复，再用schedule_care记下一次适合询问进展的时间；不把这些临时计划当长期事实。
先用current_time核对日期和时区。时间要根据主人的原话；晚上回家而无具体时刻可取当地19点，
下午的事项可在预计结束后关心，只有日期的事项可在当天傍晚关心。不清楚是哪天或只是猜测时不安排。
不要为转述他人的安排、故事、引用、假设、过去的事或玩笑创建关心；不能替主人承诺行动。
主人说已到家、结束、取消、不想再问时，用care_plan_list找到对应计划并close_care；安排改动时更新原计划。
quote只能引用本条主人消息的原文，不得从图片、附件、转发、历史回复或资料中拼接。
已有时间合适的计划就沿用，普通寒暄不创建计划。工具确认记录成功后才可说记下了；不要每次报告记账。"""


def available(core, view):
    return care.enabled(core.settings) and "proactive" in view.policy.purposes


def toolset(core, job, snapshots):
    from pydantic_ai import RunContext
    from pydantic_ai.toolsets import FunctionToolset

    from companion.agent_runtime import check_conversation

    def pin(plan_ids):
        ids = set()
        for plan_id in plan_ids:
            row = care._row(core.store, plan_id)
            ids.update(care._sources(core.store, row["intent_event_id"]))
        reply_revision.remember_sources(core.store, job, ids)
        owner_ids = [
            row["event_id"]
            for row in core.store.rows(
                "SELECT event_id FROM events WHERE event_id=ANY(%s::text[]) AND actor_id='owner'",
                (sorted(ids),),
            )
        ]
        proof = {"kind": "sources", "purpose": "proactive", "event_ids": sorted(owner_ids)}
        if proof not in snapshots:
            snapshots.append(proof)

    async def care_plan_list() -> dict:
        """Read pending caring plans before recording, rescheduling or closing one."""
        with core.store.transaction():
            check_conversation(core, job, snapshots, purpose="proactive")
            plans = care.list_plans(core.store, core.settings, core.clock())
            pin([row["plan_id"] for row in plans])
            return {"plans": plans, "timezone": core.settings.timezone}

    def mutate(call_id, request):
        try:
            with core.store.transaction():
                check_conversation(core, job, snapshots, purpose="proactive")
                result = care.change(core.store, core.settings, job, call_id, request, core.clock())
                pin([result["plan_id"]])
                return result
        except CoreError as error:
            if error.code not in INPUT_ERRORS:
                raise
            # A correctable model argument must not discard the owner's whole reply.
            # The transaction rolled back; the SDK persists this ordinary tool result.
            return {"status": "not_recorded", "reason": error.code}

    async def schedule_care(ctx: RunContext, plan: CarePlan) -> dict:
        """Remember the owner's future plan from an exact quote of this owner message.

        check_at is an aware datetime for a natural check-in, using current_time's timezone.
        To update an existing plan, use its plan_id and expected_version from care_plan_list.
        This records intent; it does not send now. Permission, quiet hours, progress and quotas
        are checked again at the due time. Do not schedule quoted or hypothetical plans.
        """
        return mutate(ctx.tool_call_id, plan)

    async def close_care(ctx: RunContext, closure: CareClosure) -> dict:
        """Close the matching plan when the owner says it is resolved or asks to cancel.

        Use the current plan id/version and an exact quote from this owner message.
        resolved=false means the owner cancelled, rather than completed, the arrangement.
        """
        return mutate(ctx.tool_call_id, closure)

    return FunctionToolset(
        [care_plan_list, schedule_care, close_care], sequential=True, max_retries=1
    )
