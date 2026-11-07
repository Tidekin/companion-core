"""Explicit Life history consent uses the native renewal and independent worker."""

import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from companion import model_carry
from companion.contracts import CoreError
from companion.life_topics import _source, authority
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.store import encode
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.life_worker_integration import process_worker
from tests.support.model_carry_native import child, finished
from tests.support.model_renewal import renewal
from tests.support.model_renewal_native import life_case, source_and_renewal
from tests.support.owner_authority import activate


async def selected_then_history(core, tmp_path, case, http):
    source, request, policy, witness = await source_and_renewal(core, tmp_path, case, http)
    model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    active, _ = activate(core, policy, revision=4, operation="first-selected-life")
    case.successor, case.policy = active, policy
    request, policy, witness = renewal(core, case, number=2, offset=-timedelta(hours=12))
    policy = OwnerGrantPolicy.model_validate({**policy.model_dump(), "life_shadow": {
        **policy.life_shadow.model_dump(), "source_scope": "owner_private_history",
        "source_event_ids": [], "authorization_sha256": policy.authorization_sha256,
        "expires_at": policy.expires_at}})
    request["successor_policy"] = policy.model_dump(mode="json")
    return source, request, policy, witness


def test_selected_to_history_renewal_cli_http_and_independent_life_worker(
    core, tmp_path, monkeypatch,
):
    case = life_case(core, tmp_path, monkeypatch)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as http:
            source, request, policy, witness = await selected_then_history(
                core, tmp_path, case, http)
            before = core.store.rows("SELECT * FROM model_usage ORDER BY request_id")
            path = tmp_path / "history-renewal.json"
            raw = encode(request).encode()
            path.write_bytes(raw)
            controller = await child(tmp_path, "history-renewal-controller", core,
                mode="controller", command="carry-closed-model-attempts",
                executors=witness.value, request=str(path),
                request_sha256=hashlib.sha256(raw).hexdigest())
            receipt = json.loads(await finished(controller))
            assert receipt["kind"] == "owner_model_approval_renewal_v1"
            query = {"expected_revision": 6, "policy": policy.model_dump(mode="json")}
            control = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
            preview = await http.post("/internal/v1/owner-authority/preview", headers=control,
                                      json=query)
            assert preview.status_code == 200, preview.text
            active = await http.post("/internal/v1/owner-authority/activate", headers=control,
                json={**query, "operation_id": "activate-life-history",
                      "preview_sha256": preview.json()["preview_sha256"]})
            assert active.status_code == 200, active.text
            assert core.store.rows("SELECT * FROM model_usage ORDER BY request_id") == before
            value = _source(core.store, core.settings, source, core.clock())
            authority(core.store, core.settings, value["conversation_id"], [value], core.clock())
            core.life_topics.synchronize()
            topics = core.store.rows("SELECT topic_id,state FROM life_topics")
            assert topics, "Native history source must produce a bounded Life topic"
            core.life_topics.material(topics[-1]["topic_id"])
            first = await process_worker(core, tmp_path, "bug", source)
            assert first["provider_calls"] == 1 and first["results"][0]["state"] == "SHADOW"
            second = await process_worker(core, tmp_path, "bug", source, restart=True)
            assert second["provider_calls"] == 0
            assert all(core.store.one("SELECT * FROM model_usage WHERE request_id=?",
                                     (row["request_id"],)) == row for row in before)
    run(scenario())


@pytest.mark.parametrize("change", ["conversation", "items", "purpose", "history", "selected"])
def test_history_renewal_preserves_other_source_action_and_context_bounds(
    core, tmp_path, monkeypatch, change,
):
    case = life_case(core, tmp_path, monkeypatch)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as http:
            _, request, _, witness = await selected_then_history(core, tmp_path, case, http)
            scope = request["successor_policy"]["life_shadow"]
            if change == "conversation":
                scope["conversation_id"] = "unrelated-private-conversation"
            elif change == "items":
                scope["max_items"] += 1
            elif change == "purpose":
                scope["purposes"].append("PRIVATE_DIARY")
            elif change == "history":
                request["successor_policy"]["history"]["max_turns"] += 1
            else:
                scope["source_scope"] = "selected"
                scope["source_event_ids"] = [case.capture.event_id]
            journal = Path(core.settings.paid_model.journal_path)
            before = await asyncio.to_thread(journal.read_bytes)
            with pytest.raises(CoreError):
                model_carry.prepare(core.store, core.settings, request, witness, core.clock())
            assert await asyncio.to_thread(journal.read_bytes) == before
    run(scenario())
