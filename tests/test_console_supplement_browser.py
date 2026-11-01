"""Supplemental console controls use real TLS, browser actions and native receipts."""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from companion.affect import NEUTRAL, feedback_reset_at
from companion.console_cognition import tool_scope
from companion.executive_protocol import ExecutorEndpoint, ToolArguments, ToolRequest
from companion.source_grants import grant_tombstone
from tests.conftest import run
from tests.test_console_browser import console_page, navigate, submit_dialog
from tests.test_executive import FakeExecutor
from tests.test_executive import configured as configure_executive
from tests.test_executive import proposal as tool_proposal
from tests.test_growth_foundations import complete_read, view
from tests.test_life_topics import bug, due
from tests.test_life_topics import proposal as life_proposal
from tests.test_m1 import remember
from tests.test_next_life_growth import feedback_days, snapshot
from tests.test_source_grants import accepted, derived_records
from tests.test_source_grants import prepare as prepare_grant

pytestmark = pytest.mark.skipif(
    os.environ.get("COMPANION_TEST_BROWSER") != "1", reason="Explicit isolated browser run"
)


def test_memory_candidates_show_in_pending_and_badges_refresh_after_confirmation(core, tmp_path):
    core.clock.now = datetime.now(UTC)
    memory_id = remember(core, 1, "主人喜欢天文主题", confirmed=False)
    before = core.store.one("SELECT * FROM memories WHERE memory_id=?", (memory_id,))

    async def scenario():
        async with console_page(core, tmp_path) as page:
            badge = page.locator("#nav a[href='#memories'] .badge")
            await badge.wait_for()
            assert await badge.inner_text() == "1"
            await page.locator("#nav a[href='#memories']").click()
            await page.locator("#page[aria-busy=false]").wait_for()
            card = page.locator(".mem").filter(has_text="主人喜欢天文主题")
            await card.wait_for()
            assert "候选记忆已自动保存，可在对话中参考" in await page.locator("#page").inner_text()
            pending = page.locator(".toolbar button[aria-pressed=true]").filter(has_text="待确认")
            assert await pending.locator(".count").inner_text() == "1"
            unchanged = core.store.one("SELECT * FROM memories WHERE memory_id=?", (memory_id,))
            assert unchanged == before
            await card.get_by_role("button", name="确认", exact=True).click()
            await submit_dialog(page, "确认", f"/memories/{memory_id}/decision")
            await page.get_by_role("heading", name="没有待确认的记忆", exact=True).wait_for()
            await badge.wait_for(state="detached")
            assert await pending.locator(".count").count() == 0
            assert core.store.one("SELECT status FROM memories WHERE memory_id=?", (memory_id,))[
                "status"] == "CONFIRMED"
            operations = core.store.one(
                "SELECT COUNT(*) n FROM console_operations WHERE state='COMPLETE'")
            assert operations["n"] == 1
            assert not core.store.rows("SELECT * FROM model_usage")

    run(scenario())


