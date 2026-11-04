import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from companion_onebot_bridge import journal, owner_handoff, owner_notices
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool, encode
from tests.support.onebot_events import raw_event
from tests.test_bridge_owner_notices import frame
from tests.test_bridge_owner_resolution import reserve_uncertain
from tests.test_bridge_owner_runtime import config as config
from tests.test_reading_followup import window


def core_handler(config, successor):
    def handler(request):
        if request.url.path.endswith("stop-receipt"):
            return httpx.Response(200, json={"authority": config.owner_authority,
                "state": "STOPPED",
                "stopped_authority": config.owner_authority | {"revision": 2},
                "core_stop_sha256": "e" * 64, "budget_journal_id": "budget_" + "f" * 32,
                "quiescence_proven": False})
        return httpx.Response(200, json={"approved": True, "authority": successor,
            "purpose": "chat", "expires_at": None, "budget_journal_id": "budget_" + "f" * 32,
            "capability_policies": {},
            "capture": {"public_key": config.capture_public_key,
                "identity_sha256": journal.digest([config.owner_qq_id, config.bot_qq_id]),
                "protocol_epoch": config.protocol_epoch}})
    return handler


def test_handoff_preserves_original_journal_and_fences_old_configuration(config):
    with Spool(config) as spool:
        OwnerRuntime(spool).stop()
    successor = config.owner_authority | {"revision": 3, "policy_sha256": "e" * 64}
    with Spool(config, reconciliation_only=True) as spool:
        runtime = OwnerRuntime(spool, clock=lambda: datetime.now(UTC) + timedelta(seconds=1))
        prefix = runtime.verify(active=False)

        async def run():
            async with httpx.AsyncClient(base_url="http://127.0.0.1:8765",
                transport=httpx.MockTransport(core_handler(config, successor))) as client:
                assert (await owner_handoff.prepare(runtime, client))["state"] == "HANDOFF_PREPARED"
                prepared = runtime.row()["journal_head"]
                assert (await owner_handoff.prepare(runtime, client))["journal_head"] == prepared
                result = await owner_handoff.handoff(runtime, client, successor)
                assert result["state"] == "ACTIVE"
        asyncio.run(run())
        assert journal.read(runtime.path)[0][:len(prefix)] == prefix
    with Spool(replace(config, owner_authority=successor)) as spool:
        runtime = OwnerRuntime(spool)
        runtime.verify()
        runtime.stop()
    with Spool(config) as spool:
        with pytest.raises(ValueError, match="OWNER_AUTHORITY_BINDING_CHANGED"):
            OwnerRuntime(spool)


@pytest.mark.parametrize("pending", ["unknown", "recall", "media"])
def test_handoff_refuses_unsettled_original_custody(config, pending):
    if pending == "unknown":
        reserve_uncertain(config)
    else:
        with Spool(config) as spool:
            runtime = OwnerRuntime(spool)
            if pending == "recall":
                now = runtime.clock()
                assert owner_notices.put(runtime, frame(config, now), now, ("127.0.0.1", 1))
            else:
                spool.db.execute("CREATE TABLE automatic_media("
                                 "event_id TEXT,plan_hash TEXT,state TEXT)")
                spool.db.execute("INSERT INTO automatic_media VALUES('event',?,'WAITING')",
                                  (journal.digest(runtime.authority),))
                spool.db.commit()
                with pytest.raises(ValueError, match="AUTOMATIC_MEDIA_JOURNAL_CHANGED"):
                    runtime.stop()
                return
            runtime.stop()
    with Spool(config, reconciliation_only=True) as spool:
        runtime = OwnerRuntime(spool, clock=lambda: datetime.now(UTC) + timedelta(seconds=1))

        async def run():
            async with httpx.AsyncClient(base_url="http://127.0.0.1:8765",
                transport=httpx.MockTransport(core_handler(config, {}))) as client:
                with pytest.raises(ValueError, match="RECONCILIATION_REQUIRED"):
                    await owner_handoff.prepare(runtime, client)
        asyncio.run(run())
        assert runtime.row()["state"] == "STOPPED"
        assert all(record["kind"] != "OWNER_HANDOFF_PREPARED"
                   for record in runtime.verify(active=False))


