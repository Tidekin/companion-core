"""Real signed ingress/API, independent Core worker and actual Bridge daemon, loopback only."""

import asyncio
import json

import pytest

from tests.conftest import run
from tests.support.evaluation_process import process_config
from tests.support.meme_core_process import check_signed_core_worker_and_bridge_delivery, process
from tests.support.meme_core_runtime import runtime_case as runtime_case


@pytest.mark.parametrize("runtime_case", ["no_current"], indirect=True)
def test_signed_core_worker_and_bridge_process_use_one_original_image(runtime_case, tmp_path):
    return check_signed_core_worker_and_bridge_delivery(runtime_case, tmp_path)


def test_original_root_withdrawal_at_actual_model_boundary_never_calls_provider(
    runtime_case, tmp_path
):
    c = runtime_case
    assert "meme_catalog_owner" in type(c.core.settings).model_fields
    # The original test claimed a native lease to exercise domain preparation;
    # no model reservation or I/O has begun. Let the actual independent Worker claim it.
    c.core.store.execute(
        "UPDATE jobs SET state='QUEUED',lease_token=NULL WHERE job_id=?", (c.job["job_id"],)
    )
    config = tmp_path / "model-boundary.json"
    process_config(c.core, config)
    before = c.core.store.rows("SELECT * FROM model_usage")

    async def scenario():
        child = await process(
            "tests.owner_runtime_process",
            "--config",
            config,
            "--now",
            c.core.clock(),
            "--fence",
            stdin=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(25):
                marker = await child.stdout.readline()
            if not marker:
                _, error = await child.communicate()
                raise AssertionError(error.decode(errors="replace"))
            assert marker.strip() == b"BEFORE_DISPATCH", marker
            with c.core.store.transaction():
                c.core.ledger.expire_events(c.core.store, [c.source["event_id"]], c.core.clock())
            child.stdin.write(b"continue\n")
            await child.stdin.drain()
            async with asyncio.timeout(20):
                output, error = await child.communicate()
            assert child.returncode == 0, error.decode(errors="replace")
            report = json.loads(output.decode().strip().splitlines()[-1])
            assert report["worked"] and report["calls"] == 0
            assert not c.core.store.rows("SELECT * FROM outbox")
            assert (
                c.core.store.one(
                    "SELECT * FROM model_usage WHERE request_id=?", (before[0]["request_id"],)
                )
                == before[0]
            )
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

    run(scenario())