def test_mood_console_shows_sources_trend_and_controls(core, tmp_path):
    from pathlib import Path

    from tests.test_mood import feedback

    core.clock.now = datetime.now(UTC)
    feedback(core)
    for number, text in enumerate((
        "这个想法很有意思", "这个问题让我很好奇", "这个角度我真没想到",
        "这个观点我不认同", "这样就说得通了",
    ), 2):
        core.clock.advance(1)
        feedback(core, number, text)
    directory = Path(__file__).parents[1] / ".local/test-results"
    directory.mkdir(parents=True, exist_ok=True)

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await page.evaluate("location.hash = 'self'")
            await page.locator("#page[aria-busy=false]").wait_for()
            card = page.locator(".sheet").filter(has=page.get_by_role(
                "heading", name="小肥鱼的心情", exact=True))
            await card.wait_for()
            assert "半衰期 3 小时" in await card.inner_text()
            await card.get_by_text("最近趋势与变化来源", exact=True).click()
            assert "明确的正向互动反馈" in await card.inner_text()
            for reason in ("主人明确觉得刚才的想法有趣", "主人明确想继续探讨问题",
                           "刚才的新角度带来明确反馈", "主人明确提出观点分歧",
                           "主人明确反馈讨论有进展"):
                assert reason in await card.inner_text()
            await page.screenshot(path=str(directory / "mood-console-desktop.png"), full_page=True)
            for label in ("暂停心情滤镜", "恢复心情滤镜", "重新计算心情"):
                await page.get_by_role("button", name=label, exact=True).click()
                await page.get_by_role("dialog").get_by_role(
                    "button", name=label, exact=True).click()
                await page.get_by_role("dialog").wait_for(state="hidden")
                await page.locator("#page[aria-busy=false]").wait_for()
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.locator("#side").evaluate(
                "node => { getComputedStyle(node).transform; return Promise.all("
                "node.getAnimations().map(a => a.finished.catch(() => {}))); }")
            await page.locator("#toast").wait_for(state="hidden")
            await page.get_by_text("最近趋势与变化来源", exact=True).click()
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth")
            await page.evaluate("window.scrollTo({top: 0, left: 0, behavior: 'instant'})")
            await page.screenshot(path=str(directory / "mood-console-mobile.png"), full_page=True)
            await page.get_by_role("button", name="清除心情", exact=True).click()
            await page.get_by_role("dialog").get_by_role(
                "button", name="清除心情", exact=True).click()
            await page.get_by_role("dialog").wait_for(state="hidden")
            await page.locator("#page[aria-busy=false]").wait_for()
            await page.get_by_text("最近趋势与变化来源", exact=True).click()
            assert "暂无有效的变化来源" in await page.locator("#page").inner_text()

    run(scenario())


def test_console_shows_saved_provider_reasoning_as_plain_text(core, tmp_path):
    from pathlib import Path

    from tests.test_model_reasoning import reasoning_chat

    core.clock.now = datetime.now(UTC)
    text = "<script>window.reasoningInjected=true</script>\n合成私人推理"
    _, _, usage, _ = reasoning_chat(core, tmp_path, reasoning=text)

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await page.evaluate("id => { location.hash = 'thinking/model:' + id; }",
                                usage[0]["request_id"])
            await page.locator("#page[aria-busy=false]").wait_for()
            await page.get_by_role("tab", name="原始推理", exact=True).click()
            content = page.get_by_label("原始推理内容", exact=True)
            assert await content.inner_text() == text + "1"
            assert await page.evaluate("window.reasoningInjected") is None
            directory = Path(__file__).parents[1] / "output/playwright/reasoning"
            directory.mkdir(parents=True, exist_ok=True)
            await page.locator(".dk-card").screenshot(path=str(directory / "desktop.png"))
            await page.set_viewport_size({"width": 375, "height": 812})
            await page.evaluate("async () => { await Promise.all(document.getAnimations()"
                                ".map(animation => animation.finished.catch(() => {}))); }")
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.locator(".dk-card").screenshot(path=str(directory / "mobile.png"))

    run(scenario())


def test_console_model_measurement_distinguishes_local_counts_and_unknown_usage(core, tmp_path):
    from pathlib import Path

    from tests.test_context_meter_inspector import recorded_attempts

    core.clock.now = datetime.now(UTC)
    run(recorded_attempts(core, tmp_path, unknown=True))
    attempts = core.store.rows("SELECT request_id,state FROM model_usage ORDER BY rowid")

    async def scenario():
        async with console_page(core, tmp_path) as page:
            for attempt in attempts:
                await page.evaluate("id => { location.hash = 'thinking/model:' + id; }",
                                    attempt["request_id"])
                await page.locator("#page[aria-busy=false]").wait_for()
                panel = page.locator(".steps-view").filter(has_text="本次请求计量")
                await panel.wait_for()
                assert "固定渲染器本地计数" in await panel.inner_text()
                assert "上下文区域分布与裁剪原因未记录" in await panel.inner_text()
                usage = panel.locator(".kv").filter(has_text="实际输入 / 输出")
                assert ("未记录" if attempt["state"] == "UNKNOWN" else "100 / 4") in (
                    await usage.inner_text())
                assert "SYNTHETIC_PRIVATE_TOOL_RESULT" not in (
                    await page.locator("#page").inner_text())
            # Retain one synthetic screenshot for the local verification receipt.
            target = Path(__file__).parents[1] / ".local/test-results/context-meter-browser.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(target), full_page=True)

    run(scenario())


