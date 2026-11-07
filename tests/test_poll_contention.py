"""Empty native polls remain authenticated without contending for the SQLite writer."""
import asyncio
import json
import sys
from pathlib import Path
from time import perf_counter

import pytest

from companion import meme_service, owner_input
from companion.contracts import LeaseRequest
from companion.transport import lease_deliveries
from tests.conftest import ingest, run
from tests.meme_authority_fixture import stop
from tests.support.api_store import PREFIX, child_done
from tests.support.meme_service import claim, discovery_environment, reservation, submit


def test_empty_polls_and_input_state_finish_while_independent_writer_is_held(
    core, tmp_path, monkeypatch, record_property,
):
    async def scenario():
        async with discovery_environment(core, tmp_path, monkeypatch) as (event, _, values):
            client, _, plan, _ = values
            capture = json.loads(core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                (owner_input.INPUT + event["event_id"],))["value"])["capture"]
            input_path = PREFIX + "/owner-authority/inputs/state"
            expected = await client.post(input_path, json=capture)
            assert expected.status_code == 200
            before = list(core.store.db.iterdump())
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", str(Path(__file__).with_name("api_store_lock_worker.py")),
                core.settings.database, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                assert (await asyncio.wait_for(process.stdout.readline(), 5)).strip() == b"LOCKED"
                started = perf_counter()
                responses = await asyncio.wait_for(asyncio.gather(
                    client.post(input_path, json=capture),
                    client.post(PREFIX + "/deliveries/lease", json={
                        "worker_id": "idle-test", "authority": plan.authority}),
                    client.post(PREFIX + "/memes/discovery/lease", json={
                        "worker_id": "idle-test"}),
                ), 2)
                record_property("three_polls_with_writer_held_ms",
                                round((perf_counter() - started) * 1000, 3))
                assert all(response.status_code == 200 for response in responses)
                assert [response.json() for response in responses] == [
                    expected.json(), {"items": []}, {"item": None}]
                assert (await client.get(PREFIX + "/health/live")).status_code == 200
                assert process.returncode is None
                assert list(core.store.db.iterdump()) == before
            finally:
                if process.returncode is None:
                    process.stdin.write(b"release\n")
                    await process.stdin.drain()
                await child_done(process)
            # Empty results carry the same permission checks as a real lease.
            missing = await client.post(PREFIX + "/deliveries/lease",
                                        json={"worker_id": "idle-test"})
            assert missing.status_code == 403
            changed = await client.post(PREFIX + "/deliveries/lease", json={
                "worker_id": "idle-test", "authority": {**plan.authority, "revision": 999}})
            assert changed.status_code == 409
            assert (await stop(client, plan)).status_code == 200
            for path, body in (
                ("/deliveries/lease", {"worker_id": "idle-test", "authority": plan.authority}),
                ("/memes/discovery/lease", {"worker_id": "idle-test"}),
            ):
                denied = await client.post(PREFIX + path, json=body)
                assert denied.status_code in {403, 409}
            historical = await client.post(input_path, json=capture)
            assert historical.status_code == 200 and historical.json()["terminal"]

    run(scenario())


@pytest.mark.parametrize("state", ["CANCELLED", "FAILED"])
def test_empty_meme_poll_recovers_expired_reserved_liability(core, tmp_path, monkeypatch, state):
    async def scenario():
        async with discovery_environment(core, tmp_path, monkeypatch) as (event, _, _):
            submit(core, event)
            item = claim(core)
            reservation(core, item)
            core.store.execute('UPDATE jobs SET state=%s WHERE job_id=%s', (state, item["job_id"]))
            core.clock.advance(core.settings.lease_seconds + 1)
            result = meme_service.lease(core.store, core.settings, core.clock,
                                       meme_service.DiscoveryLeaseRequest(worker_id="idle-test"))
            assert result == {"item": None}
            assert core.store.one("SELECT state FROM meme_discovery_reservations") == {
                "state": "UNKNOWN"}
            assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (item["job_id"],)) == {
                "state": state}

    run(scenario())


def test_delivery_poll_without_outbox_still_cancels_paused_proactive_job(core):
    accepted = ingest(core)
    core.store.execute("UPDATE jobs SET kind='activity' WHERE job_id=%s", (accepted["job_id"],))
    core.store.execute(
        "INSERT INTO runtime_meta (key,value) VALUES ('proactive_paused','1')"
        ' ON CONFLICT(key) DO UPDATE SET '
        'value=excluded.value,record_sequence=DEFAULT'
    )
    assert lease_deliveries(core.store, core.settings, LeaseRequest(worker_id="idle-test"),
                            core.clock()) == {"items": []}
    assert core.store.one('SELECT state,last_error_code FROM jobs WHERE job_id=%s',
                          (accepted["job_id"],)) == {
        "state": "CANCELLED", "last_error_code": "AUTONOMY_PAUSED"}
