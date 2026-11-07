"""Real profile API and independent native Core process enforce profile-only sources."""

import asyncio
import json
import os
import subprocess
import sys

import httpx
from pydantic import SecretStr

from companion.console_api import COOKIE
from tests.conftest import run
from tests.social_profile_support import confirmed_profile_material
from tests.support.core_http import real_core_http
from tests.support.owner_console import ORIGIN, PASSWORD
from tests.support.social_autonomy import background
from tests.support.social_owner_authority import authorize


def test_real_api_independent_profile_worker_excludes_public_and_rechecks_forget(core, tmp_path):
    module, ref, _ = background(core, nickname_enabled=True, qzone_enabled=False,
                                max_output_tokens=128)
    memory_id = confirmed_profile_material(core, confirm=False)
    core.settings.console.enabled = True
    core.settings.console.public_origin = ORIGIN
    core.settings.control_token = SecretStr(PASSWORD)
    _, authority = authorize(core, tmp_path)

    async def scenario():
        async with real_core_http(core) as port:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False,
                headers={
                    "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value(),
                },
            ) as api:
                session = await api.post("/v1/console/session", json={"password": PASSWORD},
                                         headers={"Origin": ORIGIN})
                assert session.status_code == 200, session.text
                # The loopback server stands behind the configured HTTPS Console origin.
                # Forward its Secure cookie explicitly, as a TLS terminator would.
                owner_headers = {"Origin": ORIGIN, "X-CSRF-Token": session.json()["csrf_token"],
                                 "Cookie": COOKIE + "=" + session.cookies[COOKIE],
                                 "Idempotency-Key": "real-console-profile-confirm"}
                confirmed = await api.post("/v1/console/memories/" + memory_id + "/decision",
                                           json={"decision": "confirm"}, headers=owner_headers)
                assert confirmed.status_code == 200, confirmed.text
                baseline = await api.post("/internal/v1/account-actions/profile-baselines", json={
                    "authority": authority,
                    "authorization_sha256": module.config.authorization_sha256,
                    "profiles": [{"bot_qq_id": core.settings.bot_qq_id, "field": "nickname",
                                  "value": "External baseline instruction",
                                  "observed_at": core.clock(),
                                  "receipt_sha256": "b" * 64}],
                })
                assert baseline.status_code == 200, baseline.text
                config, result = tmp_path / "profile-child.json", tmp_path / "profile-result.json"
                config.write_text(json.dumps({
                    "settings": core.settings.model_dump(mode="python"), "source_id": ref,
                    "now": core.clock(), "mode": "core_profile", "result_path": str(result),
                }, default=lambda value: value.get_secret_value()
                   if isinstance(value, SecretStr) else str(value)), encoding="utf-8")
                child = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-B", "-m", "tests.social_recovery_process", str(config)],
                    capture_output=True, timeout=30,
                )
                assert child.returncode == 0, child.stderr.decode(errors="replace")
                outcome = json.loads(result.read_text(encoding="utf-8"))
                assert outcome["pid"] != os.getpid()
                assert outcome["worked"] and outcome["idle"] and outcome["provider_calls"] == 1
                payload = json.loads(outcome["payloads"][0][-1]["content"])
                assert payload["purpose"] == "owner_confirmed_profile"
                assert "sources" not in payload and "current_profile" not in payload
                assert payload["owner_confirmed_material"] == [{
                    "subject": "user", "kind": "preference", "content": "我偏爱星空与简洁的表达。",
                }]
                assert "External baseline instruction" not in json.dumps(payload)
                assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
                leased = await api.post("/internal/v1/deliveries/lease", json={
                    "worker_id": "synthetic-profile", "limit": 1, "delivery_lane": "qq_account",
                    "authority": authority,
                    "account_actions_authorization_sha256": module.config.authorization_sha256,
                })
                assert leased.status_code == 200, leased.text
                item = leased.json()["items"][0]
                assert item["action_type"] == "qq.profile.nickname"
                forgotten = await api.post("/v1/console/memories/" + memory_id + "/decision",
                                           json={"decision": "forget"}, headers={
                                               **owner_headers,
                                               "Idempotency-Key": "real-console-forget",
                                           })
                assert forgotten.status_code == 200, forgotten.text
                denied = await api.post(
                    "/internal/v1/deliveries/" + item["message_id"] + "/begin-send",
                    json={"lease_token": item["lease_token"]},
                )
                assert denied.status_code >= 400, denied.text
                assert core.store.one("SELECT state,attempt_count FROM outbox") == {
                    "state": "CANCELLED", "attempt_count": 0}
                assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    run(scenario())