def test_console_shows_cache_three_states_from_the_backend_projection(core, tmp_path):
    from tests.test_cache_usage_presence import ABSENT
    from tests.test_cache_usage_presence import attempt as cache_attempt
    from tests.test_paid_model_gate import approved_live

    approved_live(core, tmp_path)
    core.clock.now = datetime.now(UTC)
    rows = [cache_attempt(core, cache=cache, number=n)[1]
            for n, cache in enumerate([ABSENT, 0, 40], start=1)]
    # The financial column keeps its settlement default; only cache_usage may show a value.
    assert [row["cached_input_tokens"] for row in rows] == [0, 0, 40]

    async def scenario():
        async with console_page(core, tmp_path) as page:
            for row, expected in zip(rows, ["未记录", "0", "40"], strict=True):
                await page.evaluate("id => { location.hash = 'thinking/model:' + id; }",
                                    row["request_id"])
                await page.locator("#page[aria-busy=false]").wait_for()
                panel = page.locator(".steps-view").filter(has_text="本次请求计量")
                cached = panel.locator(".kv").filter(has_text="缓存命中输入")
                assert (await cached.locator("span").last.inner_text()) == expected
                usage = panel.locator(".kv").filter(has_text="实际输入 / 输出")
                assert "80 / 3" in await usage.inner_text()
                fee = panel.locator(".kv").filter(has_text="已计或预留费用")
                assert "未记录" not in await fee.inner_text()
            overview = page.locator(".meter")
            # 40 of the 160 inputs whose cache was recorded; the unknown request is excluded.
            assert "缓存命中 25%" in await overview.inner_text()
            assert await page.locator(".m-bar").count() == 3

    run(scenario())


def assert_receipts(core, states):
    receipts = core.store.rows("SELECT state,result_json FROM console_operations ORDER BY rowid")
    assert all(item["state"] == "COMPLETE" for item in receipts)
    # These native actions intentionally return no business identifier to the UI.
    assert [json.loads(item["result_json"]) for item in receipts] == [
        {"state": state} for state in states]


def test_console_source_grant_revoke_retains_history_then_forget_erases_it(core, tmp_path):
    core.clock.now = datetime.now(UTC)
    grant = prepare_grant(core)
    event_id = accepted(core, grant)
    derived_records(core, event_id)

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await navigate(page, "阅读与文件")
            await page.get_by_role("button", name="来源授权", exact=True).click()
            row = page.locator("tbody tr").filter(has_text="读取程序 heybox")
            assert await row.get_by_text("有效", exact=True).count() == 1
            path = f"/source-grants/{grant.grant_id}"
            await row.get_by_role("button", name="撤回", exact=True).click()
            await submit_dialog(page, "撤回授权", path)
            saved = core.store.one("SELECT * FROM source_grants WHERE grant_id=?",
                                   (grant.grant_id,))
            assert saved["revoked_at"] and saved["forgotten_at"] is None
            assert not core.store.one("SELECT redacted FROM events WHERE event_id=?",
                                      (event_id,))["redacted"]
            assert core.store.one("SELECT content FROM memories")["content"] == (
                "Private derived memory")
            assert await row.get_by_text("已撤回", exact=True).count() == 1
            assert await row.get_by_role("button", name="撤回", exact=True).count() == 0
            assert_receipts(core, ["COMPLETE"])

            await row.get_by_role("button", name="遗忘", exact=True).click()
            await submit_dialog(page, "遗忘这个授权", path)
            assert core.store.one("SELECT forgotten_at FROM source_grants WHERE grant_id=?",
                                  (grant.grant_id,))["forgotten_at"]
            assert core.store.one("SELECT redacted FROM events WHERE event_id=?",
                                  (event_id,))["redacted"]
            assert core.store.one("SELECT status,content FROM memories") == {
                "status": "FORGOTTEN", "content": ""}
            assert core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                                  (grant_tombstone(grant.grant_id),))
            assert await row.get_by_text("已遗忘", exact=True).count() == 1
            assert await row.get_by_role("button").count() == 0
            assert_receipts(core, ["COMPLETE", "COMPLETE"])
            assert core.model.calls == []

    run(scenario())


