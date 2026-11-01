"""Native console assets and owner HTTP contracts in a real, isolated browser."""

import asyncio
import json
import os
import re
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from companion import delegation, operator_config, runtime_config
from tests.conftest import run
from tests.console_proxy import ROOT, console_proxy
from tests.test_cognition_goal_steps import finish_record, retained_plan
from tests.test_consolidation import enable_nightly
from tests.test_diary import DAY
from tests.test_goals import setup_goal

pytestmark = pytest.mark.skipif(
    os.environ.get("COMPANION_TEST_BROWSER") != "1", reason="Explicit isolated browser run"
)
PASSWORD = "synthetic-console-browser-password-" * 2


async def browser_failure_evidence(page, navigation, events, proxy, tmp_path):
    directory = ROOT / "output/playwright" / (
        tmp_path.name + "-" + uuid4().hex[:8])
    directory.mkdir(parents=True)
    evidence = {"url": page.url, "navigation_status": navigation.status if navigation else None,
        "navigation_type": navigation.headers.get("content-type") if navigation else None,
        "events": list(events), "edge_exit": proxy.edge.poll(),
        "backend_exit": proxy.backend.poll() if proxy.backend else None}
    for name, capture in (
        ("page.html", page.content),
        ("page.png", lambda: page.screenshot(path=str(directory / "page.png"), timeout=5000)),
    ):
        try:
            content = await capture()
            if isinstance(content, str):
                (directory / name).write_text(content.replace(PASSWORD, "[fixture-password]"),
                                               encoding="utf-8")
        except Exception as error:
            evidence[name + "_error"] = f"{type(error).__name__}: {error}"
    for name in ("backend", "edge"):
        source = tmp_path / name / "process.log"
        if source.exists():
            (directory / (name + ".log")).write_bytes(source.read_bytes())
    (directory / "page.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    print(f"Browser failure evidence: {directory}\n" + json.dumps(evidence, ensure_ascii=False))


@asynccontextmanager
async def console_page(core, tmp_path):
    from playwright.async_api import async_playwright

    core.settings.console.enabled = True
    core.settings.console.public_origin = "https://console.example.test"
    core.settings.control_token = SecretStr(PASSWORD)
    async with console_proxy(core, tmp_path) as proxy, async_playwright() as playwright:
        url = proxy.url
        browser = await playwright.chromium.launch(
            channel=os.environ.get("COMPANION_TEST_BROWSER_CHANNEL"), headless=True)
        try:
            context = await browser.new_context(ignore_https_errors=True,
                timezone_id=core.settings.timezone, viewport={"width": 1440, "height": 1080})
            page = await context.new_page()
            events, navigation = deque(maxlen=200), None
            page.on("console", lambda message: events.append(
                {"console": message.type, "text": message.text[:2000]}))
            page.on("pageerror", lambda error: events.append({"pageerror": str(error)}))
            page.on("response", lambda response: events.append(
                {"response": response.url, "status": response.status,
                 "type": response.headers.get("content-type")}))
            page.on("requestfailed", lambda request: events.append(
                {"requestfailed": request.url, "failure": request.failure}))
            navigation = await page.goto(url + "/console/")
            await page.get_by_role("textbox", name="控制台密码").fill(PASSWORD)
            await page.get_by_role("button", name="登录", exact=True).click()
            await page.get_by_role("button", name="退出登录").wait_for()
            yield page
        except Exception as error:
            print(f"Browser failure before server cleanup: {type(error).__name__}: {error}")
            try:
                await browser_failure_evidence(page, navigation, events, proxy, tmp_path)
            except Exception as capture_error:
                print(f"Browser evidence capture failed: {type(capture_error).__name__}")
            raise
        finally:
            for context in browser.contexts:
                await context.close()
            await browser.close()


async def navigate(page, name):
    await page.get_by_role("navigation").get_by_role("link", name=name, exact=True).click()
    await page.locator("#crumb").filter(has_text=name).wait_for()
    await page.locator("#page[aria-busy=false]").wait_for()


async def submit_dialog(page, label, path):
    async with page.expect_response(lambda r: r.url.endswith(path)
        and r.request.method == "POST") as response:
        await page.get_by_role("dialog").get_by_role("button", name=label, exact=True).click()
    result = await response.value
    assert result.status == 200, await result.text()
    await page.get_by_role("dialog").wait_for(state="hidden")
    await page.locator("#page[aria-busy=false]").wait_for()


def test_console_public_web_scope_round_trips(core, tmp_path):
    core.clock.now = datetime.now(UTC)

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await navigate(page, "能力与开关")
            await page.locator('a[href="#caps/reading.web"]').click()
            await page.locator("#cf-web-scope").select_option("all")
            assert await page.locator("#cf-web-hosts").count() == 0
            await page.get_by_role("button", name="保存为 v1", exact=True).click()
            async with page.expect_response(lambda r: r.url.endswith("/configuration")
                and r.request.method == "PUT") as response:
                await page.get_by_role("dialog").get_by_role(
                    "button", name="保存 1 项修改", exact=True).click()
            result = await response.value
            assert result.status == 200, await result.text()
            await page.get_by_role("dialog").wait_for(state="hidden")
            await page.get_by_role("button", name="刷新当前页面").click()
            assert await page.locator("#cf-web-scope").input_value() == "all"
            saved = operator_config._decode(operator_config.latest(core.store), core.settings)
            assert saved["web_reading_hosts"] == ["*"]
            evidence = ROOT / "output/playwright/public-web"
            evidence.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(evidence / "all-public-domains.png"))
            await page.locator("#cf-web-scope").select_option("exact")
            await page.locator("#cf-web-hosts").fill("articles.example")
            await page.locator("#cf-web-hosts").press("Enter")
            assert await page.locator("#cf-web-scope").input_value() == "exact"

    run(scenario())


