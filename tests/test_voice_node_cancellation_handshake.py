"""Pre-dispatch cancellation retains custody until Core accepts stopped-resource control."""

import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest

from companion.core import Core
from companion.model import ModelClient
from companion.retrieval import SemanticIndex
from companion.store import encode
from companion.voice_config import VoiceNode, VoiceReplyPolicy
from companion.voice_protocol import (
    VoiceControlRequest,
    VoiceProtocolError,
    VoiceStopRequest,
    digest,
)
from companion_voice_node.state import NodeState
from companion_voice_node.supervisor import ProcessSupervisor, process_has_exited
from companion_voice_node.worker import VoiceNodeWorker, local_manifest
from tests.conftest import run
from tests.support.voice_jobs import voice_config
from tests.support.voice_node_api import independent_node, tls_core
from tests.support.voice_node_worker import FakeCore, FixtureSupervisor, child_receipts
from tests.support.voice_node_worker import node as node  # noqa: F401 -- pytest fixture


class CancelCore(FakeCore):
    def __init__(self, config, key, *, claimed, loss=None):
        super().__init__(config, key)
        self.cancel_at, self.claimed, self.loss = claimed, 0, loss
        self.terminal_controls, self.children = [], []
        self.allow_running = False
        self.terminal_continue = False

    def __call__(self, request):
        if request.url.path.endswith("/control"):
            body = VoiceControlRequest.model_validate_json(request.content)
            if body.phase == "CLAIMED":
                self.claimed += 1
                if self.claimed == self.cancel_at:
                    self.controls.append(body)
                    return httpx.Response(200, json={"action": "CANCEL"})
        if request.url.path.endswith("/stop"):
            body = VoiceStopRequest.model_validate_json(request.content)
            self.terminal_controls.append(body)
            for child in self.children:
                assert process_has_exited(child.process.pid, child.identity) or (
                    self.allow_running and body.resource_state == "QUARANTINED")
            if self.terminal_continue:
                return httpx.Response(200, json={"action": "CONTINUE"})
            if self.loss == "before":
                self.loss = None
                raise httpx.ReadError("synthetic lost stop request")
            response = super().__call__(request)
            if self.loss == "after":
                self.loss = None
                raise httpx.ReadError("synthetic lost stop acknowledgment")
            return response
        return super().__call__(request)

    def supervisor(self, workspace, temporary, *, counter):
        child = FixtureSupervisor(workspace, temporary, counter=counter)
        self.children.append(child)
        return child


@pytest.mark.parametrize("claimed", [1, 2])
@pytest.mark.parametrize("loss", [None, "before", "after"])
def test_cancel_control_waits_for_exit_and_replays_after_ack_loss(node, claimed, loss):
    config, key, counter = node
    remote = CancelCore(config, key, claimed=claimed, loss=loss)

    def worker():
        return VoiceNodeWorker(config, transport=httpx.MockTransport(remote),
            supervisor_factory=lambda workspace, temporary: remote.supervisor(
                workspace, temporary, counter=counter))

    async def scenario():
        if loss:
            with pytest.raises(VoiceProtocolError, match="VOICE_CORE_UNAVAILABLE"):
                await worker().run(once=True)
            with NodeState(config.state_directory) as state:
                assert state.active_job()["phase"] == "CANCELLED"
                assert state.resource()["state"] == "READY"
            assert await worker().run(once=True) == "DISCARDED"
        else:
            assert await worker().run(once=True) == "DISCARDED"
        assert len(remote.terminal_controls) == (2 if loss else 1)
        assert all(row.resource_state == "READY" for row in remote.terminal_controls)
        assert len({row.ref.model_dump_json() for row in remote.terminal_controls}) == 1
        assert remote.claimed == claimed
        assert len(remote.children) == claimed - 1
        assert child_receipts(counter) == [] and remote.result_bodies == []
        assert sum(request.url.path.endswith("/input")
                   for request in remote.requests) == claimed - 1
        with NodeState(config.state_directory) as state:
            assert state.active_job() is None
            assert state.resource()["state"] == "READY"

    run(scenario())


