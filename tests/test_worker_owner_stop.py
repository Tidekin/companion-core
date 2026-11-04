"""Idle caption scans keep the native chat worker alive across owner STOP."""

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from companion import owner_authority, owner_input
from companion.owner_authority_protocol import AuthorityStop, OwnerCapture
from companion.store import utcnow
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.owner_authority import activate
from tests.test_meme_core import body, caption_environment, worker
from tests.test_owner_authority import prepare
from tests.test_owner_input import captured
from tests.test_worker_wakeup_process import close_child, finish, ready, signed_input, start, state


@pytest.mark.parametrize("fence", ["stop", "restore", "disabled"])
def test_idle_caption_lane_waits_without_owner_authority(core, tmp_path, fence):
    core.settings.meme_semantics_enabled = True
    policy = prepare(core, tmp_path, enabled=fence != "disabled")
    if fence != "disabled":
        active, _ = activate(core, policy)
        if fence == "stop":
            owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                operation_id="idle-worker-stop", authority=active["authority"],
            ))
        else:
            with core.store.transaction():
                owner_authority.restore(core.store)

    for _ in range(2):
        assert not run(core.run_once("idle-owner-worker"))
    assert core.store.rows("SELECT * FROM jobs") == []
    assert core.store.rows("SELECT * FROM model_usage") == []
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.model.calls == []
    assert owner_authority.status(core.store, core.settings, core.clock)["state"] == "OFF"


@pytest.mark.parametrize("fence", ["owner_waiting", "restore"])
def test_queued_caption_keeps_owner_priority_and_revocation(core, tmp_path, monkeypatch, fence):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, _, grant, now, item = values
            response = await client.post("/internal/v1/memes/captions",
                                         json=body(core, event, item, now))
            assert response.status_code == 200, response.text
            job_id = response.json()["job_id"]
            if fence == "owner_waiting":
                source = core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                                        (owner_input.INPUT + event["event_id"],))
                template = OwnerCapture.model_validate(json.loads(source["value"])["capture"])
                capture, _ = signed_input(core, template, grant.key, 999)
                owner_input.approve(core.store, core.settings, core.clock, capture)
            else:
                with core.store.transaction():
                    owner_authority.restore(core.store)

            calls = []
            assert await worker(core, calls) == (fence == "restore")
            assert core.store.one(
                "SELECT state,attempts,lease_token,last_error_code FROM jobs WHERE job_id=?",
                (job_id,),
            ) == {"state": "CANCELLED" if fence == "restore" else "QUEUED",
                  "attempts": 0, "lease_token": None,
                  "last_error_code": "OWNER_AUTHORITY_DISABLED" if fence == "restore" else None}
            assert core.store.rows("SELECT * FROM model_usage") == []
            assert core.store.rows("SELECT * FROM outbox") == []
            assert core.model.calls == []
            assert calls == []

    run(scenario())


@pytest.mark.parametrize("initially_stopped", [False, True])
def test_owner_stop_and_activation_keep_one_independent_worker(
    core, tmp_path, initially_stopped,
):
    core.clock = utcnow
    core.settings.meme_semantics_enabled = True
    core.settings.control_token = SecretStr("synthetic-worker-stop-control-token")
    template, key = captured(core, tmp_path)
    policy = owner_authority.current(core.store, core.settings, core.clock).policy

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer synthetic-worker-stop-control-token"},
        ) as api:
            async def stop():
                response = await api.post("/internal/v1/owner-authority/stop", json={
                    "operation_id": "independent-worker-stop",
                    "authority": template.authority,
                })
                assert response.status_code == 200, response.text
                assert response.json()["authority"]["revision"] == 2

            if initially_stopped:
                await stop()
            directory = tmp_path / "owner-worker"
            child = await start(core, directory, duration=30, controlled=True)
            try:
                await ready(child)
                scans = int((directory / "scans").read_text())
                if not initially_stopped:
                    await stop()
                    async with asyncio.timeout(5):
                        while int((directory / "scans").read_text()) == scans:  # noqa: ASYNC110 - independent worker progress
                            assert child[0].returncode is None
                            await asyncio.sleep(0.02)
                stopped_scans = int((directory / "scans").read_text())
                duplicate = await start(core, tmp_path / "duplicate", duration=1)
                assert (await finish(duplicate, code=2))["watchers"] == 0
                assert child[0].returncode is None
                assert int((directory / "scans").read_text()) <= stopped_scans + 1
                assert core.store.rows("SELECT * FROM model_usage") == []
                assert core.store.rows("SELECT * FROM outbox") == []

                query = {"expected_revision": 2, "policy": policy.model_dump(mode="json")}
                preview = await api.post("/internal/v1/owner-authority/preview", json=query)
                assert preview.status_code == 200, preview.text
                response = await api.post("/internal/v1/owner-authority/activate", json={
                    **query, "operation_id": "independent-worker-resume",
                    "preview_sha256": preview.json()["preview_sha256"],
                })
                assert response.status_code == 200, response.text
                successor = response.json()["authority"]
                assert successor == {**template.authority, "revision": 3}
                capture, message = signed_input(
                    core, template.model_copy(update={"authority": successor}), key, 1,
                )
                bridge = {"Authorization": "Bearer "
                          + core.settings.bridge_token.get_secret_value()}
                approved = await api.post("/internal/v1/owner-authority/inputs/approve",
                                          json=capture.model_dump(mode="json"), headers=bridge)
                assert approved.status_code == 200, approved.text
                response = await api.post("/internal/v1/inbox/events",
                    json=message.model_dump(mode="json"),
                    headers={**bridge, "X-Idempotency-Key": "owner-worker-after-resume"})
                assert response.status_code == 202, response.text
                await state(core, response.json()["job_id"], seconds=5)
                assert child[0].returncode is None
                (directory / "release").touch()
                result = await finish(child)
                assert result["total"]["provider_calls"] == 1
                assert result["watchers"] == result["closed_watchers"] == 1
                assert core.store.one("SELECT state FROM model_usage") == {"state": "SETTLED"}
                assert core.store.one("SELECT state FROM outbox") == {"state": "PENDING"}
            finally:
                await close_child(child)

    run(scenario())
