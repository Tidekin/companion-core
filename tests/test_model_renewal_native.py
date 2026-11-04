"""Original offline CLI, authenticated HTTP activation and independent Life worker."""

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from companion import model_carry
from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.store import after, encode
from companion_onebot_bridge.config import BridgeConfig
from tests import owner_model_fixtures
from tests.conftest import run
from tests.model_carry_support import migrated
from tests.support.core_http import real_core_http
from tests.test_life_worker_integration import process_worker
from tests.test_model_carry_native import child, finished
from tests.test_model_renewal import renewal
from tests.test_reading_followup import admit


def life_case(core, tmp_path, monkeypatch):
    def approved_policy(**values):
        return OwnerGrantPolicy(
            **{
                **values,
                "purposes": ["chat", "proactive", "history"],
                "history": {
                    "scope": "owner_private",
                    "not_before": after(core.clock(), -3600),
                    "max_turns": 16,
                    "max_utf8_bytes": 64000,
                },
            }
        )

    with monkeypatch.context() as patch:
        patch.setattr(owner_model_fixtures, "OwnerGrantPolicy", approved_policy)
        case = migrated(core, tmp_path, expires_at=core.clock.now + timedelta(hours=2),
                        rates_valid_until=core.clock.now + timedelta(days=2))
    core.settings.control_token = SecretStr("synthetic-renewal-control")
    core.settings.life_loop = LifeLoopConfig(mode="SHADOW", follow_up_delay_seconds=60)
    core.settings.wake_enabled = True
    core.settings.proactive_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    core.clock.now = core.settings.paid_model.expires_at - timedelta(hours=2)
    return case


async def source_and_renewal(core, tmp_path, case, http):
    config = BridgeConfig(
        owner_qq_id=core.settings.owner_qq_id,
        bot_qq_id=core.settings.bot_qq_id,
        bridge_token=core.settings.bridge_token.get_secret_value(),
        onebot_token="s" * 32,
        protocol_epoch=case.policy.capture.protocol_epoch,
    )
    plan = SimpleNamespace(authority=case.successor["authority"], key=case.key, policy=case.policy)
    incoming, _ = await admit(http, core, config, plan, 2, "导出Bug还没解决，明天接着看")
    source_id = incoming["event_id"]
    request, policy, witness = renewal(core, case)
    scope = LifeShadowApproval(
        scope="owner_private_life_shadow_v1",
        authorization_sha256=policy.authorization_sha256,
        conversation_id=core.store.one(
            "SELECT conversation_id FROM events WHERE event_id=?", (source_id,)
        )["conversation_id"],
        source_event_ids=[source_id],
        purposes=["PRIVATE_MESSAGE_DRAFT"],
        automatic_actions=["qq.private.proactive"],
        expires_at=policy.expires_at,
        max_items=1,
    )
    policy = OwnerGrantPolicy.model_validate(
        {**policy.model_dump(), "purposes": [*policy.purposes, "life"], "life_shadow": scope}
    )
    request["successor_policy"] = policy.model_dump(mode="json")
    return source_id, request, policy, witness


def test_renewed_life_uses_native_cli_http_and_worker_without_replaying_unknown(
    core,
    tmp_path,
    monkeypatch,
):
    case = life_case(core, tmp_path, monkeypatch)
    original = core.store.one("SELECT * FROM model_usage WHERE request_id=?", (case.request_id,))

    async def scenario():
        async with (
            real_core_http(core) as port,
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                trust_env=False,
                headers={
                    "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()
                },
            ) as http,
        ):
            source, request, policy, witness = await source_and_renewal(core, tmp_path, case, http)
            path = tmp_path / "native-renewal.json"
            raw = encode(request).encode()
            path.write_bytes(raw)
            controller = await child(
                tmp_path,
                "renewal-controller",
                core,
                mode="controller",
                command="carry-closed-model-attempts",
                executors=witness.value,
                request=str(path),
                request_sha256=hashlib.sha256(raw).hexdigest(),
            )
            receipt = json.loads(await finished(controller))
            assert receipt["kind"] == "owner_model_approval_renewal_v1"
            query = {"expected_revision": 4, "policy": policy.model_dump(mode="json")}
            control = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
            preview = await http.post(
                "/internal/v1/owner-authority/preview", headers=control, json=query
            )
            assert preview.status_code == 200, preview.text
            active = await http.post(
                "/internal/v1/owner-authority/activate",
                headers=control,
                json={
                    **query,
                    "operation_id": "native-life-renewal",
                    "preview_sha256": preview.json()["preview_sha256"],
                },
            )
            assert active.status_code == 200, active.text
            first = await process_worker(core, tmp_path, "bug", source)
            assert first["provider_calls"] == 1
            assert first["results"][0]["state"] == "SHADOW", first
            second = await process_worker(core, tmp_path, "bug", source, restart=True)
            assert second["provider_calls"] == 0
            messages = core.store.rows(
                "SELECT * FROM outbox WHERE action_type='qq.private.proactive'"
            )
            assert len(messages) == 1 and messages[0]["state"] == "PENDING", messages
            assert (
                core.store.one("SELECT * FROM model_usage WHERE request_id=?", (case.request_id,))
                == original
            )
            assert all(case.capture.event_id not in encode(m) for m in first["materials"])

    run(scenario())


@pytest.mark.parametrize("change", ["public", "old_unknown", "conversation", "new_purpose"])
def test_life_renewal_cannot_expand_sources_or_actions(core, tmp_path, monkeypatch, change):
    case = life_case(core, tmp_path, monkeypatch)

    async def scenario():
        async with (
            real_core_http(core) as port,
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                trust_env=False,
                headers={
                    "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()
                },
            ) as http,
        ):
            source, request, policy, witness = await source_and_renewal(core, tmp_path, case, http)
            scope = request["successor_policy"]["life_shadow"]
            if change == "public":
                scope["purposes"] = ["PRIVATE_DIARY"]
                scope["automatic_actions"] = ["diary.save"]
            elif change == "old_unknown":
                scope["source_event_ids"] = [case.capture.event_id]
            elif change == "conversation":
                scope["conversation_id"] = "unrelated-conversation"
            else:
                request["successor_policy"]["purposes"].append("goals")
            from companion.contracts import CoreError

            with pytest.raises(CoreError):
                model_carry.prepare(core.store, core.settings, request, witness, core.clock())

    run(scenario())