def test_cancel_control_continue_is_not_a_terminal_acknowledgment(node):
    config, key, counter = node
    remote = CancelCore(config, key, claimed=1)
    remote.terminal_continue = True

    async def scenario():
        worker = VoiceNodeWorker(config, transport=httpx.MockTransport(remote),
            supervisor_factory=lambda *_: pytest.fail("cancelled claim cannot start a child"))
        with pytest.raises(VoiceProtocolError, match="VOICE_CORE_RESPONSE_INVALID"):
            await worker.run(once=True)
        with NodeState(config.state_directory) as state:
            assert state.active_job()["phase"] == "CANCELLED"
        remote.terminal_continue = False
        assert await worker.run(once=True) == "DISCARDED"
        assert child_receipts(counter) == []

    run(scenario())


def test_unconfirmed_child_stop_cannot_release_slot_until_operator_confirms_exit(node):
    config, key, counter = node
    remote = CancelCore(config, key, claimed=2)
    remote.allow_running = True

    class UnconfirmedStop(FixtureSupervisor):
        async def stop(self):
            return False

    def factory(workspace, temporary):
        child = UnconfirmedStop(workspace, temporary, counter=counter)
        remote.children.append(child)
        return child

    async def scenario():
        worker = VoiceNodeWorker(config, transport=httpx.MockTransport(remote),
                                 supervisor_factory=factory)
        try:
            assert await worker.run(once=True) == "RETRY"
            with NodeState(config.state_directory) as state:
                assert state.active_job()["phase"] == "CANCELLED"
                resource = state.resource()
                assert resource["state"] == "QUARANTINED"
            assert len(remote.terminal_controls) == 1
            assert remote.terminal_controls[0].resource_state == "QUARANTINED"
            assert remote.children[0].process.returncode is None
            assert child_receipts(counter) == []
            assert await ProcessSupervisor.stop(remote.children[0])
            with NodeState(config.state_directory) as state:
                state.recover_manually(quarantine_id=resource["quarantine_id"],
                    operator="synthetic-operator", service_instance="synthetic-process",
                    confirmation="inflight-execution-stopped")
            assert await worker.run(once=True) == "DISCARDED"
            assert remote.terminal_controls[-1].resource_state == "READY"
            assert len(remote.children) == 1 and child_receipts(counter) == []
        finally:
            for child in remote.children:
                await ProcessSupervisor.stop(child)

    run(scenario())


async def launch_node(config, certificate, counter, tmp_path, timestamp, *, monotonic_start,
                      gate, loss):
    config_file = tmp_path / "cancel-node-config.json"
    config_file.write_text(encode(config.model_dump(mode="json") | {
        "credential": config.credential.get_secret_value()}), encoding="utf-8")
    return await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
        "tests.support.voice_node_cancellation_handshake", "--cancel-node", str(config_file),
        str(certificate), str(counter), "gate" if gate else "resume", loss or "none",
        str(timestamp), str(monotonic_start),
        cwd=str(config.workspace), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


async def node_finished(process):
    stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    value = json.loads(stdout)
    assert value["stage"] == "finished" and value["pid"] != os.getpid()
    return value["outcome"]


@asynccontextmanager
async def advancing_clock(core):
    initial, start = core.clock.now, time.monotonic()
    stop = asyncio.Event()

    async def advance():
        while not stop.is_set():
            core.clock.now = initial + timedelta(seconds=time.monotonic() - start)
            await asyncio.sleep(0.01)

    task = asyncio.create_task(advance())
    try:
        yield initial.timestamp(), start
    finally:
        stop.set()
        await task


