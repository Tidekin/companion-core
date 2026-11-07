"""Closed current input uses the original CLI/HTTP/Bridge handoff and Life worker."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from companion import owner_authority
from companion.store import encode
from companion_onebot_bridge.capture import prepare_owner_input
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.life_worker_integration import process_worker
from tests.support.model_carry_native import child, finished
from tests.support.model_renewal_native import life_case, source_and_renewal
from tests.support.native_context import Capture
from tests.support.onebot import provider as onebot_provider
from tests.support.onebot_events import raw_event


@pytest.mark.parametrize("history_source", [False, True])
def test_closed_unknown_renewal_native_input_handoff_and_private_life(
    core, tmp_path, monkeypatch, history_source,
):
    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    case = life_case(core, tmp_path, monkeypatch)

    async def scenario():
        async with real_core_http(core) as port, private_http_client(
            f"http://127.0.0.1:{port}", core.settings.bridge_token.get_secret_value(),
        ) as http:
            config = BridgeConfig(
                owner_qq_id=core.settings.owner_qq_id, bot_qq_id=core.settings.bot_qq_id,
                bridge_token=core.settings.bridge_token.get_secret_value(), onebot_token="s" * 32,
                onebot_http_url="http://127.0.0.1:3000",
                onebot_ws_url="ws://127.0.0.1:3001",
                protocol_epoch=case.policy.capture.protocol_epoch,
                capture_signing_key=case.key.private_bytes_raw().hex(),
                owner_authority=case.successor["authority"],
                core_url=str(http.base_url).rstrip("/"),
                data_dir=str(tmp_path / "bridge"),
            )
            async def capture_input(number, text):
                with Spool(config) as spool:
                    runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    frame = encode(raw_event(
                        number, time=int(core.clock.now.timestamp()), text=text))
                    value = await prepare_owner_input(runtime, http, None, frame, core.clock.now,
                                                       ("127.0.0.1", 3001))
                    assert runtime.persist_capture(value["event"], frame,
                        received_at=core.clock.now, peer=("127.0.0.1", 3001))
                    assert await runtime.flush_one(http)
                    return value["event"]["event_id"]

            history = None
            if history_source:
                history = await capture_input(39, "导出Bug还没解决，明天继续排查")
                earlier = await child(tmp_path, "settled-history", core, mode="worker-text",
                                      output=str(tmp_path / "history-provider.json"))
                await finished(earlier)
                assert core.store.one("SELECT state FROM jobs WHERE event_id=?",
                                       (history,))["state"] == "SUCCEEDED"
                sends = []
                async with private_http_client(config.onebot_http_url, config.onebot_token,
                    transport=httpx.MockTransport(lambda req: onebot_provider(req, sends)),
                ) as protocol:
                    with Spool(config) as spool:
                        runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                        assert (await Capture(core, spool, runtime).send(http, protocol))["worked"]
                assert len(sends) == 1
                core.clock.now += timedelta(seconds=1)
            event_id = await capture_input(40, "synthetic current uncertain input")
            output = tmp_path / "current-provider.json"
            worker = await child(tmp_path, "current-timeout", core, mode="worker-timeout",
                                 output=str(output))
            await finished(worker)
            assert len(json.loads(output.read_text())) == 1
            current = core.store.one(
                "SELECT m.* FROM model_usage m JOIN jobs j USING(job_id) WHERE j.event_id=?",
                (event_id,),
            )
            assert current["state"] == "UNKNOWN"
            if history:
                assert core.store.one("SELECT 1 FROM job_sources WHERE job_id=? AND event_id=?",
                                       (current["job_id"], history))
            with core.store.transaction():
                view = owner_authority.custody(core.store, core.settings, settlement=True)
            assert view["model_controls"][view["model_closures"][current["request_id"]]][
                "kind"] == "model_local_close"
            historical = core.store.rows("SELECT * FROM model_usage ORDER BY request_id")
            assert len(historical) == 2 + int(history_source)
            assert sum(row["state"] == "UNKNOWN" for row in historical) == 2
            source, request, policy, witness = await source_and_renewal(
                core, tmp_path, case, http,
            )
            if history:
                source = history
                policy = policy.model_copy(update={"life_shadow": policy.life_shadow.model_copy(
                    update={"source_event_ids": [source]},
                )})
                request["successor_policy"] = policy.model_dump(mode="json")
            with Spool(config) as spool:
                OwnerRuntime(spool, clock=lambda: core.clock.now).stop()
            raw = encode(request).encode()
            path = tmp_path / "renewal.json"
            path.write_bytes(raw)
            controller = await child(
                tmp_path, "renewal-controller", core, mode="controller",
                command="carry-closed-model-attempts", executors=witness.value,
                request=str(path), request_sha256=hashlib.sha256(raw).hexdigest(),
            )
            summary = json.loads(await finished(controller))
            assert summary["kind"] == "owner_model_approval_renewal_v1"
            assert summary["retained_micro_usd"] == sum(
                r["billed_micro_usd"] for r in historical if r["state"] == "UNKNOWN")
            query = {"authority": config.owner_authority, "carry_sha256": summary["carry_sha256"],
                     "successor_policy_sha256": summary["successor_policy_sha256"]}
            response = await http.post("/internal/v1/owner-authority/model-carry-receipt",
                                       json=query)
            assert response.status_code == 200, response.text
            proof = response.json()
            assert proof["kind"] == "owner_model_unknown_carry_v1" and proof["version"] == 1
            assert proof["request_ids"] == [current["request_id"]]
            assert proof["retained_micro_usd"] == current["billed_micro_usd"]

            async def bridge(name, cfg, arguments):
                process = await child(tmp_path, name, core, mode="bridge", config=cfg,
                                      arguments=arguments, freeze_bridge_clock=True)
                return json.loads(await finished(process))

            prepared = await bridge("carry-prepare", config, [
                "owner-carry-handoff-prepare", "--carry-sha256", summary["carry_sha256"],
                "--successor-policy-sha256", summary["successor_policy_sha256"],
            ])
            assert prepared["state"] == "CARRY_HANDOFF_PREPARED"
            control = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
            body = {"expected_revision": 4, "policy": policy.model_dump(mode="json")}
            preview = await http.post("/internal/v1/owner-authority/preview", headers=control,
                                      json=body)
            assert preview.status_code == 200, preview.text
            activated = await http.post("/internal/v1/owner-authority/activate", headers=control,
                json={**body, "operation_id": "renew-current-input",
                      "preview_sha256": preview.json()["preview_sha256"]})
            assert activated.status_code == 200, activated.text
            newer = replace(config, owner_authority=activated.json()["authority"])
            assert (await bridge("handoff", newer, ["owner-handoff"]))["state"] == "ACTIVE"
            with Spool(newer) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                records = runtime.verify()
                carried = [r["data"] for r in records if r["kind"] == "OWNER_INPUT_CARRIED"]
                assert {r["event_id"] for r in carried} == (
                    {history, event_id} if history else {event_id})
                assert all(r["carry_receipt"] == proof for r in carried)
                assert all(not r["input_state"]["terminal"] for r in carried)
                assert spool.db.execute("SELECT state,body FROM inbox").fetchall() == (
                    [("accepted", None)] * len(carried))
                assert not await runtime.flush_one(http)
                old_capture = json.loads(spool.db.execute(
                    "SELECT capture_json FROM window_inputs WHERE event_id=?", (event_id,),
                ).fetchone()[0])
            denied = await http.post("/internal/v1/owner-authority/inputs/approve",
                                     json=old_capture)
            assert denied.status_code >= 400
            assert core.store.rows("SELECT * FROM model_usage ORDER BY request_id") == historical
            assert (await http.post("/internal/v1/owner-authority/model-carry-receipt",
                                    json=query)).json() == proof
            # A newer owner input still fences an older conversation topic, even
            # when its history is valid renewal scope. Do not weaken that guard.
            first = await process_worker(core, tmp_path, "bug", source,
                                         restart=history_source)
            if history_source:
                assert first["provider_calls"] == 0 and first["results"] == [], first
            else:
                assert first["provider_calls"] == 1, first
                assert first["results"][0]["state"] == "SHADOW", first
                again = await process_worker(core, tmp_path, "bug", source, restart=True)
                assert again["provider_calls"] == 0
            assert len(core.store.rows(
                "SELECT * FROM outbox WHERE action_type='qq.private.proactive'")) == int(
                    not history_source)
            assert all(event_id not in encode(item) for item in first["materials"])
            for row in historical:
                assert core.store.one("SELECT * FROM model_usage WHERE request_id=?",
                                       (row["request_id"],)) == row

    run(scenario())
