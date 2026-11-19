"""Two TLS origins, the shipped Nginx locations and isolated synthetic owner sessions."""

import json
import shutil
import ssl

import httpx

from tests.console_proxy import ROOT, nginx, port, ready
from tests.support.voice_node_api import tls_material


async def exercise_instance_overview(tmp_path):
    from playwright.async_api import async_playwright, expect

    edge_port, backend_port = port(), port()
    cert, key = tls_material(tmp_path)
    assets = tmp_path / "overview"
    shutil.copytree(ROOT / "deploy/console-instances", assets)
    hub = f"https://hub.instances.localhost:{edge_port}"
    peer = f"https://peer.instances.localhost:{edge_port}"
    for item in ("index.html", "card.nginx.conf", "hub.nginx.conf"):
        path = assets / item
        source = path.read_text("utf-8").replace("https://hub.example.test", hub)
        source = source.replace("https://peer.example.test", peer)
        source = source.replace("/srv/companion-instances/", assets.as_posix() + "/")
        path.write_text(source, encoding="utf-8")

    config = ("error_log stderr warn; events {} http { access_log off; "
              "server_names_hash_bucket_size 64;")
    for name, count in (("hub", 3), ("peer", 7)):
        domain = name + ".instances.localhost"
        cookie = f"session={name}; Path=/v1/console; Secure; HttpOnly; SameSite=Strict"
        snapshot = json.dumps({
            "runtime": {"state": "READY", "worker": {"state": "RECORDED"},
                        "modes": {"proactive": {"paused": False, "available": True}}},
            "counts": {"pending_jobs": count, "pending_deliveries": 0},
            "observed_at": "2026-01-01T08:00:00+00:00",
        })
        config += f"""
        server {{
            listen 127.0.0.1:{backend_port}; server_name {domain};
            location = /v1/console/overview {{
                if ($cookie_session != {name}) {{ return 401; }}
                default_type application/json; return 200 '{snapshot}';
            }}
        }}
        server {{
            listen 127.0.0.1:{edge_port} ssl; server_name {domain};
            ssl_certificate "{cert.as_posix()}"; ssl_certificate_key "{key.as_posix()}";
            include "{assets.as_posix()}/card.nginx.conf";
            {f'include "{assets.as_posix()}/hub.nginx.conf";' if name == 'hub' else ''}
            location ^~ /v1/console/ {{
                proxy_set_header Host $host; proxy_pass http://127.0.0.1:{backend_port};
            }}
            location = /console/ {{
                add_header Set-Cookie "{cookie}";
                default_type text/plain; return 200 "{name}";
            }}
        }}"""
    directory = tmp_path / "nginx"
    with nginx(config + "}", directory) as process:
        async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(cert)),
                                     trust_env=False) as client:
            await ready(client, f"https://127.0.0.1:{edge_port}/console/", process, directory)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=["--no-proxy-server"])
            context = await browser.new_context(ignore_https_errors=True,
                                                viewport={"width": 390, "height": 844})
            try:
                page = await context.new_page()
                anonymous = await page.goto(peer + "/instances/")
                assert anonymous.status == 401 and anonymous.url.startswith(hub + "/")
                assert peer not in (await anonymous.text())
                assert not await page.locator("iframe").count()
                await page.goto(hub + "/console/")
                await page.goto(hub + "/instances/")
                primary = page.frame_locator('iframe[title="主实例运行状态"]')
                secondary = page.frame_locator('iframe[title="Ximi 运行状态"]')
                await expect(primary.locator("#jobs")).to_have_text("3")
                await expect(secondary.get_by_role("status")).to_contain_text("登录")
                link = page.get_by_role("link", name="进入 Ximi 控制台", exact=False)
                async with page.expect_popup() as popup:
                    await link.click()
                login = await popup.value
                await login.wait_for_load_state()
                assert login.url == peer + "/console/"
                await login.close()
                await page.reload()
                await expect(primary.locator("#jobs")).to_have_text("3")
                await expect(secondary.locator("#jobs")).to_have_text("7")
                await expect(secondary.locator("#updated")).to_contain_text("更新于")
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                # Each owner cookie is host-only, and unavailable on the frame's static path.
                for origin, token in ((hub, "hub"), (peer, "peer")):
                    cookies = await context.cookies(origin + "/v1/console/overview")
                    assert [c["value"] for c in cookies] == [token]
                    assert await context.cookies(origin + "/instances/card.html") == []
                await page.route(peer + "/v1/console/overview", lambda route: route.fulfill(
                    status=200, content_type="application/json", body="{}"))
                await page.reload()
                await expect(secondary.get_by_role("status")).to_contain_text("暂时无法读取")
                await expect(secondary.locator("#metrics")).to_be_hidden()
                await expect(primary.locator("#jobs")).to_have_text("3")
                await page.unroute(peer + "/v1/console/overview")
                await context.clear_cookies(domain="peer.instances.localhost")
                await page.reload()
                await expect(secondary.get_by_role("status")).to_contain_text("登录")
                await expect(secondary.locator("#metrics")).to_be_hidden()
                await expect(primary.locator("#jobs")).to_have_text("3")
            finally:
                await context.close()
                await browser.close()
