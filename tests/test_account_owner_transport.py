"""Account HTTP custody uses its native proof and the original owner reference."""

import asyncio
import json
import sys

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from companion.api import create_app
from companion.contracts import LeaseRequest
from companion.model import ModelClient
from tests.support.core_http import real_core_http
from tests.test_social_autonomy import background, queue
from tests.test_social_owner_authority import (
    authorize,
    close_provider_clients,  # noqa: F401 - pytest autouse fixture for the shared helper
    stop,
)


def prepared(core, tmp_path):
    module, source, conversation = background(core, max_output_tokens=512)
    _, ref = authorize(core, tmp_path)

    async def plan():
        def reply(request):
            assert request.url.host == "synthetic.invalid"
            data = json.loads(json.loads(request.content)["messages"][-1]["content"])
            proposal = {"kind": "qzone_publish", "text": "Synthetic public observation",
                        "source_refs": [data["sources"][0]["ref"]]}
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(proposal)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply),
                                     trust_env=False) as client:
            module.model = ModelClient(core.settings, client)
            return await module.plan("account-transport-test", source_ids=[source])

    frozen = queue(module, asyncio.run(plan()), conversation)
    return ref, frozen


def test_account_http_state_and_lease_use_exact_authority(core, tmp_path):
    ref, frozen = prepared(core, tmp_path)
    app = create_app(core.settings)
    app.state.core = core
    client = TestClient(app, headers={
        "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()})
    path = "/internal/v1/account-actions/profile-baselines/state"
    query = {"authority": json.dumps(ref), "authorization_sha256": frozen.authorization_sha256}
    response = client.get(path, params=query)
    assert response.status_code == 200, response.text
    assert response.json()["authority"] == ref
    assert client.get(path, params={**query, "authority": "{}"}).status_code == 400
    assert client.get(path, params={"window_sha256": "a" * 64,
        "authorization_sha256": frozen.authorization_sha256}).status_code == 400
    request = dict(worker_id="account-test", authority=ref, delivery_lane="qq_account",
                   account_actions_authorization_sha256=frozen.authorization_sha256)
    with pytest.raises(ValidationError):
        LeaseRequest(**request, window_sha256="a" * 64)
    foreign = {**ref, "revision": ref["revision"] + 1}
    rejected = client.post("/internal/v1/deliveries/lease", json={**request, "authority": foreign})
    assert rejected.status_code == 409, rejected.text
    response = client.post("/internal/v1/deliveries/lease", json=request)
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["authority"] == ref and "window_sha256" not in item
    assert item["message_id"] == frozen.message_id


@pytest.mark.parametrize("fence", [False, True])
def test_account_real_tcp_independent_delivery_worker(core, tmp_path, fence):
    ref, frozen = prepared(core, tmp_path)

    async def scenario():
        async with real_core_http(core) as port:
            config = tmp_path / "account-worker.json"
            config.write_text(json.dumps({"url": f"http://127.0.0.1:{port}",
                "token": core.settings.bridge_token.get_secret_value(), "authority": ref,
                "authorization_sha256": frozen.authorization_sha256,
                "payload_sha256": frozen.payload_sha256}), encoding="utf-8")
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                "tests.account_owner_process", str(config), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                async with asyncio.timeout(30):
                    signal = await child.stdout.readline()
                    assert signal.strip() == b"BEFORE_SEND_CHECK", signal
                    if fence:
                        stop(core, ref)
                    output, error = await child.communicate(b"continue\n")
                    assert child.returncode == 0, error.decode()
                    result = json.loads(output)
                    assert result["status"] == (403 if fence else 200), result
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()

    asyncio.run(scenario())
    reservation = core.store.one("SELECT dispatched_at FROM social_autonomy_reservations")
    assert bool(reservation["dispatched_at"]) is not fence
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
