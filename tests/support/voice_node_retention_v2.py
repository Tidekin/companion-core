import asyncio
import json
import sys
from pathlib import Path

from companion.voice_protocol import SignedVoiceLease, digest
from companion_voice_node.state import NodeState
from companion_voice_node.worker import local_manifest
from tests.support.voice_node_worker import FakeCore, FixtureSupervisor


def scope(node):
    config, key, _ = node
    remote = FakeCore(config, key)
    lease = SignedVoiceLease.model_validate(remote.lease(digest(local_manifest(config))))
    return config, remote, lease


async def temp_crash_fixture(config_path, lease_path, counter, stage):
    from companion_voice_node.worker import VoiceNodeConfig

    config = VoiceNodeConfig.model_validate_json(
        await asyncio.to_thread(Path(config_path).read_bytes),
    )
    lease = SignedVoiceLease.model_validate_json(
        await asyncio.to_thread(Path(lease_path).read_bytes),
    )
    with NodeState(config.state_directory) as state:
        state.accept_claim(lease)
        temporary = state.prepare_temp(lease)
        temporary.mkdir(parents=True)
        (temporary / "private.txt").write_text("synthetic crash residue")
        child = None
        if stage != "prepared":
            child = FixtureSupervisor(config.workspace, temporary, counter=Path(counter))
            await child.start(5)
            if stage != "spawned":
                state.bind_process(lease, child.process.pid, child.identity)
            if stage == "stopped":
                assert await child.stop()
                state.process_stopped(lease, child.process.pid, child.identity)
        print(json.dumps({"stage": stage, "name": temporary.name,
                          "pid": child.process.pid if child else None,
                          "identity": child.identity if child else None}), flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__" and sys.argv[1:2] == ["--temp-crash"]:
    asyncio.run(temp_crash_fixture(*sys.argv[2:]))