def test_console_can_close_consumed_topic_and_invalidate_its_retained_shadow(core, tmp_path):
    core.clock.now = datetime.now(UTC) - timedelta(days=1)
    topic = bug(core)
    life_proposal(core)
    assert run(core.run_once(job_id=due(core)))
    assert core.store.one("SELECT state,reason_code FROM life_topics") == {
        "state": "CLOSED", "reason_code": "CONSUMED"}
    draft = core.store.one("SELECT * FROM life_activity_results")
    assert draft["state"] == "SHADOW" and draft["text"]
    usage = core.store.rows("SELECT * FROM model_usage")
    calls = list(core.model.calls)

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await navigate(page, "日记与成长")
            card = page.locator(".topic").filter(has_text="导出时还有一个没有解决的Bug")
            assert draft["text"] in await card.inner_text()
            assert "她已经做了决定" in await card.inner_text()
            await card.get_by_role("button", name="关闭话题", exact=True).click()
            await page.get_by_label("以哪句话作为关闭依据").select_option("evt_test_1")
            await submit_dialog(page, "关闭话题", f"/life/topics/{topic['topic_id']}/close")
            assert core.store.one("SELECT state,reason_code,closed_event_id FROM life_topics") == {
                "state": "CLOSED", "reason_code": "OWNER_CLOSED", "closed_event_id": "evt_test_1"}
            assert core.store.one("SELECT state,text,reason FROM life_activity_results") == {
                "state": "INVALIDATED", "text": "", "reason": ""}
            assert await card.get_by_role("button", name="关闭话题", exact=True).count() == 0
            assert draft["text"] not in await card.inner_text()
            assert_receipts(core, ["CLOSED"])
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert core.model.calls == calls
            assert not core.store.rows("SELECT * FROM outbox")

    run(scenario())


