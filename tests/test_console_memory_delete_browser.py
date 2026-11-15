"""Real TLS/browser controls distinguish an incorrect fact from source erasure."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import run
from tests.support.confirmed_memory import remember
from tests.support.console_browser import console_page, navigate, submit_dialog

pytestmark = pytest.mark.skipif(os.environ.get('COMPANION_TEST_BROWSER') != '1',
                               reason='Explicit isolated browser run')


@pytest.mark.parametrize(('mode', 'width'), [('list', 1440), ('graph', 390)])
def test_memory_only_delete_and_persistent_browser_login(core, tmp_path, mode, width):
    core.clock.now = datetime.now(UTC)
    wrong = remember(core, 1, '鲸鲸喜欢浅焙咖啡', confirmed=True)
    other = remember(core, 2, '鲸鲸喜欢科幻小说', confirmed=True)
    sources = core.store.rows('SELECT * FROM events ORDER BY event_id')
    artifacts = Path(__file__).resolve().parents[1] / 'output/playwright/login-memory'
    artifacts.mkdir(parents=True, exist_ok=True)

    async def scenario():
        async with console_page(core, tmp_path) as page:
            await navigate(page, '记忆')
            await page.get_by_role('button', name='全部', exact=True).click()
            await page.locator('.mem').filter(has_text='鲸鲸喜欢浅焙咖啡').wait_for()
            assert '关于鲸鲸' in await page.locator('#page').inner_text()
            await page.set_viewport_size({'width': width, 'height': 1000})
            if mode == 'graph':
                await page.get_by_role('button', name='关系图', exact=True).click()
                assert await page.locator('[data-gn="me"]').get_attribute('aria-label') == '鲸鲸'
                await page.locator(f'[data-gn="m:{wrong}"]').click()
                card = page.locator('.gmem').filter(has_text='鲸鲸喜欢浅焙咖啡')
            else:
                card = page.locator('.mem').filter(has_text='鲸鲸喜欢浅焙咖啡')
            label = '删除这条记忆（记忆有误）'
            await card.get_by_role('button', name=label, exact=True).click()
            dialog = page.get_by_role('dialog')
            assert '原始消息、日记和其他记忆都保留' in await dialog.inner_text()
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.screenshot(path=str(artifacts / f'delete-{width}.png'), full_page=True)
            await submit_dialog(page, label, f'/memories/{wrong}/decision')
            assert core.store.rows('SELECT * FROM events ORDER BY event_id') == sources
            assert core.store.one('SELECT status FROM memories WHERE memory_id=%s', (other,))[
                'status'] == 'CONFIRMED'
            cookies = await page.context.cookies()
            owner_cookie = next(c for c in cookies if c['name'] == 'companion_owner_session')
            assert owner_cookie['expires'] > datetime.now(UTC).timestamp() + 300 * 86400
            context = await page.context.browser.new_context(
                storage_state=await page.context.storage_state(), ignore_https_errors=True)
            reopened = await context.new_page()
            await reopened.goto(page.url)
            await reopened.get_by_role('button', name='退出登录', exact=True).wait_for()
            assert await reopened.locator('#login').is_hidden()
            assert core.store.one("SELECT count(*) AS n FROM audit_log WHERE "
                                  "action='console.login'")['n'] == 1
            await context.close()

    run(scenario())
