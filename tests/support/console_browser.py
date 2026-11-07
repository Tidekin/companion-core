import json
import os
from collections import deque
from contextlib import asynccontextmanager
from uuid import uuid4

from pydantic import SecretStr

from tests.console_proxy import ROOT, console_proxy

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
