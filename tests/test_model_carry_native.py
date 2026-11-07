"""STOP, real process exits, original journal carry, Bridge handoff and new-input ACK."""

import asyncio
import hashlib
import json
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr

from companion import owner_authority, owner_input_state, paid_budget
from companion.owner_authority_protocol import OwnerCapture
from companion.store import encode
from companion_delivery_protocol import digest
from companion_onebot_bridge.capture import prepare_owner_input
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_memes import MemePump
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.model_carry_support import executors
from tests.support.model_carry_native import child, finished
from tests.support.onebot import provider
from tests.support.onebot_events import raw_event
from tests.support.prepare_owner_input import window
from tests.support.video_understanding import video_policy
from tests.test_native_video import animation


async def marker(process, expected):
    lines = []
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), 30)
        assert line, b"".join(lines).decode(errors="replace")
        lines.append(line)
        if expected.encode() in line:
            return line


async def stopped(process):
    if process.returncode is None:
        process.terminate()
    await asyncio.wait_for(process.wait(), 10)
    assert process.returncode is not None


@pytest.mark.parametrize(
    "meme_enabled", [False, True], ids=["default_scope", "meme_expression_enabled"]
)
@pytest.mark.parametrize("current_timeout", [False, True])
@pytest.mark.parametrize("evaluation_revision", [False, True])
def test_native_video_unknown_carry_recovers_only_new_input_and_real_ack(
    core,
    tmp_path,
    monkeypatch,
    current_timeout,
    meme_enabled,
    evaluation_revision,
):
    import tests.support.prepare_owner_input as preparation

    original_prepare = preparation.prepare

    def one_slot(*args, **kwargs):
        policy = original_prepare(*args, **kwargs)
        args[0].settings.paid_model = args[0].settings.paid_model.model_copy(
            update={"concurrency": 1}
        )
        return policy

    monkeypatch.setattr(preparation, "prepare", one_slot)
    core.settings.control_token = SecretStr("synthetic-native-carry-control")

    async def scenario():
        async def semantic_offer(runtime, client, event_id):
            if not meme_enabled:
                return
            assert runtime.config.meme_enabled and runtime.config.meme_semantics_enabled
            pump = MemePump(runtime, client)
            try:
                pump.admitted(event_id)
                assert pump.query_task is not None
                await pump.query_task
            finally:
                await pump.close()
            offer = json.loads(
                core.store.one(
                    "SELECT value FROM runtime_meta WHERE key=?", ("meme_offer:" + event_id,)
                )["value"]
            )
            assert offer["event_id"] == event_id and offer["offer"] is None

        # Initial capture/upload is real authenticated HTTP over original synthetic pixels.
        # Honest frozen rates yield an 84 microUSD video reservation, matching
        # the recovery case without editing any usage or reservation projection.
        video = video_policy(input_rate=1, cached_input_rate=1, output_rate=650000)
        async with window(
            core,
            tmp_path,
            media=True,
            content=True,
            video=True,
            video_config=video,
            meme=meme_enabled,
        ) as (
            client,
            config,
            plan,
        ):
            event = await animation(core, client, config, plan, tmp_path, "image/gif")
        config = replace(
            config,
            owner_authority=plan.authority,
            capture_signing_key=plan.key.private_bytes_raw().hex(),
            rich_input_scope="references_v1",
            data_dir=str(tmp_path / "native-bridge"),
        )
        old_paid = core.settings.paid_model
        old_policy = owner_authority.evidence(core.store, core.settings, plan.authority).policy
        if meme_enabled:
            assert core.settings.meme_semantics_enabled and core.settings.expression_enabled
            assert {"meme", "expression"} <= set(old_policy.purposes)
            assert (
                old_policy.capability_policies["meme"]
                == owner_authority.capability_hashes(core.settings)["meme"]
            )
        api = await child(tmp_path, "old-api", core, mode="api", port=0)
        workers = []
        try:
            port = int((await marker(api, "API_READY ")).split()[1])
            assert 0 < port < 65536
            config = replace(config, core_url=f"http://127.0.0.1:{port}")
            worker = await child(
                tmp_path,
                "old-worker",
                core,
                mode="worker-video",
                output=str(tmp_path / "old-provider.json"),
            )
            workers.append(worker)
            await marker(worker, "WORK_COMPLETED")
            old_wire = json.loads((tmp_path / "old-provider.json").read_text("utf-8"))
            assert len(old_wire) == 2
            assert len(core.store.rows("SELECT * FROM model_usage WHERE state='UNKNOWN'")) == 1
            original_tool = core.store.one("SELECT * FROM agent_tool_calls")
            assert original_tool["state"] == "STARTED"
            async with private_http_client(config.core_url, config.bridge_token) as client:
                response = await client.post(
                    "/internal/v1/owner-authority/stop",
                    json={"authority": plan.authority, "operation_id": "native-carry-stop"},
                    headers={
                        "Authorization": "Bearer " + core.settings.control_token.get_secret_value()
                    },
                )
                assert response.status_code == 200, response.text
            with Spool(config) as spool:
                OwnerRuntime(spool, clock=lambda: core.clock.now).stop()
            # Genuine old API and native worker exits precede the offline controller.
            await stopped(worker)
            await stopped(api)
            assert worker.returncode is not None and api.returncode is not None
            original_usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
            capture = OwnerCapture.model_validate(
                json.loads(
                    core.store.one(
                        "SELECT value FROM runtime_meta WHERE key=?",
                        ("owner_input:" + event["event_id"],),
                    )["value"]
                )["capture"]
            )
            original_state = owner_input_state.read(core.store, core.settings, capture)
            assert original_state["terminal"] is False
            assert original_state["unsettled_model"] is True
            assert original_state["unsettled_work"] is False
            core.settings.paid_model = old_paid.model_copy(
                update={
                    "window_id": "native-recovered-window",
                    "approval_sha256": "e" * 64,
                    "concurrency_basis": "local_active_attempts_v2",
                }
            )
            items = executors(core.clock())
            request = {
                "version": 1,
                "operation_id": "native-carry-offline",
                "authorization_sha256": "f" * 64,
                "expected_head_sha256": owner_authority.custody(
                    core.store, core.settings, settlement=True
                )["head_sha256"],
                "authority": plan.authority,
                "successor_window_id": "native-recovered-window",
                "manifest_sha256": "0" * 64,
                "successor_policy": {},
                "executors": items,
            }
            path = tmp_path / "carry-request.json"

            async def controller(command):
                raw = encode(request).encode()
                path.write_bytes(raw)
                process = await child(
                    tmp_path,
                    command,
                    core,
                    mode="controller",
                    command=command,
                    executors=items,
                    request=str(path),
                    request_sha256=hashlib.sha256(raw).hexdigest(),
                )
                return json.loads(await finished(process))

            preview = await controller("preview-model-carry")
            request["manifest_sha256"] = preview["manifest_sha256"]
            policy = old_policy.model_copy(
                update={
                    "authorization_sha256": "f" * 64,
                    "unknown_carry_sha256": request["manifest_sha256"],
                    "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings)),
                }
            )
            request["successor_policy"] = policy.model_dump(mode="json")
            receipt = await controller("carry-closed-model-attempts")
            assert receipt["request_ids"] == [
                item["request_id"] for item in original_usage if item["state"] == "UNKNOWN"
            ]
            assert receipt["retained_micro_usd"] == sum(
                item["billed_micro_usd"] for item in original_usage if item["state"] == "UNKNOWN"
            )
            assert receipt["retained_micro_usd"] == 84
            api = await child(tmp_path, "new-api", core, mode="api", port=0)
            port = int((await marker(api, "API_READY ")).split()[1])
            assert 0 < port < 65536
            config = replace(config, core_url=f"http://127.0.0.1:{port}")

            async def bridge(arguments, bridge_config):
                process = await child(
                    tmp_path,
                    "bridge-" + arguments[0],
                    core,
                    mode="bridge",
                    config=bridge_config,
                    arguments=arguments,
                )
                return json.loads(await finished(process))

            prepared = await bridge(
                [
                    "owner-carry-handoff-prepare",
                    "--carry-sha256",
                    receipt["carry_sha256"],
                    "--successor-policy-sha256",
                    digest(request["successor_policy"]),
                ],
                config,
            )
            assert prepared["state"] == "CARRY_HANDOFF_PREPARED"
            async with private_http_client(config.core_url, config.bridge_token) as client:
                response = await client.post(
                    "/internal/v1/owner-authority/inputs/state",
                    json=capture.model_dump(mode="json"),
                )
                assert response.json() == original_state
                control_headers = {
                    "Authorization": "Bearer " + core.settings.control_token.get_secret_value()
                }
                query = {"expected_revision": 2, "policy": request["successor_policy"]}
                preview_response = await client.post(
                    "/internal/v1/owner-authority/preview", json=query, headers=control_headers
                )
                assert preview_response.status_code == 200, preview_response.text
                activated = await client.post(
                    "/internal/v1/owner-authority/activate",
                    json={
                        **query,
                        "operation_id": "native-carry-activate",
                        "preview_sha256": preview_response.json()["preview_sha256"],
                    },
                    headers=control_headers,
                )
                assert activated.status_code == 200, activated.text
                successor = activated.json()["authority"]

                async def retained_scope(authority):
                    if meme_enabled:
                        assert core.settings.meme_semantics_enabled
                        assert core.settings.expression_enabled
                        for purpose in ("meme", "expression"):
                            checked = await client.post(
                                "/internal/v1/owner-authority/check",
                                json={"authority": authority, "purpose": purpose},
                            )
                            assert checked.status_code == 200, checked.text
                            assert (
                                checked.json()["capability_policies"]["meme"]
                                == (old_policy.capability_policies["meme"])
                            )

                await retained_scope(successor)
                newer = replace(config, owner_authority=successor)
                assert (await bridge(["owner-handoff"], newer))["state"] == "ACTIVE"
                if evaluation_revision:
                    revision_stop = await client.post(
                        "/internal/v1/owner-authority/stop",
                        json={"authority": successor, "operation_id": "native-evaluation-stop"},
                        headers=control_headers)
                    assert revision_stop.status_code == 200, revision_stop.text
                    with Spool(newer) as spool:
                        OwnerRuntime(spool, clock=lambda: core.clock.now).stop()
                    normal = await bridge(["owner-handoff-prepare"], newer)
                    assert normal["state"] == "HANDOFF_PREPARED"
                    policy = policy.model_copy(update={
                        "purposes": [*policy.purposes, "evaluation"],
                        "authorization_sha256": "a" * 64})
                    request["successor_policy"] = policy.model_dump(mode="json")
                    query = {"expected_revision": successor["revision"] + 1,
                             "policy": policy.model_dump(mode="json")}
                    preview = await client.post("/internal/v1/owner-authority/preview",
                                                 json=query, headers=control_headers)
                    assert preview.status_code == 200, preview.text
                    revised = await client.post("/internal/v1/owner-authority/activate", json={
                        **query, "operation_id": "native-evaluation-revision",
                        "preview_sha256": preview.json()["preview_sha256"]},
                        headers=control_headers)
                    assert revised.status_code == 200, revised.text
                    successor = revised.json()["authority"]
                    newer = replace(config, owner_authority=successor)
                    assert (await bridge(["owner-handoff"], newer))["state"] == "ACTIVE"
                    await retained_scope(successor)
                # Old source/reference cannot become an independent new input.
                assert (
                    await client.post(
                        "/internal/v1/owner-authority/inputs/approve",
                        json=capture.model_dump(mode="json"),
                    )
                ).status_code >= 400
                with Spool(newer) as spool:
                    runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    frame = encode(
                        raw_event(
                            number=43,
                            time=int(core.clock.now.timestamp()),
                            message=[{"type": "text", "data": {"text": "现在普通聊天"}}],
                        )
                    )
                    value = await prepare_owner_input(
                        runtime, client, None, frame, core.clock.now, ("127.0.0.1", 3001)
                    )
                    assert runtime.persist_capture(
                        value["event"], frame, received_at=core.clock.now, peer=("127.0.0.1", 3001)
                    )
                    assert await runtime.flush_one(client)
                    await semantic_offer(runtime, client, value["event"]["event_id"])
                text_worker = await child(
                    tmp_path,
                    "new-worker",
                    core,
                    mode="worker-text",
                    output=str(tmp_path / "new-provider.json"),
                )
                workers.append(text_worker)
                await finished(text_worker)
                wire = json.loads((tmp_path / "new-provider.json").read_text("utf-8"))
                assert len(wire) == 1 and "video_url" not in encode(wire)
                sends = []
                with Spool(newer) as spool:
                    runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    async with private_http_client(
                        newer.onebot_http_url,
                        newer.onebot_token,
                        transport=httpx.MockTransport(lambda req: provider(req, sends)),
                    ) as protocol:
                        guard = OwnerSendGuard(runtime, client)
                        assert await deliver_tick(
                            client, OwnerOneBot(guard, protocol), spool, guard=guard
                        )
                assert len(sends) == 1

                async def independent_input(number, bridge_config, mode):
                    with Spool(bridge_config) as spool:
                        runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                        frame = encode(
                            raw_event(
                                number=number,
                                time=int(core.clock.now.timestamp()),
                                message=[
                                    {"type": "text", "data": {"text": f"新的独立消息 {number}"}}
                                ],
                            )
                        )
                        value = await prepare_owner_input(
                            runtime, client, None, frame, core.clock.now, ("127.0.0.1", 3001)
                        )
                        assert runtime.persist_capture(
                            value["event"],
                            frame,
                            received_at=core.clock.now,
                            peer=("127.0.0.1", 3001),
                        )
                        assert await runtime.flush_one(client)
                        await semantic_offer(runtime, client, value["event"]["event_id"])
                    output = tmp_path / f"current-provider-{number}.json"
                    process = await child(
                        tmp_path, f"current-worker-{number}", core, mode=mode, output=str(output)
                    )
                    workers.append(process)
                    await finished(process)
                    assert len(json.loads(output.read_text("utf-8"))) == 1
                    if mode == "worker-timeout":
                        return
                    acknowledged = []
                    with Spool(bridge_config) as spool:
                        runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                        async with private_http_client(
                            bridge_config.onebot_http_url,
                            bridge_config.onebot_token,
                            transport=httpx.MockTransport(
                                lambda req: provider(
                                    req,
                                    acknowledged,
                                    {
                                        "status": "ok",
                                        "retcode": 0,
                                        "data": {"message_id": 123456 + number},
                                    },
                                )
                            ),
                        ) as protocol:
                            guard = OwnerSendGuard(runtime, client)
                            assert await deliver_tick(
                                client, OwnerOneBot(guard, protocol), spool, guard=guard
                            )
                    assert len(acknowledged) == 1

                if current_timeout:
                    await independent_input(44, newer, "worker-timeout")
                second_stop = await client.post(
                    "/internal/v1/owner-authority/stop",
                    json={"authority": successor, "operation_id": "native-second-stop"},
                    headers=control_headers,
                )
                assert second_stop.status_code == 200, second_stop.text
                with Spool(newer) as spool:
                    OwnerRuntime(spool, clock=lambda: core.clock.now).stop()
                preserved = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
                windows = core.store.rows("SELECT * FROM model_budget_windows ORDER BY window_id")
                if current_timeout:
                    query_carry = {
                        "authority": successor,
                        "carry_sha256": receipt["carry_sha256"],
                        "successor_policy_sha256": digest(request["successor_policy"]),
                    }
                    current_proof = await client.post(
                        "/internal/v1/owner-authority/model-carry-receipt", json=query_carry
                    )
                    assert current_proof.status_code == 200, current_proof.text
                    current_proof = current_proof.json()
                    assert current_proof["version"] == 2
                    assert current_proof["kind"] == "owner_model_unknown_carry_v2"
                    assert current_proof["origin_authority"] == plan.authority
                    assert current_proof["authority"] == successor
                    assert not set(current_proof["request_ids"]) & set(receipt["request_ids"])
                    assert len(current_proof["local_closures"]) == 1
                    with core.store.transaction():
                        view = owner_authority.custody(core.store, core.settings, settlement=True)
                    local_close = view["model_controls"][
                        current_proof["local_closures"][0]["record_sha256"]
                    ]
                    assert local_close["kind"] == "model_local_close"
                    assert local_close["authority"] == successor
                    arguments = [
                        "owner-carry-handoff-prepare",
                        "--carry-sha256",
                        receipt["carry_sha256"],
                        "--successor-policy-sha256",
                        digest(request["successor_policy"]),
                    ]
                    prepared_state = "CARRY_HANDOFF_PREPARED"
                else:
                    arguments, prepared_state = ["owner-handoff-prepare"], "HANDOFF_PREPARED"
                assert (await bridge(arguments, newer))["state"] == prepared_state
                repeated_query = {"expected_revision": successor["revision"] + 1,
                                  "policy": request["successor_policy"]}
                preview = await client.post(
                    "/internal/v1/owner-authority/preview",
                    json=repeated_query,
                    headers=control_headers,
                )
                assert preview.status_code == 200, preview.text
                activated_again = await client.post(
                    "/internal/v1/owner-authority/activate",
                    json={
                        **repeated_query,
                        "operation_id": "native-second-activate",
                        "preview_sha256": preview.json()["preview_sha256"],
                    },
                    headers=control_headers,
                )
                assert activated_again.status_code == 200, activated_again.text
                final_authority = activated_again.json()["authority"]
                await retained_scope(final_authority)
                assert final_authority == successor | {"revision": successor["revision"] + 2}
                assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == preserved
                assert (
                    core.store.rows("SELECT * FROM model_budget_windows ORDER BY window_id")
                    == windows
                )
                if current_timeout:
                    assert (
                        await client.post(
                            "/internal/v1/owner-authority/model-carry-receipt", json=query_carry
                        )
                    ).json() == current_proof
                final_config = replace(newer, owner_authority=final_authority)
                assert (await bridge(["owner-handoff"], final_config))["state"] == "ACTIVE"
                await independent_input(45, final_config, "worker-text")
            assert core.store.one("SELECT COUNT(*) n FROM outbox")["n"] == 2
            assert core.store.one("SELECT COUNT(*) n FROM outbox WHERE state='SENT'")["n"] == 2
            assert core.store.one("SELECT * FROM agent_tool_calls") == original_tool
            assert not core.store.one("SELECT 1 FROM meme_captions")
            assert not core.store.one("SELECT 1 FROM model_usage WHERE scenario='meme_caption'")
            assert (
                core.store.rows(
                    "SELECT * FROM model_usage WHERE request_id IN (?,?) ORDER BY rowid",
                    tuple(item["request_id"] for item in original_usage),
                )
                == original_usage
            )
        finally:
            for process in workers:
                await stopped(process)
            await stopped(api)

    run(scenario())