def test_handoff_cannot_run_beside_original_process(config):
    with Spool(config) as spool:
        OwnerRuntime(spool).stop()
        with pytest.raises(RuntimeError, match="BRIDGE_ALREADY_RUNNING"):
            asyncio.run(owner_handoff.command(config, SimpleNamespace(command="owner-handoff")))


def test_handoff_does_not_cross_restore_generation(config):
    with Spool(config) as spool:
        OwnerRuntime(spool).stop()
    with Spool(config, reconciliation_only=True) as spool:
        runtime = OwnerRuntime(spool, clock=lambda: datetime.now(UTC) + timedelta(seconds=1))
        successor = config.owner_authority | {"revision": 3, "restore_generation": "restored"}

        async def run():
            async with httpx.AsyncClient(base_url="http://127.0.0.1:8765",
                transport=httpx.MockTransport(core_handler(config, successor))) as client:
                await owner_handoff.prepare(runtime, client)
                with pytest.raises(ValueError, match="OWNER_HANDOFF_BINDING_CHANGED"):
                    await owner_handoff.handoff(runtime, client, successor)
        asyncio.run(run())


@pytest.mark.parametrize("buffered", [False, True])
def test_real_core_stop_prepare_and_current_successor_handoff(
    core, tmp_path, monkeypatch, buffered,
):
    from companion_onebot_bridge.event_normalizer import normalize

    core.settings.control_token = SecretStr("native-handoff-owner-token")
    owner = {"Authorization": "Bearer native-handoff-owner-token"}

    async def run():
        async with window(core, tmp_path, monkeypatch) as (client, base, plan):
            config = replace(base, owner_authority=plan.authority,
                capture_signing_key=plan.key.private_bytes_raw().hex(),
                data_dir=str(tmp_path / "handoff-bridge"))
            with Spool(config) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                if buffered:
                    raw = encode(raw_event(1, time=int(core.clock.now.timestamp()), text="queued"))
                    event = normalize(raw, config, now=core.clock.now.timestamp())
                    runtime.persist_capture(event, raw, received_at=core.clock.now,
                                            peer=("127.0.0.1", 1))
                runtime.stop()
            stopped = await client.post("/internal/v1/owner-authority/stop", json={
                "authority": plan.authority, "operation_id": "stop-for-native-handoff"},
                headers=owner)
            assert stopped.status_code == 200, stopped.text
            core.clock.advance(60)
            with Spool(config, reconciliation_only=True) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                prepared = await owner_handoff.prepare(runtime, client)
                assert prepared["state"] == "HANDOFF_PREPARED"
                value = {"expected_revision": 2, "policy": plan.policy.model_dump(mode="json")}
                preview = await client.post("/internal/v1/owner-authority/preview",
                                             json=value, headers=owner)
                assert preview.status_code == 200, preview.text
                activated = await client.post(
                    "/internal/v1/owner-authority/activate", headers=owner,
                    json=value | {"operation_id": "activate-after-native-stop",
                                  "preview_sha256": preview.json()["preview_sha256"]})
                assert activated.status_code == 200, activated.text
                successor = activated.json()["authority"]
                result = await owner_handoff.handoff(runtime, client, successor)
                assert result["state"] == "ACTIVE"
            with Spool(replace(config, owner_authority=successor)) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                runtime.verify()
                assert not await runtime.flush_one(client)
                if buffered:
                    assert tuple(spool.db.execute("SELECT state,body FROM inbox").fetchone()) == (
                        "closed", None)
                    assert not core.store.one("SELECT 1 FROM events")
    asyncio.run(run())


