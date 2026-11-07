"""Actual shipped Nginx gate, TLS, resource isolation and owner-session contracts."""

import os

import httpx
import pytest
from pydantic import SecretStr

from companion.console_api import COOKIE
from tests.conftest import run
from tests.console_proxy import STATIC, console_proxy
from tests.support.console_browser import PASSWORD

pytestmark = pytest.mark.skipif(
    os.environ.get("COMPANION_TEST_BROWSER") != "1"
    and os.environ.get("COMPANION_TEST_CONSOLE_PROXY") != "1",
    reason="Explicit native Nginx run (missing Nginx is an error when enabled)",
)
KINDS = {"index.html": "text/html", "app.js": "text/javascript",
         "app.css": "text/css", "icon.svg": "image/svg+xml"}


def configure(core):
    core.settings.console.enabled = True
    core.settings.console.public_origin = "https://console.example.test"
    core.settings.control_token = SecretStr(PASSWORD)


def assert_headers(response):
    for key, value in {"cache-control": "no-store", "x-content-type-options": "nosniff",
                       "referrer-policy": "no-referrer", "x-frame-options": "DENY"}.items():
        assert response.headers.get_list(key) == [value]
    csp = response.headers.get_list("content-security-policy")
    assert len(csp) == 1 and "script-src 'self'" in csp[0]
    assert "x-accel-redirect" not in response.headers
    return csp[0]


def test_shipped_proxy_serves_only_gated_assets_with_exact_headers(core, tmp_path):
    configure(core)

    async def scenario():
        async with console_proxy(core, tmp_path) as proxy, httpx.AsyncClient(
            base_url=proxy.url, verify=proxy.verify, trust_env=False,
        ) as http:
            # The actual synthetic CA and IP SAN are verified by this TLS client.
            for asset, kind in KINDS.items():
                response = await http.get("/console/" + ("" if asset == "index.html" else asset))
                assert response.status_code == 200
                assert response.headers["content-type"].startswith(kind)
                assert response.content == (STATIC / asset).read_bytes()
                static_csp = assert_headers(response)
            for path in ("/console/unknown", "/console/index.html", "/_console_assets/app.js"):
                response = await http.get(path)
                assert response.status_code == 404
                assert_headers(response)
            response = await http.get("/v1/console/session")
            assert response.status_code == 401
            assert assert_headers(response) == static_csp
            assert (await http.get("/internal/v1/health/live")).status_code == 404
    run(scenario())


def test_proxy_preserves_native_disabled_and_missing_credential_refusals(core, tmp_path):
    configure(core)

    async def scenario():
        async with console_proxy(core, tmp_path) as proxy, httpx.AsyncClient(
            base_url=proxy.url, verify=proxy.verify, trust_env=False,
        ) as http:
            for enabled, token, status, error in (
                (False, PASSWORD, 404, "CONSOLE_DISABLED"),
                (True, "", 503, "CONSOLE_OWNER_CREDENTIAL_REQUIRED"),
            ):
                core.settings.console.enabled = enabled
                core.settings.control_token = SecretStr(token)
                for path in ("/console/", "/console/app.js", "/console/unknown"):
                    response = await http.get(path)
                    assert response.status_code == status
                    assert response.json() == {"error": error}
                    assert_headers(response)
    run(scenario())


def test_proxy_never_sends_browser_credentials_or_body_to_static_backend(core, tmp_path):
    configure(core)

    async def scenario():
        async with console_proxy(core, tmp_path, native_backend=True) as proxy, httpx.AsyncClient(
            base_url=proxy.url, verify=proxy.verify, trust_env=False,
        ) as http:
            response = await http.request("GET", "/console/app.js",
                headers={"Cookie": "private-session=synthetic", "Authorization": "Bearer private"},
                content=b"SYNTHETIC_PRIVATE_BODY")
            assert response.status_code == 200
            assert set(proxy.backend_log.read_text().splitlines()) == {"-|-|-"}
    run(scenario())


def test_owner_cookie_origin_csrf_and_revocation_through_nginx(core, tmp_path):
    configure(core)

    async def scenario():
        async with console_proxy(core, tmp_path) as proxy, httpx.AsyncClient(
            base_url=proxy.url, verify=proxy.verify, trust_env=False,
        ) as http:
            login = await http.post("/v1/console/session", json={"password": PASSWORD},
                                    headers={"Origin": proxy.url})
            assert login.status_code == 200
            cookie = login.headers["set-cookie"]
            assert all(item in cookie for item in
                       (COOKIE + "=", "HttpOnly", "Secure", "SameSite=strict", "Path=/v1/console"))
            assert (await http.get("/v1/console/overview")).status_code == 200
            body = {"target": "proactive", "enabled": False}
            headers = {"Origin": proxy.url, "X-CSRF-Token": login.json()["csrf_token"],
                       "Idempotency-Key": "console-proxy-mode"}
            path = "/v1/console/mode"
            assert (await http.post(path, json=body, headers=headers | {
                "Origin": "https://foreign.invalid"})).status_code == 403
            assert (await http.post(path, json=body, headers=headers | {
                "X-CSRF-Token": "wrong"})).status_code == 403
            changed = await http.post(path, json=body, headers=headers)
            assert changed.status_code == 200
            assert (await http.post(path, json=body, headers=headers)).json() == changed.json()
            token = http.cookies.get(COOKIE)
            assert (await http.delete("/v1/console/session", headers=headers)).status_code == 200
            http.cookies.set(COOKIE, token, domain="127.0.0.1", path="/v1/console")
            assert (await http.get("/v1/console/overview")).status_code == 401
    run(scenario())


def test_static_backend_failure_is_closed_without_packaged_fallback(core, tmp_path):
    configure(core)

    async def scenario():
        async with console_proxy(core, tmp_path, native_backend=True) as proxy, httpx.AsyncClient(
            base_url=proxy.url, verify=proxy.verify, trust_env=False,
        ) as http:
            proxy.backend.terminate()
            proxy.backend.wait(timeout=5)
            response = await http.get("/console/")
            assert response.status_code == 502
            assert_headers(response)
            assert (await http.get("/v1/console/session")).status_code == 401
    run(scenario())