def test_console_affect_reset_clears_feedback_but_retains_preferences_and_interests(core, tmp_path):
    core.clock.now = datetime.now(UTC) - timedelta(days=2)
    core.settings.affect_enabled = True
    feedback_days(core, count=3)
    memory_id = remember(core, 100, "我喜欢天文主题", confirmed=True)
    complete_read(core, 200, "https://example.org/console-browser-astronomy")
    assert core.affect.snapshot()["state"]["interaction_tone"] == "concise"
    learned = snapshot(core)["items"][0]
    assert learned["independent_days"] == 3 and learned["status"] == "EMERGING"
    interests = view(core)
    events = core.store.rows("SELECT event_id,body_json,redacted FROM events ORDER BY rowid")

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await navigate(page, "情绪与偏好")
            affect = page.locator(".affect-dim").filter(has_text="回复风格")
            assert await affect.locator("b").inner_text() == "简短一点"
            preference = page.locator(".sheet").filter(
                has=page.get_by_role("heading", name="你的偏好", exact=True))
            interest = page.locator(".sheet").filter(
                has=page.get_by_role("heading", name="她的兴趣", exact=True))
            expression = page.locator(".sheet").filter(
                has=page.get_by_role("heading", name="表达习惯学习", exact=True))
            assert "我喜欢天文主题" in await preference.inner_text()
            assert "来自你的 1 条原话" in await preference.inner_text()
            assert "天文研究" in await interest.inner_text()
            assert "读完 1 篇，分布在 1 天" in await interest.inner_text()
            assert "回复风格：简短一点" in await expression.inner_text()
            assert "有 3 天的明确反馈" in await expression.inner_text()
            assert "正在形成" in await expression.inner_text()

            await page.get_by_role("button", name="重置交流偏好", exact=True).click()
            await submit_dialog(page, "重置", "/affect/reset")
            assert core.affect.snapshot()["state"] == NEUTRAL
            assert not core.affect.snapshot()["evidence"]
            assert snapshot(core)["items"] == [] and feedback_reset_at(core.store)
            assert await affect.locator("b").inner_text() == "默认"
            assert "还没有可以学习的反馈" in await expression.inner_text()
            assert core.store.rows(
                "SELECT event_id,body_json,redacted FROM events ORDER BY rowid") == events
            assert core.store.one("SELECT status FROM memories WHERE memory_id=?",
                                  (memory_id,))["status"] == "CONFIRMED"
            after = view(core)
            assert after["privacy_epoch"] == interests["privacy_epoch"] + 1
            for kind in ("owner.preference", "self.interest"):
                assert after[kind] == interests[kind]
            assert_receipts(core, ["RESET"])
            assert core.model.calls == []

    run(scenario())


def test_console_tools_approve_exact_scope_then_revoke_and_reject_without_dispatch(core, tmp_path):
    core.clock.now = datetime.now(UTC)
    executor = FakeExecutor()
    executive = configure_executive(core, executor, allow_changes=True)
    core.executive.adapter = executor
    approved = executive.submit(tool_proposal(2, "windows.write_text", text="待批准的合成文件正文"))
    rejected = executive.submit(tool_proposal(3, "windows.write_text",
                                             text="待拒绝的另一份合成正文"))

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await navigate(page, "执行任务")
            approve_card = page.locator(".tool-card").filter(has_text="待批准的合成文件正文")
            assert "result.txt" in await approve_card.inner_text()
            await approve_card.get_by_role("button", name="批准", exact=True).click()
            await page.get_by_role("checkbox", name="我已经看过写入文件的具体内容").check()
            await submit_dialog(page, "批准执行", f"/tools/{approved['task_id']}/decision")
            assert executive.get(approved["task_id"])["state"] == "QUEUED"
            approval = core.store.one("SELECT * FROM tool_approvals WHERE task_id=?",
                                      (approved["task_id"],))
            assert approval["decision"] == "APPROVE"
            assert approval["scope_sha256"] == approved["scope_sha256"]
            assert_receipts(core, ["QUEUED"])

            await approve_card.get_by_role("button", name="撤回", exact=True).click()
            await submit_dialog(page, "撤回任务", f"/tools/{approved['task_id']}/revoke")
            assert executive.get(approved["task_id"])["state"] == "CANCELLED"
            assert executive.get(approved["task_id"])["last_error_code"] == "OWNER_REVOKED"
            assert core.store.one("SELECT revoked_at FROM tool_approvals WHERE task_id=?",
                                  (approved["task_id"],))["revoked_at"]

            reject_card = page.locator(".tool-card").filter(has_text="待拒绝的另一份合成正文")
            await reject_card.get_by_role("button", name="拒绝", exact=True).click()
            await submit_dialog(page, "拒绝", f"/tools/{rejected['task_id']}/decision")
            assert executive.get(rejected["task_id"])["state"] == "CANCELLED"
            assert core.store.one("SELECT decision FROM tool_approvals WHERE task_id=?",
                                  (rejected["task_id"],))["decision"] == "REJECT"
            assert_receipts(core, ["QUEUED", "CANCELLED", "CANCELLED"])
            assert executor.calls == core.model.calls == []
            assert not core.store.rows("SELECT * FROM tool_tasks WHERE dispatched_at IS NOT NULL")

    run(scenario())


