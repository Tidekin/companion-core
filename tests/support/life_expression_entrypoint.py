import asyncio
import json
import os
import subprocess
import sys

from pydantic import SecretStr


async def native_child(core, tmp_path, source, destination, number, *, wording=None):
    config = tmp_path / f"life-{number}.json"
    result = tmp_path / f"life-{number}-result.json"
    config.write_text(json.dumps({
        "settings": core.settings.model_dump(mode="python"), "now": core.clock(),
        "source_id": source, "destination": destination, "result_path": str(result),
        "wording": wording,
    }, default=lambda value: value.get_secret_value()
       if isinstance(value, SecretStr) else str(value)), encoding="utf-8")
    process = await asyncio.to_thread(subprocess.run,
        [sys.executable, "-B", "-m", "tests.life_expression_process", str(config)],
        capture_output=True, timeout=60)
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    outcome = json.loads(result.read_text(encoding="utf-8"))
    assert outcome["pid"] != os.getpid()
    return outcome
