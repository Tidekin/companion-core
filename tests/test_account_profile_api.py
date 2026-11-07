"""Trusted account observations and reviewed asset registration use distinct API authority."""

import json

import httpx
import pytest
from pydantic import SecretStr

from companion.social_autonomy_protocol import SocialAutonomyConfig
from companion.store import after
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.social_autonomy import avatar
from tests.support.social_owner_authority import authorize


def test_real_profile_batch_and_asset_api_preserve_scope_and_transaction(
    core, tmp_path,
):
    core.settings.control_token = SecretStr("account-test-control-credential-32-characters")
    policy = SocialAutonomyConfig(
        enabled=True,
        scope="qq_social_v1",
        authorization_sha256="a" * 64,
        nickname_enabled=True,
        signature_enabled=True,
        avatar_enabled=True,
        asset_policy_sha256="e" * 64,
        max_output_tokens=128,
    )
    core.settings.social_autonomy = policy
    _, authority = authorize(core, tmp_path)

    async def scenario():
        async with real_core_http(core) as port:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                trust_env=False,
                headers={
                    "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value(),
                },
            ) as client:
                profile = {
                    "bot_qq_id": core.settings.bot_qq_id,
                    "field": "nickname",
                    "value": "原昵称",
                    "observed_at": core.clock(),
                    "receipt_sha256": "b" * 64,
                }
                body = {
                    "authority": authority,
                    "authorization_sha256": policy.authorization_sha256,
                    "profiles": [profile],
                }
                path = "/internal/v1/account-actions/profile-baselines"
                query = {"authority": json.dumps(authority),
                         "authorization_sha256": policy.authorization_sha256}
                state = await client.get(path + "/state", params=query)
                assert state.status_code == 200, state.text
                assert state.json() == {
                    "authority": authority, "authorization_sha256": policy.authorization_sha256,
                    "fields": ["nickname", "signature", "avatar"],
                    "baseline_seconds": policy.profile_baseline_seconds,
                }
                forbidden_state = await client.get(path + "/state", params=query,
                    headers={"Authorization": "Bearer not-the-bridge"})
                assert forbidden_state.status_code == 401
                wrong_state = await client.get(path + "/state",
                    params={**query, "authorization_sha256": "0" * 64})
                assert wrong_state.status_code == 403
                malformed_state = await client.get(path + "/state",
                    params={**query, "authority": "bad"})
                assert malformed_state.status_code == 400
                wrong = await client.post(path, json={**body,
                    "authority": {**authority, "revision": authority["revision"] + 1}})
                assert wrong.status_code == 409
                assert core.store.rows("SELECT * FROM social_autonomy_profiles") == []
                wrong_identity = {**profile, "bot_qq_id": "99999", "field": "signature"}
                rejected = await client.post(
                    path, json={**body, "profiles": [profile, wrong_identity]}
                )
                assert rejected.status_code == 403
                assert core.store.rows("SELECT * FROM social_autonomy_profiles") == []
                duplicate = await client.post(path, json={**body, "profiles": [profile, profile]})
                assert duplicate.status_code == 400
                accepted = await client.post(path, json=body)
                assert accepted.status_code == 200, accepted.text
                assert accepted.json() == {
                    "accepted": True,
                    "profiles": [
                        {
                            "field": "nickname",
                            "observed_at": core.clock(),
                        }
                    ],
                }
                replay = await client.post(path, json=body)
                assert replay.json() == accepted.json()
                stale = await client.post(
                    path,
                    json={
                        **body,
                        "profiles": [
                            {
                                **profile,
                                "observed_at": after(
                                    core.clock(), -policy.profile_baseline_seconds - 1
                                ),
                            }
                        ],
                    },
                )
                assert stale.status_code == 403
                asset = avatar(core).model_dump(mode="json")
                forbidden = await client.post("/internal/v2/account-assets", json=asset)
                assert forbidden.status_code == 401
                registration = await client.post(
                    "/internal/v2/account-assets",
                    json=asset,
                    headers={
                        "Authorization": "Bearer " + core.settings.control_token.get_secret_value()
                    },
                )
                assert registration.status_code == 200, registration.text
                assert registration.json()["asset_id"] == asset["asset_id"]
                assert core.store.one("SELECT COUNT(*) n FROM social_autonomy_assets")["n"] == 1
                assert core.store.rows("SELECT * FROM model_usage") == []
                assert core.store.rows("SELECT * FROM outbox") == []

    run(scenario())


@pytest.mark.parametrize("fields", [[], ["signature"]])
def test_profile_probe_discovery_never_grants_disabled_fields(core, tmp_path, fields):
    policy = SocialAutonomyConfig(enabled=True, scope="qq_social_v1",
        authorization_sha256="a" * 64, qzone_enabled=True,
        signature_enabled="signature" in fields, max_output_tokens=128)
    core.settings.social_autonomy = policy
    _, authority = authorize(core, tmp_path)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as client:
            response = await client.get("/internal/v1/account-actions/profile-baselines/state",
                params={"authority": json.dumps(authority),
                        "authorization_sha256": policy.authorization_sha256})
            assert response.status_code == 200, response.text
            assert response.json()["fields"] == fields
            for table in ("social_autonomy_profiles", "model_usage", "outbox"):
                assert core.store.rows(f"SELECT * FROM {table}") == []

    run(scenario())
