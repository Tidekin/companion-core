import pytest

from companion_onebot_bridge.config import BridgeConfig


@pytest.fixture
def config(tmp_path):
    return BridgeConfig(owner_qq_id="12345", bot_qq_id="67890", bridge_token="b" * 32,
        onebot_token="o" * 32, protocol_epoch="epoch1234", data_dir=str(tmp_path),
        capture_signing_key="c" * 64, owner_authority={"version": 2,
            "grant_id": "grant_" + "a" * 32, "revision": 1, "policy_sha256": "d" * 64,
            "restore_generation": "initial"})