@pytest.mark.parametrize("case", ["unbound", "bound_work", "stale_target"])
def test_bridge_hands_off_across_an_unbound_core_stop_chain(core, tmp_path, monkeypatch, case):
    """A failed release can ACTIVATE and STOP Core again before the Bridge hands off."""
    from companion import owner_stop_receipt
    from companion.contracts import CoreError
    from companion_delivery_protocol.authority import AuthorityRef

    core.settings.control_token = SecretStr("native-handoff-owner-token")
    owner = {"Authorization": "Bearer native-handoff-owner-token"}

    async def run():
        async with window(core, tmp_path, monkeypatch) as (client, base, plan):
            config = replace(base, owner_authority=plan.authority,
                capture_signing_key=plan.key.private_bytes_raw().hex(),
                data_dir=str(tmp_path / "chain-bridge"))
            with Spool(config) as spool:
                OwnerRuntime(spool, clock=lambda: core.clock.now).stop()
            policy = plan.policy.model_dump(mode="json")

            async def post(path, value):
                response = await client.post(path, json=value, headers=owner)
                assert response.status_code == 200, response.text
                return response.json()

            async def activate(expected, operation):
                value = {"expected_revision": expected, "policy": policy}
                preview = await post("/internal/v1/owner-authority/preview", value)
                return (await post("/internal/v1/owner-authority/activate", value | {
                    "operation_id": operation, "preview_sha256": preview["preview_sha256"]}))[
                        "authority"]

            await post("/internal/v1/owner-authority/stop",
                       {"authority": plan.authority, "operation_id": "chain-stop-1"})
            unbound = await activate(2, "chain-activate-unbound")
            if case == "bound_work":
                now = core.clock()
                core.store.execute(
                    "INSERT INTO jobs(job_id,kind,state,idempotency_key,created_at,updated_at,"
                    "authority_json) VALUES('job_chain','life','QUEUED','chain-key',?,?,?)",
                    (now, now, encode(unbound)))
            await post("/internal/v1/owner-authority/stop",
                       {"authority": unbound, "operation_id": "chain-stop-2"})
            core.clock.advance(60)
            ref = AuthorityRef.from_dict(plan.authority)
            with core.store.transaction(), pytest.raises(CoreError):
                owner_stop_receipt.read(core.store, core.settings, plan.authority)
            with Spool(config, reconciliation_only=True) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                if case == "bound_work":
                    with core.store.transaction():
                        assert owner_stop_receipt.stopped_chain(core.store, ref) is None
                    with pytest.raises(httpx.HTTPStatusError) as refused:
                        await owner_handoff.prepare(runtime, client)
                    assert refused.value.response.status_code == 409
                    assert runtime.row()["state"] == "STOPPED"
                    return
                prepared = await owner_handoff.prepare(runtime, client)
                assert prepared["state"] == "HANDOFF_PREPARED"
                if case == "stale_target":
                    with pytest.raises(ValueError, match="OWNER_HANDOFF_BINDING_CHANGED"):
                        await owner_handoff.handoff(runtime, client, unbound)
                    assert runtime.row()["state"] == "STOPPED"
                    return
                successor = await activate(4, "chain-activate-final")
                assert successor["revision"] == plan.authority["revision"] + 4
                result = await owner_handoff.handoff(runtime, client, successor)
                assert result == {"state": "ACTIVE", "authority": successor,
                                  "journal_head": runtime.row()["journal_head"]}
            with Spool(replace(config, owner_authority=successor)) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                runtime.verify()
                assert not await runtime.flush_one(client)
    asyncio.run(run())


def test_stop_chain_keeps_the_direct_policy_rule():
    from companion_onebot_bridge.owner_native_resolution import stop_chain

    authority = {"version": 2, "grant_id": "g", "revision": 7, "policy_sha256": "a" * 64,
                 "restore_generation": "initial"}
    assert stop_chain(authority, authority | {"revision": 8})
    assert not stop_chain(authority, authority | {"revision": 8, "policy_sha256": "b" * 64})
    assert stop_chain(authority, authority | {"revision": 10, "policy_sha256": "b" * 64})
    for change in ({"revision": 9}, {"revision": 7}, {"revision": 6},
                   {"revision": 10, "grant_id": "other"},
                   {"revision": 10, "restore_generation": "restored"}):
        assert not stop_chain(authority, authority | change)