@pytest.mark.parametrize("field", ["text", "prompt"])
def test_console_redacted_tool_scope_disables_approval_but_allows_rejection(core, tmp_path, field):
    core.clock.now = datetime.now(UTC)
    executor = FakeExecutor()
    executive = configure_executive(core, executor, allow_changes=True)
    core.executive.adapter = executor
    private_text = "我朋友的住址（仅供测试的合成内容）"
    if field == "text":
        request = tool_proposal(2, "windows.write_text", text=private_text)
    else:
        executive.config.endpoints["dsh"] = ExecutorEndpoint(
            url="http://127.0.0.1:8992", token="synthetic-dsh-browser-token-123456",
            workspace="work", capabilities=["dsh.run"], max_cost_micro_usd=1000)
        request = ToolRequest(idempotency_key="browser-redacted-dsh", capability="dsh.run",
            workspace="work", arguments=ToolArguments(prompt=private_text),
            source_event_ids=["evt_test_1"])
    task = executive.submit(request)
    scope = tool_scope(core, task)
    assert scope["state"] == "RECORDED" and scope[field]["state"] == "REDACTED"

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await navigate(page, "执行任务")
            card = page.locator(".tool-card")
            assert "含有可能敏感的内容，不在这里显示" in await card.inner_text()
            assert private_text not in await page.locator("#page").inner_text()
            assert await card.get_by_role("button", name="批准", exact=True).is_disabled()
            assert await card.get_by_role("button", name="拒绝", exact=True).is_enabled()
            assert await card.get_by_role("button", name="撤回", exact=True).is_enabled()
            await card.get_by_role("button", name="拒绝", exact=True).click()
            await submit_dialog(page, "拒绝", f"/tools/{task['task_id']}/decision")
            assert executive.get(task["task_id"])["state"] == "CANCELLED"
            assert core.store.one("SELECT decision,scope_sha256 FROM tool_approvals") == {
                "decision": "REJECT", "scope_sha256": task["scope_sha256"]}
            assert_receipts(core, ["CANCELLED"])
            assert executor.calls == core.model.calls == []

    run(scenario())


def synthetic_call(number, at, job="job_meter"):
    """A model-call projection with the native field shape; only the frontend reads it."""
    return {"id": f"model_meter_{number}", "state": "SETTLED", "job_id": job,
            "occurred_at": at.isoformat(), "source_event_ids": [], "reason_code": "NOT_RECORDED",
            "context_regions": "NOT_RECORDED", "trimming_reason": "NOT_RECORDED",
            "usage": {"input_tokens": 1000 * number, "output_tokens": 10, "cached_input_tokens": 0,
                      "billed_micro_usd": 500, "is_estimate": 1, "observed": True,
                      "cache_usage": {"state": "NOT_RECORDED", "cached_input_tokens": None}},
            "measurement": {"state": "NOT_RECORDED"}}


async def serve_model_pages(page, respond):
    """Replace only kind=model reads, keeping the backend's privacy epoch so pages stay coherent."""
    async def handle(route):
        url = route.request.url
        if "kind=model" not in url:
            await route.continue_()
            return
        # Synthetic cursors are unknown to the backend, so read the epoch from its first page.
        first = url.split("?", 1)[0] + "?kind=model&limit=30"
        real = await route.fetch(url=first)
        body = await real.json()
        query = dict(part.split("=", 1) for part in url.split("?", 1)[1].split("&"))
        await route.fulfill(status=200, json={**body, **respond(query)})

    await page.route("**/v1/console/cognition?*", handle)


