"""Quarantined terminal outcomes retain the ref needed to release Core custody."""

import json

import httpx
import pytest

from companion.voice_protocol import VoiceLeaseRef, VoiceProtocolError
from companion_voice_node.state import NodeState
from companion_voice_node.worker import VoiceNodeWorker
from tests.conftest import run
from tests.support.voice_jobs import TOKEN, claim, create_tts, prepared
from tests.support.voice_node_api import app_for
from tests.support.voice_node_worker import node as node  # noqa: F401 -- pytest fixture


@pytest.mark.parametrize("phase", ["UNKNOWN", "FAILED", "RESULT_LOST"])
@pytest.mark.parametrize("loss", [None, "before", "after"])
def test_quarantine_recovery_replays_ref_until_core_accepts_ready(
    core, monkeypatch, tmp_path, node, phase, loss,
):
    jobs, message_id, _, _ = prepared(core, monkeypatch)
    job = create_tts(core, jobs, message_id)
    _, answer = claim(jobs)
    lease = answer.lease
    jobs.input("local", VoiceLeaseRef.from_lease(lease))
    config, _, _ = node
    # This case verifies custody and lost ACK replay, not a subsecond request deadline.
    config = config.model_copy(update={"request_timeout_seconds": 5})
    directory = tmp_path / "quarantined-node"
    controls = []

    class LostAck(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.ASGITransport(app=app_for(core))
            self.loss = loss

        async def handle_async_request(self, request):
            assert request.url.path.endswith("/stop")
            body = json.loads(await request.aread())
            controls.append(body)
            if body["resource_state"] == "READY" and self.loss == "before":
                self.loss = None
                raise httpx.ReadError("synthetic lost stopped-resource control")
            response = await self.inner.handle_async_request(request)
            if body["resource_state"] == "READY" and self.loss == "after":
                self.loss = None
                raise httpx.ReadError("synthetic lost stopped-resource acknowledgment")
            return response

    async def scenario():
        transport = LostAck()
        async with httpx.AsyncClient(transport=transport,
                headers={"Authorization": "Bearer " + TOKEN}) as client:
            worker = VoiceNodeWorker(config)
            with NodeState(directory) as state:
                state.accept_claim(lease)
                # Synthetic stopped process identity; no real provider/GPU is started.
                state.dispatch(lease, pid=12345, process_identity="synthetic-process")
                worker._startup(state)
                assert state.active_job()["phase"] == "UNKNOWN"
                state.phase(lease, phase)
                assert (await worker._cycle(client, state))[0] == "RETRY"
                assert state.active_job()["phase"] == phase
                assert state.resource()["state"] == "QUARANTINED"
                assert core.store.one("SELECT active_job_id FROM voice_node_state")[
                    "active_job_id"] == job["job_id"]

            # Restart must retain the original ref through the existing operator recovery.
            with NodeState(directory) as state:
                resource = state.resource()
                state.recover_manually(quarantine_id=resource["quarantine_id"],
                    operator="synthetic-operator", service_instance="synthetic-stopped-instance",
                    confirmation="inflight-execution-stopped")
                if loss:
                    with pytest.raises(VoiceProtocolError, match="VOICE_CORE_UNAVAILABLE"):
                        await worker._cycle(client, state)
                    assert transport.loss is None, "Expected synthetic ACK loss was not consumed"
                    assert state.active_job()["phase"] == phase
                    held = core.store.one("SELECT active_job_id FROM voice_node_state")[
                        "active_job_id"]
                    assert held == (job["job_id"] if loss == "before" else None)
                assert (await worker._cycle(client, state))[0] == "DISCARDED"
                assert state.active_job() is None
                assert core.store.one("SELECT active_job_id FROM voice_node_state")[
                    "active_job_id"] is None
                assert len({json.dumps(c["ref"], sort_keys=True) for c in controls}) == 1
                assert controls[0]["resource_state"] == "QUARANTINED"
                assert all(c["resource_state"] == "READY" for c in controls[1:])
                assert core.store.one("SELECT state FROM voice_jobs")["state"] == "UNKNOWN"
    run(scenario())