def test_console_runtime_versions_show_boundary_restart_review_and_restore(core, tmp_path):
    core.settings.operator_hot_reload = True
    core.store.configuration_worker = "worker"
    core.clock.now = datetime.now(UTC)
    evidence = ROOT / "output/playwright/runtime-configuration"
    evidence.mkdir(parents=True, exist_ok=True)

    def save(version, **changes):
        operator_config.update(core.store, core.settings,
            operator_config.ConfigurationChange(expected_version=version, changes=changes),
            f"browser-config-{version}", core.clock())

    async def scenario():
        async with console_page(core, tmp_path) as page:
            runtime_config.boundary(core.store, core.settings, core.clock)
            await navigate(page, "配置")
            guidance = "接住一个具体细节，自然地说自己的看法。\n不必每次追问。"
            editor = page.get_by_label("交流指导正文", exact=True)
            assert await editor.get_attribute("maxlength") == "2000"
            await editor.fill(guidance)
            await page.get_by_role("switch", name="对话里标注时间", exact=True).click()
            await page.get_by_role("button", name="保存为 v1", exact=True).click()
            async with page.expect_response(lambda r: r.url.endswith("/configuration")
                and r.request.method == "PUT") as response:
                await page.get_by_role("dialog").get_by_role(
                    "button", name="保存 2 项修改", exact=True).click()
            saved = await response.value
            assert saved.status == 200, await saved.text()
            await page.get_by_role("heading", name="v1 已保存 · 等待任务边界",
                                   exact=True).wait_for()
            await page.screenshot(path=str(evidence / "pending.png"))
            runtime_config.boundary(core.store, core.settings, core.clock)
            await page.get_by_role("button", name="刷新当前页面").click()
            await page.get_by_role("heading", name="v1 已保存 · 已生效（新任务）",
                                   exact=True).wait_for()
            assert await editor.input_value() == guidance
            assert (await editor.bounding_box())["width"] > (
                await page.locator("#pf-conversation_guidance").bounding_box())["width"] * .8
            await page.locator("#pf-conversation_guidance").screenshot(
                path=str(evidence / "expression-guidance.png"))
            await page.screenshot(path=str(evidence / "active.png"))
            save(1, timeout_seconds=60)
            await page.get_by_role("button", name="刷新当前页面").click()
            await page.get_by_role("heading", name="v2 已保存 · 需要重启", exact=True).wait_for()
            save(2, max_output_tokens=1024)
            await page.get_by_role("button", name="刷新当前页面").click()
            await page.get_by_role("heading", name="v3 已保存 · 需要重新审批与重启",
                                   exact=True).wait_for()
            await page.screenshot(path=str(evidence / "reapproval.png"))
            await page.locator(".vchain button").filter(has_text="v1").click()
            await page.get_by_role("button", name="重新选择此版本", exact=True).click()
            async with page.expect_response(lambda r: r.url.endswith("/configuration")
                and r.request.method == "PUT") as response:
                await page.get_by_role("dialog").get_by_role(
                    "button", name="重新选择并保存", exact=True).click()
            restored = await response.value
            assert restored.status == 200, await restored.text()
            assert (await restored.json())["version"] == 4
            await page.get_by_role("heading", name="v4 已保存 · 等待任务边界",
                                   exact=True).wait_for()
            runtime_config.boundary(core.store, core.settings, core.clock)
            await page.get_by_role("button", name="刷新当前页面").click()
            await page.get_by_role("heading", name="v4 已保存 · 已生效（新任务）",
                                   exact=True).wait_for()
            await page.screenshot(path=str(evidence / "restored.png"))

    run(scenario())


