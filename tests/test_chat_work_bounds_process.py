"""Real signed WS/HTTP ingress, independent worker and native segmented delivery."""

import asyncio
import json
import os
import sys
from pathlib import Path

from companion import image_spool
from companion.config import SegmentedReplyConfig
from companion_onebot_bridge.metrics import Metrics
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from tests.conftest import run
from tests.support.evaluation_process import process_config
from tests.support.native_consumer_entry import environment


def test_independent_chat_survives_writer_contention_without_scanning_media(
    core, tmp_path,
):
    workspace = Path(__file__).resolve().parents[1]

    async def scenario():
        policy = SegmentedReplyConfig(max_segments=3, min_chars=4, interval_ms=600)
        async with environment(core, tmp_path, purposes=("chat",),
                               core_options={"segmented_reply": policy}) as (entry, sends):
            event = await entry.admit(1, "合成的并发聊天输入")
            root = image_spool.directory(core.store)
            root.mkdir(mode=0o700)
            for number in range(97):
                (root / f"content_{number:032x}.raw").write_bytes(b"synthetic orphan")
            settings_path = tmp_path / "worker-settings.json"
            process_config(core, settings_path)
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c",
                "import sys; from tests.support.chat_claim_worker import run_worker; "
                "run_worker(sys.argv[1], sys.argv[2])", str(settings_path), core.clock(),
                cwd=workspace, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                   "COMPANION_TELEMETRY": "0"},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)

            async def phase(name):
                while line := await asyncio.wait_for(child.stdout.readline(), 45):
                    record = json.loads(line)
                    if record.get("phase") == name:
                        return record
                raise AssertionError("worker exited before " + name)

            try:
                ready = await phase("ready")
                assert ready["pid"] != os.getpid()
                with core.store.transaction(control_write=True):
                    child.stdin.write(b"continue\n")
                    await child.stdin.drain()
                    assert (await phase("busy"))["provider_calls"] == 0
                result = await phase("finished")
                stdout, stderr = await asyncio.wait_for(child.communicate(), 45)
                assert child.returncode == 0, (stdout + stderr).decode(errors="replace")
                assert result["provider_calls"] == 1 and result["busy_claims"] >= 1
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            assert len(list(root.iterdir())) == 97
            job = core.store.one('SELECT state,attempts FROM jobs WHERE event_id=%s',
                                 (event["event_id"],))
            assert job == {"state": "SUCCEEDED", "attempts": 1}
            assert core.store.rows("SELECT state FROM model_usage") == [{"state": "SETTLED"}]
            records = []
            observed = Metrics("OFFLINE_FIXTURE", records.append)
            observed.start(event, 0)
            for _ in range(2):
                guard = OwnerSendGuard(entry.runtime, entry.client)
                assert await deliver_tick(entry.client, OwnerOneBot(guard, entry.protocol),
                                          entry.spool, guard=guard, metrics=observed)
                core.clock.advance(0.6)
            assert len(sends) == 2
            assert core.store.rows("SELECT state,attempt_count FROM outbox") == [
                {"state": "SENT", "attempt_count": 1}, {"state": "SENT", "attempt_count": 1}]
            assert len([r for r in records if r["stage"] == "t_reply_group_ack"]) == 1
            assert len([r for r in records if r["stage"] == "t_reply_segments_ack"]) == 1

    run(scenario())