@pytest.fixture(params=[
    pytest.param(("Asia/Shanghai", "2026-10-27T00:00:30+08:00"), id="shanghai-after-midnight"),
    pytest.param(("Asia/Shanghai", "2026-10-27T23:59:30+08:00"), id="shanghai-before-midnight"),
    pytest.param(("UTC", "2026-10-27T00:00:30+00:00"), id="utc-after-midnight"),
])
def model_chart_clock(core, request):
    core.settings.timezone, instant = request.param
    core.clock.now = datetime.fromisoformat(instant)
    return core.clock.now


def test_today_tokens_page_past_empty_pages_and_never_report_zero_when_unread(
    core, tmp_path, model_chart_clock,
):
    today = synthetic_call(1, model_chart_clock)
    yesterday = synthetic_call(2, model_chart_clock - timedelta(days=2))

    def filtered_first_page(query):
        # The first page is empty after source filtering but still has a cursor.
        if "before" not in query:
            return {"items": [], "next_cursor": "c1", "truncated": True}
        return {"items": [today, yesterday], "next_cursor": None, "truncated": False}

    def never_ending(query):
        return {"items": [], "next_cursor": "c" + query.get("before", "0"), "truncated": True}

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await page.clock.set_fixed_time(model_chart_clock)
            card = page.locator(".sheet.tok")
            await serve_model_pages(page, filtered_first_page)
            await page.get_by_role("button", name="刷新当前页面").click()
            await card.filter(has_text="模型调用").locator(".tok-stat b").first.filter(
                has_text="1").wait_for()
            text = await card.inner_text()
            assert "今天还没有模型调用" not in text and "不完整" not in text
            # Drain callbacks before replacing the paginated response source.
            await page.unroute_all(behavior="wait")
            await serve_model_pages(page, never_ending)
            await page.get_by_role("button", name="刷新当前页面").click()
            await card.filter(has_text="没有全部读到").wait_for()
            text = await card.inner_text()
            assert "今天还没有模型调用" not in text and "¥0" not in text and "$0" not in text
            assert "未读完" in text

    run(scenario())


def test_located_old_call_keeps_time_order_and_day_chart_ends_today(
    core, tmp_path, model_chart_clock,
):
    now = model_chart_clock
    # Keep both recent calls on the fixed local date, including just after midnight.
    newest = synthetic_call(3, now)
    middle = synthetic_call(2, now.replace(hour=0, minute=0, second=0, microsecond=0))
    oldest = synthetic_call(1, now - timedelta(days=500))

    def first_page_without_oldest(query):
        if query.get("record_id") == oldest["id"]:
            return {"items": [oldest], "next_cursor": None}
        return {"items": [newest, middle], "next_cursor": "older", "truncated": True}

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await page.clock.set_fixed_time(now)
            await serve_model_pages(page, first_page_without_oldest)
            await page.evaluate("id => { location.hash = 'thinking/model:' + id; }", oldest["id"])
            await page.locator("#page[aria-busy=false]").wait_for()
            panel = page.locator(".steps-view").filter(has_text="本次请求计量")
            assert "同一任务第 1 次调用（本页共 3 次）" in await panel.inner_text()
            bars = page.locator(".m-chart .m-bar")
            assert await bars.count() == 3
            assert "sel" in await bars.first.get_attribute("class")
            # The list keeps the newest call first even though the old one was located separately.
            first_row = page.locator(".dk-list button").first
            assert await first_row.get_attribute("data-record-id") == newest["id"]
            await page.locator(".m-chart .seg").get_by_role("button", name="按天").click()
            day = await page.evaluate("() => new Intl.DateTimeFormat('en-CA', {timeZone: "
                                      "state.tz, year: 'numeric', month: '2-digit', "
                                      "day: '2-digit'}).format(new Date())")
            assert day == now.date().isoformat()
            assert await bars.count() == 30
            label = await bars.last.get_attribute("aria-label")
            assert label.startswith(day) and "2 次请求" in label
            assert "图中只画最近 30 天" in await page.locator(".m-chart").inner_text()

    run(scenario())
