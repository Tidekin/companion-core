"""Signed native image ingress → SDK receipt → independent extraction → recall payload."""

import asyncio
import json
import os
import sys
from pathlib import Path

from companion.config import ObservationAdapterConfig
from companion.store import encode
from tests.conftest import run
from tests.support.native_image_memory import native_image_memory


def test_image_receipt_reaches_independent_extractor_and_final_request(core, tmp_path):
    workspace = Path(__file__).resolve().parents[1]
    async def scenario():
        options = {"extraction_min_events": 1, "extraction_idle_seconds": 0,
                   "observation_adapters": {"content_image": ObservationAdapterConfig(
                       source="qq_image", token="synthetic-image-reference-token-12345",
                       media_analysis=True)}}
        async with native_image_memory(core, tmp_path, record_visible=True,
                                       core_options=options) as value:
            assert await value.worker.run_once("record-native-image", job_id=value.job["job_id"])
            job = core.store.one("SELECT state,last_error_code FROM jobs WHERE job_id=?",
                                 (value.job["job_id"],))
            assert job["state"] == "SUCCEEDED", job
            observed = core.store.one("SELECT * FROM observations "
                                       "WHERE source_kind='image_analysis'")
            assert observed and observed["result_status"] == "FETCHED", encode(value.calls[-1])
            assert all(row["state"] == "SETTLED" for row in core.store.rows(
                "SELECT state FROM model_usage"))
            core.store.execute("UPDATE events SET memory_processed=1 WHERE event_id<>?",
                               (value.job["event_id"],))
            config = core.settings.model_dump(mode="json")
            for key in ("model_key", "bridge_token", "control_token"):
                config[key] = getattr(core.settings, key).get_secret_value()
            config["observation_adapters"]["content_image"]["token"] = (
                core.settings.observation_adapters["content_image"].token.get_secret_value())
            path = tmp_path / "extractor.json"
            path.write_text(encode(config), encoding="utf-8")
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c",
                "import sys; from tests.support.memory_reference_process import run_worker; "
                "run_worker(sys.argv[1],sys.argv[2])", str(path), core.clock(),
                cwd=workspace,
                env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(child.communicate(), 60)
                assert child.returncode == 0, stderr.decode(errors="replace")
                receipt = json.loads(stdout.decode().splitlines()[-1])
                assert receipt["pid"] != os.getpid()
                assert "十二月九日入场" in receipt["calls"][0]["messages"][-1]["content"]
                assert any(row["origin"] == "event_observed" for row in receipt["memories"])
                assert all(row["state"] == "SETTLED" for row in receipt["usage"])
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.communicate()
            # Fresh question, so its original model request has no current images.
            event = await value.entry.admit(2, "青栎资料卡上是什么日期和座位？")
            assert await value.worker.run_once("recall-image-reference", job_id=core.store.one(
                "SELECT job_id FROM jobs WHERE event_id=?", (event["event_id"],))["job_id"])
            assert "十二月九日" in encode(value.calls[-1]["messages"])
            assert "event_observed" in encode(value.calls[-1]["messages"])
            assert not value.sends
            with core.store.transaction():
                core.ledger.expire_events(core.store, [value.job["event_id"]], core.clock())
            assert not core.memory.search("青栎资料卡")

    run(scenario())
