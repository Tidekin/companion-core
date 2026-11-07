import asyncio
import json
import os
import sys

from companion.config import ObservationAdapterConfig
from tests.support.qzone_reader import TOKEN, URL, account_file, response


def options():
    return {"content_enabled": True, "observation_enabled": True, "qzone_reading_enabled": True,
        "quiet_start": 0, "quiet_end": 0, "proactive_idle_seconds": 0,
        "exploration_daily_requests": 8, "exploration_monthly_requests": 16,
        "exploration_daily_bytes": 4194304,
        "observation_adapters": {"qzone": ObservationAdapterConfig(
            source="qzone", token=TOKEN, source_grant_required=True)}}


async def selected(core, entry, tmp_path):
    event = await entry.admit(1, "请读取我选择的空间目标。PRIVATE_OWNER_SENTINEL")
    control = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
    account = tmp_path / "qzone-account.json"
    grant = account_file(account, core.clock.now)
    registered = await entry.client.post("/internal/v2/source-grants", headers=control,
                                         json={"adapter_id": "qzone", **grant})
    assert registered.status_code == 201, registered.text
    body = {"operation_id": "qzone-target-one", "adapter_id": "qzone", "url": URL,
            "source_event_id": event["event_id"], "source_grant": grant}
    target = await entry.client.post("/internal/v2/qzone/targets", headers=control, json=body)
    assert target.status_code == 202, target.text
    repeated = await entry.client.post("/internal/v2/qzone/targets", headers=control, json=body)
    assert repeated.status_code == 202 and repeated.json()["duplicate"]
    return event, control, account, grant, target.json()["request_id"], body


async def reader_process(core, client, account):
    payload = {"origin": str(client.base_url), "now": core.clock(),
               "target_uin": "12345678", "account_file": str(account),
               "response": response(core.clock.now)}
    child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
        "tests.support.qzone_reader_process", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "COMPANION_READER_TOKEN": TOKEN})
    try:
        output, error = await asyncio.wait_for(child.communicate(
            json.dumps(payload).encode()), 30)
        assert child.returncode == 0, (output, error)
        result = json.loads(output.decode().splitlines()[-1])
        assert result["pid"] != os.getpid()
        return result
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
