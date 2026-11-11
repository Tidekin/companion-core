"""Real import CLI, signed HTTP ingress, independent worker and delivery fence."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime

import httpx
import pytest

from companion import history_import
from companion.conversation_policy import ConversationPolicyConfig
from companion.store import utcnow
from tests.support.core_http import real_core_http
from tests.support.evaluation_process import process_config
from tests.support.history_import_package import package
from tests.support.owner_memory import environment


def toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key) + "=" + toml(item)
                                for key, item in value.items() if item is not None) + "}"
    if isinstance(value, list):
        return "[" + ",".join(toml(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


@pytest.mark.parametrize("strategy", ["participation_v2", "character_v1"])
def test_import_cli_to_signed_http_and_real_independent_worker(core, tmp_path, strategy):
    core.clock.now = datetime.fromisoformat(utcnow())
    core.settings.conversation_policy = ConversationPolicyConfig(strategy=strategy)

    async def scenario():
        async with environment(core, tmp_path, additional_purposes=("expression",)) as context:
            admit, _, authority = context
            config = tmp_path / "worker.json"
            process_config(core, config)
            values = json.loads(config.read_text("utf-8"))
            values["segmented_reply"] = False
            deployed = tmp_path / "config.toml"
            deployed.write_text("\n".join(json.dumps(key) + "=" + toml(value)
                for key, value in values.items() if value is not None), encoding="utf-8")
            request, raw, _ = package()
            source, manifest = tmp_path / "source.txt", tmp_path / "request.json"
            source.write_bytes(raw)
            manifest.write_text(request.model_dump_json(), encoding="utf-8")
            sha = history_import.digest(manifest.read_bytes())

            def cli(command, *extra):
                result = subprocess.run([sys.executable, "-B", "-m", "companion", command,
                    *extra], env={**os.environ, "COMPANION_CONFIG": str(deployed)},
                    capture_output=True, text=True, encoding="utf-8", timeout=30)
                assert result.returncode == 0, result.stderr
                return json.loads(result.stdout)

            args = ("--source", str(source), "--request", str(manifest), "--request-sha256", sha)
            assert cli("preview-history", *args)["segments"]["analysis"] == 1
            assert core.store.rows("SELECT * FROM events") == []
            imported = cli("import-history", *args)
            assert imported["created"]
            assert not cli("import-history", *args)["created"]
            assert core.store.rows("SELECT * FROM jobs") == []
            restored = tmp_path / "exported.txt"
            cli("export-history", "--archive-event-id", imported["archive_event_id"],
                "--output", str(restored))
            assert restored.read_bytes() == raw
            core.clock.now = datetime.fromisoformat(utcnow())
            assert core.memory_context.recall("米饭作为搬家后的暗号", core.clock())["rows"]
            capture, accepted = await admit(400, "你记得“米饭作为搬家后的暗号”这个约定吗？")
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                "tests.support.history_import_process", "--config", str(config),
                "--now", core.clock(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                output, error = await asyncio.wait_for(child.communicate(), timeout=60)
                assert child.returncode == 0, error.decode()
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            result = json.loads(output)
            job = core.store.one("SELECT * FROM jobs WHERE job_id=%s", (accepted["job_id"],))
            assert job["state"] == "SUCCEEDED", (json.dumps(job), result, error.decode())
            assert result == {"calls": 2, "character_in_context": True,
                "actions_in_context": True, "memory_in_context": True,
                "analysis_replayed": False, "reasoning_fields": False,
                "character_expression_policy": strategy == "character_v1",
                "natural_expression_policy": strategy == "participation_v2",
                "common_rules_retained": True}
            row = core.store.one("SELECT * FROM outbox WHERE origin_event_id=%s",
                                 (capture.event_id,))
            assert "（尾巴尖轻轻晃了晃）" in json.loads(row["content_json"])["text"]
            # An explicit role change must also fence an already-created reply.
            history_import.clear_character(core)
            async with real_core_http(core) as port, httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False,
                headers={"Authorization": "Bearer "
                         + core.settings.bridge_token.get_secret_value()},
            ) as client:
                leased = await client.post("/internal/v1/deliveries/lease",
                    json={"worker_id": "synthetic-bridge", "authority": authority})
                assert leased.status_code == 200, leased.text
                assert leased.json()["items"] == []

    asyncio.run(scenario())