def test_console_goal_buttons_submit_original_plan_ids_and_current_version(core, tmp_path):
    core.clock.now = datetime.now(UTC)
    goal, steps = retained_plan(core)
    finish_record(core, steps[0], 1)
    candidate = setup_goal(core, number=10, topic="主人候选目标")

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await navigate(page, "日记与成长")
            card = page.locator(".goal").filter(has_text="主人候选目标")
            await card.get_by_role("button", name="接受", exact=True).click()
            await submit_dialog(page, "接受", f"/goals/{candidate}/decision")
            assert core.goals.get(candidate)["state"] == "QUEUED"
            await card.get_by_role("button", name="取消", exact=True).click()
            await submit_dialog(page, "取消", f"/goals/{candidate}/decision")
            assert core.goals.get(candidate)["state"] == "CANCELLED"

            plan = page.locator(".goal").filter(has_text="了解天文内容")
            assert "第 1 步：已完成" in await plan.inner_text()
            await plan.get_by_role("button", name="续行").click()
            await page.get_by_label("操作", exact=True).select_option("WAIT")
            until = (core.clock.now.astimezone(ZoneInfo(core.settings.timezone))
                     + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M")
            await page.get_by_label("等待到期时间（浏览器本地时间）").fill(until)
            await submit_dialog(page, "确认", f"/goals/{goal}/continuation")
            waited = core.goals.plan(goal)
            assert waited["version"] == 2 and waited["allowed_actions"] == ["WAIT", "ABANDON"]
            assert not core.store.rows("SELECT * FROM exploration_requests")

            core.clock.advance(181)
            await page.get_by_role("button", name="刷新当前页面").click()
            await page.locator("#page[aria-busy=false]").wait_for()
            await plan.get_by_role("button", name="续行").click()
            await page.get_by_label("操作", exact=True).select_option("CONTINUE")
            await page.get_by_role("button", name="上移第 3 步").click()
            await submit_dialog(page, "确认", f"/goals/{goal}/continuation")
            saved = core.goals.plan(goal)
            assert saved["version"] == 3
            assert [s["step_id"] for s in saved["steps"]] == [steps[0], steps[2], steps[1]]
            assert [s["state"] for s in saved["steps"]] == ["DONE", "QUEUED", "WAITING_DECISION"]

    run(scenario())


@pytest.mark.parametrize("label,decision", [
    ("作为假设接受", "ACCEPT_AS_HYPOTHESIS"), ("拒绝", "REJECT"),
    ("需要更多证据", "NEED_MORE_EVIDENCE"),
])
def test_console_reflection_decisions_use_native_contract(core, tmp_path, label, decision):
    core.clock.now = datetime.now(UTC)
    enable_nightly(core)
    assert run(core.reflections.run_day(DAY))["state"] == "COMMITTED"
    suggestion = core.reflections.recent()[0]

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await navigate(page, "日记与成长")
            card = page.locator(".reflection-card")
            assert await card.get_by_text(re.compile(r"证据（\d+ 条原话）")).count() == 1
            await card.get_by_role("button", name=label, exact=True).click()
            await submit_dialog(page, label, f"/reflections/{suggestion['suggestion_id']}/decision")
            assert core.reflections.recent()[0]["state"] == decision
            assert not core.store.rows("SELECT * FROM memories")
            assert not core.store.rows("SELECT * FROM outbox")

    run(scenario())


def test_console_probability_delegation_round_trips_fraction(core, tmp_path):
    core.clock.now = datetime.now(UTC)

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await navigate(page, "能力与开关")
            await page.get_by_role("link", name=re.compile("表情回应")).click()
            await page.get_by_role("radio", name=re.compile("她提议，我批准")).click()
            await page.get_by_role("checkbox", name="表情选择概率", exact=True).click()
            assert await page.get_by_role("textbox", name="表情选择概率").input_value() == "0.175"
            async with page.expect_response(lambda r: r.url.endswith("/autonomy/expression")
                and r.request.method == "PUT") as response:
                await page.get_by_role("button", name="保存授权").click()
            result = await response.value
            assert result.status == 200, await result.text()
            assert delegation.delegation(core.store, "expression")["ranges"] == {
                "expression_selection_probability": {"min": 0.175}}

    run(scenario())


def test_console_cognition_times_row_states_and_read_failures(core, tmp_path):
    core.clock.now = datetime.now(UTC)
    core.settings.wake_enabled = core.settings.goals_enabled = True
    core.wake.evaluate()  # A saved SKIP choice is distinct from its RECORDED state.
    core.clock.advance(1)
    enable_nightly(core)
    assert run(core.reflections.run_day(DAY))["state"] == "COMMITTED"
    core.clock.advance(1)
    setup_goal(core, number=10)
    core.clock.advance(1)
    newest = setup_goal(core, number=11)
    core.goals.accept(newest)

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await navigate(page, "思考与决策")
            stats = page.locator(".dk-stats > div")
            assert await stats.filter(has_text="本页今日记录").locator("b").inner_text() == "5"
            assert await stats.filter(has_text="跳过").locator("b").inner_text() == "1"
            assert "时间未记录" not in await page.locator("#page").inner_text()
            labels = await page.locator(".dk-list .tag").all_text_contents()
            assert labels[0] == "排队中"  # Newer goal sorts ahead of the older wake's ID.
            assert set(labels) == {"排队中", "待接受", "完成", "已结算", "跳过"}
            await page.locator(".dk-list button").nth(1).click()
            assert await page.locator(".dk-list .tag").all_text_contents() == labels

            async def unavailable(route):
                await route.fulfill(status=503, content_type="application/json",
                                    body='{"error":"SYNTHETIC_UNAVAILABLE"}')

            await page.route("**/v1/console/cognition?kind=goal&*", unavailable)
            await page.get_by_role("button", name="刷新当前页面").click()
            await page.get_by_text(re.compile("数据没有更新")).wait_for()
            assert await page.locator(".dk-list .tag").all_text_contents() == labels
            assert await page.get_by_text("同步失败", exact=True).count() == 1
            await page.unroute("**/v1/console/cognition?kind=goal&*")
            await navigate(page, "日记与成长")
            await page.route("**/v1/console/cognition?*", unavailable)
            await navigate(page, "思考与决策")
            assert await page.get_by_text("暂时读不到数据", exact=True).count() == 1
            assert await page.get_by_text("还没有认知记录", exact=True).count() == 0
            assert await page.get_by_text("同步失败", exact=True).count() == 1

    asyncio.run(scenario())


def test_console_cognition_locates_original_record_and_loads_older_page(core, tmp_path):
    core.clock.now = datetime.now(UTC)
    goals = []
    for number in range(1, 33):
        goals.append(setup_goal(core, number=number, topic=f"分页目标 {number:02}"))
        core.goals.cancel(goals[-1])
        core.clock.advance(1)

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await navigate(page, "思考与决策")
            records = page.locator(".dk-list > li > button")
            assert await records.count() == 30
            await page.get_by_label("记录类型", exact=True).select_option("goal")
            await page.get_by_label("记录编号", exact=True).fill(goals[0])
            async with page.expect_response(lambda r: "record_id=" + goals[0] in r.url):
                await page.get_by_role("button", name="查找", exact=True).click()
            await page.locator("#page[aria-busy=false]").wait_for()
            assert await page.locator(f'button[data-record-id="{goals[0]}"]').get_attribute(
                "class") == "sel"
            await page.get_by_label("记录编号", exact=True).fill(goals[2])
            await page.get_by_role("button", name="查找", exact=True).click()
            await page.locator("#page[aria-busy=false]").wait_for()
            await page.locator(f'button.sel[data-record-id="{goals[2]}"]').wait_for()
            await navigate(page, "日记与成长")
            await navigate(page, "思考与决策")
            async with page.expect_response(lambda r: "&before=" in r.url):
                await page.get_by_role("button", name="更早的目标", exact=True).click()
            await page.locator(f'button[data-record-id="{goals[0]}"]').wait_for()
            assert await records.count() == 32
            assert await page.get_by_role("button", name="更早的目标", exact=True).count() == 0

    run(scenario())


@pytest.mark.parametrize("refresh_during_read", [False, True])
def test_console_cognition_pagination_does_not_restore_erased_snapshot(
    core, tmp_path, refresh_during_read,
):
    core.clock.now = datetime.now(UTC)
    for number in range(1, 33):
        goal = setup_goal(core, number=number, topic=f"分页隐私 {number:02}")
        core.goals.cancel(goal)
        core.clock.advance(1)

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await navigate(page, "思考与决策")
            held, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def old_page(route):
                if refresh_during_read:
                    response = await route.fetch()
                    held.set()
                    await release.wait()
                    try:
                        await route.fulfill(response=response)
                    finally:
                        finished.set()
                else:
                    # The next native page is fresh but the already loaded page is erased.
                    with core.store.transaction():
                        core.ledger.expire_events(core.store, ["evt_test_32"], core.clock())
                    await route.continue_()

            await page.route("**/v1/console/cognition?kind=goal&limit=30&before=*", old_page)
            await page.get_by_role("button", name="更早的目标", exact=True).click()
            if refresh_during_read:
                await asyncio.wait_for(held.wait(), timeout=10)
                with core.store.transaction():
                    core.ledger.expire_events(core.store, ["evt_test_32"], core.clock())
                await page.get_by_role("button", name="刷新当前页面").click()
                await page.locator("#page[aria-busy=false]").wait_for()
                release.set()
                await asyncio.wait_for(finished.wait(), timeout=10)
            await page.locator(f'button[data-record-id="{goal}"]').wait_for(state="detached")
            # The native cursor scans 30 rows, then omits the one erased source.
            assert await page.locator(".dk-list > li > button").count() == 29
            assert await page.get_by_role("button", name="更早的目标", exact=True).is_enabled()

    run(scenario())