@pytest.mark.parametrize("loss", [None, "before", "after"])
def test_real_tls_cancel_frees_old_slot_after_new_owner_input_and_next_job_runs(
    core, node, tmp_path, monkeypatch, loss,
):
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    config, signing_key, counter = node
    config = config.model_copy(update={"node_id": "local", "request_timeout_seconds": 10.0,
                                       "execution_timeout_seconds": 20.0})
    voice = voice_config(signing_private_key=signing_key.private_bytes_raw().hex(),
        reply=VoiceReplyPolicy(mode="auto"), nodes={"local": VoiceNode(token=config.credential,
            manifest_sha256=digest(local_manifest(config)))})
    from tests.voice_owner_fixtures import capture_voice_event, native_voice

    capture, capture_key, _ = native_voice(core, tmp_path, record=False, config=voice)
    calls = []

    def model(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "合成语音回复。"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    async def scenario():
        processes = []
        async with advancing_clock(core) as (timestamp, monotonic_start), httpx.AsyncClient(
            transport=httpx.MockTransport(model)) as external:
            with Core(core.settings, ModelClient(core.settings, external), core.clock) as worker:
                assert worker.store is not core.store
                assert await worker.run_once("first-native-voice-chat")
                with worker.store.transaction():
                    reply = worker.store.one(
                        "SELECT message_id FROM outbox ORDER BY rowid DESC LIMIT 1")
                    worker.voice.prepare_reply(reply["message_id"])
                first = core.store.one("SELECT * FROM voice_jobs")
                assert first["state"] == "QUEUED"
                async with tls_core(core, tmp_path) as (origin, certificate):
                    node_config = config.model_copy(update={"core_origin": origin})
                    process = await launch_node(node_config, certificate, counter, tmp_path,
                        timestamp, monotonic_start=monotonic_start, gate=True, loss=loss)
                    processes.append(process)
                    try:
                        line = await asyncio.wait_for(process.stdout.readline(), 15)
                        assert line, (await process.stderr.read()).decode()
                        started = json.loads(line)
                        assert started["stage"] == "child-ready"
                        assert started["node_pid"] != os.getpid()
                        assert not process_has_exited(started["pid"], started["identity"])
                        assert child_receipts(counter) == []
                        assert core.store.one("SELECT state FROM voice_jobs")["state"] == (
                            "RUNNING")
                        capture_voice_event(core, capture_key, capture.authority,
                                            number=124, record=False)
                        assert await worker.run_once("second-native-voice-chat")
                        with worker.store.transaction():
                            reply = worker.store.one(
                                "SELECT message_id FROM outbox ORDER BY rowid DESC LIMIT 1")
                            worker.voice.prepare_reply(reply["message_id"])
                        following = core.store.one("SELECT * FROM voice_jobs WHERE job_id<>?",
                                                   (first["job_id"],))
                        assert following["state"] == "QUEUED"
                        assert core.store.one("SELECT active_job_id FROM voice_node_state")[
                            "active_job_id"] == first["job_id"]
                        process.stdin.write(b"continue\n")
                        await process.stdin.drain()
                        outcome = await node_finished(process)
                        assert outcome == ("VOICE_CORE_UNAVAILABLE" if loss else "DISCARDED")
                        assert process_has_exited(started["pid"], started["identity"])
                        assert child_receipts(counter) == []
                        if loss:
                            with NodeState(config.state_directory) as state:
                                assert state.active_job()["phase"] == "CANCELLED"
                                assert state.resource()["state"] == "READY"
                            # A distinct node process replays the terminal handshake;
                            # it cannot consume the next queued job in the same cycle.
                            resumed = await launch_node(node_config, certificate, counter,
                                tmp_path, timestamp, monotonic_start=monotonic_start,
                                gate=False, loss=None)
                            processes.append(resumed)
                            assert await node_finished(resumed) == "DISCARDED"
                        assert core.store.one("SELECT active_job_id FROM voice_node_state")[
                            "active_job_id"] is None
                        retained = core.store.one("SELECT state,started_at FROM voice_jobs "
                                                  "WHERE job_id=?", (first["job_id"],))
                        assert retained["state"] == "UNKNOWN" and retained["started_at"]
                        await independent_node(node_config, certificate, counter, tmp_path,
                                               timestamp, monotonic_start=monotonic_start)
                    finally:
                        for current in processes:
                            if current.returncode is None:
                                current.kill()
                                await current.wait()
                assert core.store.one("SELECT state FROM voice_jobs WHERE job_id=?",
                                      (following["job_id"],))["state"] == "READY"
                assert len(child_receipts(counter)) == 1 and len(calls) == 2
                assert core.store.one("SELECT COUNT(*) n FROM runtime_meta "
                    "WHERE key LIKE 'owner_input:%'")["n"] == 2
                assert core.store.one("SELECT active_job_id FROM voice_node_state")[
                    "active_job_id"] is None

    run(scenario())
