"""Extracted long history reaches the provider through TCP and a separate worker."""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from companion.model import ModelClient
from companion.store import encode
from tests.conftest import run
from tests.sdk_reader_fixture import answer
from tests.support.native_consumer_entry import environment


def test_extracted_long_history_reaches_actual_final_provider_request(
    core, tmp_path, record_property,
):
    history_size = 480
    workspace = Path(__file__).resolve().parents[1]
    async def scenario():
        candidates = []
        def provider(request):
            payload = json.loads(request.content)
            data = json.loads(payload["messages"][-1]["content"])
            if "events" in data:
                text = encode({"memories": candidates})
            else:
                text = encode({"relations": [{"supported": True, "update_authorized": False,
                    "claims_replacement": False, "same": None, "conflicts": [], "replaces": []}
                    for _ in data["candidates"]]})
            return httpx.Response(200, json=answer(text))
        async with environment(core, tmp_path, core_options={
            "extraction_min_events": 8, "extraction_idle_seconds": 0}, paid_options={
                "daily_calls": 2000, "window_calls": 2000}) as (entry, _):
            for start in range(0, history_size, 8):
                candidates = []
                for index in range(start, start+8):
                    text = ("芦虹资料铜盒放在绿柜二层。" if index == 0 else
                            f"第{index+1}次展务整理使用编号{index+1}的纸袋。")
                    event = await entry.admit(index+1, text)
                    candidates.append({"subject": "user", "kind": "fact",
                        "fact_key": "archive_box_KX71" if index == 0 else f"shelf_{index}",
                        "content": text, "evidence_event_id": event["event_id"],
                        "evidence_quote": text, "salience": 8})
                core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
                from companion.memory_work import checkpoint_state
                async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
                    core.model = ModelClient(core.settings, http)
                    completed = await core.extract_once()
                batch = (checkpoint_state(core.store) or {}).get("batch", {})
                assert completed, {key: batch.get(key) for key in ("phase", "error")}
                core.clock.advance(60)
            assert core.store.one("SELECT COUNT(*) n FROM runtime_meta WHERE "
                                  "key LIKE 'memory_importance:%%'")["n"] == history_size
            core.clock.advance(3*86400)
            event = await entry.admit(history_size+1, "archive_box_KX71 芦虹资料铜盒放在哪？")
            config = core.settings.model_dump(mode="json")
            for key in ("database", "model_key", "bridge_token", "control_token"):
                config[key] = getattr(core.settings, key).get_secret_value()
            path = tmp_path / "worker.private.json"
            path.write_text(encode(config), encoding="utf-8")
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c",
                "import sys; from tests.support.context_selection_process import run_worker; "
                "run_worker(sys.argv[1],sys.argv[2])", str(path), core.clock(),
                cwd=workspace,
                env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(child.communicate(), 60)
                assert child.returncode == 0, stderr.decode(errors="replace")
                value = json.loads(stdout.decode().splitlines()[-1])
                assert value["pid"] != os.getpid()
                job = core.store.one("SELECT state,last_error_code FROM jobs WHERE event_id=%s",
                                     (event["event_id"],))
                assert job["state"] == "SUCCEEDED", job
                prompt = encode(value["calls"][-1]["messages"])
                assert "芦虹资料铜盒放在绿柜二层" in prompt
                assert "memory_id" in prompt
                record_property("evidence_delivery", encode({"history_size": history_size,
                    "pid": value["pid"], "source_in_final_request": True,
                    "calls": len(value["calls"])}))
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.communicate()
    run(scenario())
