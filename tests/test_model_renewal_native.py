"""Original offline CLI, authenticated HTTP activation and independent Life worker."""

import hashlib
import json

import httpx
import pytest

from companion import model_carry
from companion.store import encode
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.life_worker_integration import process_worker
from tests.support.model_carry_native import child, finished
from tests.support.model_renewal_native import life_case, source_and_renewal


def test_renewed_life_uses_native_cli_http_and_worker_without_replaying_unknown(
    core,
    tmp_path,
    monkeypatch,
):
    case = life_case(core, tmp_path, monkeypatch)
    original = core.store.one('SELECT * FROM model_usage WHERE request_id=%s', (case.request_id,))

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
                core.store.one('SELECT * FROM model_usage WHERE request_id=%s', (case.request_id,))
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
