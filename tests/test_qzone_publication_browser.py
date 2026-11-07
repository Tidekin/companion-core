"""Owner review and confirmation through the shipped Console and native HTTPS edge."""

import os
from datetime import UTC, datetime

import pytest

from tests.conftest import run
from tests.console_proxy import ROOT
from tests.support.social_owner_authority import close_provider_clients  # noqa: F401
from tests.test_console_browser import console_page, navigate
from tests.test_qzone_publication import setup

pytestmark = pytest.mark.skipif(os.environ.get("COMPANION_TEST_BROWSER") != "1",
                                reason="Explicit isolated browser run")


def test_console_public_draft_edit_review_approval_and_revocation(core, tmp_path):
    from playwright.async_api import expect

    core.clock.now = datetime.now(UTC)
    setup(core, tmp_path)
    evidence = ROOT / "output/playwright/qzone-publication"
    evidence.mkdir(parents=True, exist_ok=True)

    async def submitted(page, button, method):
        async with page.expect_response(lambda r: "/qzone/publications" in r.url
            and r.request.method == method) as response:
            await page.get_by_role("dialog").get_by_role("button", name=button, exact=True).click()
        result = await response.value
        assert result.status in {200, 201}, await result.text()
        await page.get_by_role("dialog").wait_for(state="hidden")
        await page.locator("#page[aria-busy=false]").wait_for()

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await navigate(page, "QQ 资料行为")
            await page.get_by_role("button", name="准备公开发布", exact=True).click()
            await submitted(page, "保存待审稿", "POST")
            assert not core.store.rows("SELECT * FROM outbox")
            await page.get_by_role("button", name="审阅公开稿", exact=True).click()
            dialog = page.get_by_role("dialog")
            confirm = dialog.get_by_role("checkbox")
            await expect(confirm).not_to_be_checked()
            await expect(dialog.get_by_role("button", name="批准公开发布",
                                           exact=True)).to_be_disabled()
            await confirm.check()
            await dialog.get_by_role("textbox", name="完整正文", exact=True).fill(
                "主人审阅修改的公开稿。")
            await expect(confirm).not_to_be_checked()
            await submitted(page, "保存修改，重新审阅", "PUT")
            assert not core.store.rows("SELECT * FROM outbox")
            await page.get_by_role("button", name="审阅公开稿", exact=True).click()
            dialog = page.get_by_role("dialog")
            await expect(dialog.get_by_role("textbox", name="完整正文", exact=True)).to_have_value(
                "主人审阅修改的公开稿。")
            await expect(dialog.get_by_role("checkbox")).not_to_be_checked()
            await dialog.screenshot(path=str(evidence / "desktop.png"))
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert await dialog.evaluate("d => d.scrollWidth <= d.clientWidth")
            await dialog.screenshot(path=str(evidence / "mobile.png"))
            await dialog.get_by_role("checkbox").check()
            await submitted(page, "批准公开发布", "POST")
            assert core.store.one("SELECT state FROM outbox")["state"] == "PENDING"
            await page.get_by_role("button", name="撤销批准", exact=True).click()
            await submitted(page, "撤销批准", "POST")
            assert core.store.one("SELECT state FROM outbox")["state"] == "CANCELLED"

    run(scenario())
