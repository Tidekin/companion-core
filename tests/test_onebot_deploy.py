import json
import tomllib
from pathlib import Path

import yaml

from companion.config import Settings
from companion_onebot_bridge.config import BridgeConfig


def test_demo_template_has_no_paid_or_autonomous_capability():
    data = tomllib.loads(Path("deploy/qq-beta-core.example.toml").read_text("utf-8"))
    settings = Settings(
        owner_qq_id="10001",
        bot_qq_id="10002",
        bridge_token="synthetic-core-token-32-characters",
        **data,
    )
    assert settings.mode == "demo"
    assert not settings.model_key.get_secret_value()
    assert settings.paid_model is None
    assert settings.input_rate == settings.cached_input_rate == settings.output_rate == 0
    assert all(not value for name, value in data.items() if name.endswith("_enabled"))
    assert data["executive"]["enabled"] is data["executive"]["allow_changes"] is False
    assert data["media"]["enabled"] is False


def test_dry_run_networks_and_volumes_keep_protocol_separate():
    compose = yaml.safe_load(Path("deploy/compose.onebot-direct.yaml").read_text("utf-8"))
    services = compose["services"]
    assert set(services) == {"onebot-bridge", "snowluma"}
    assert "ports" not in services["onebot-bridge"]
    snow = services["snowluma"]
    assert snow["ports"] == ["127.0.0.1:6081:6081", "127.0.0.1:5099:5099"]
    assert snow["cap_add"] == ["SYS_PTRACE"]
    assert "companion" not in snow["networks"]
    assert len(snow["volumes"]) == 3
    assert all("core-data" not in mount and "erasure" not in mount for mount in snow["volumes"])
    account = json.loads(Path("deploy/snowluma-onebot.example.json").read_text("utf-8"))
    assert account["statusCommand"]["enabled"] is False
    assert account["networks"]["wsClients"] == []
    for item in account["networks"]["httpServers"] + account["networks"]["wsServers"]:
        assert item["reportSelfMessage"] is False
        assert item["messageFormat"] == "array"
    bridge = BridgeConfig(
        **tomllib.loads(Path("deploy/onebot-bridge.example.toml").read_text("utf-8"))
    )
    assert bridge.delivery_enabled is False


def test_production_and_live_gates_stay_closed():
    gates = json.loads(Path("docs/qq-beta/p5-gates.json").read_text("utf-8"))
    assert gates["production_ready"] is gates["snowluma_actual_onebot_compat_verified"] is False
    assert gates["real_qq_private_scenarios_accepted"] == 0
    assert gates["grant_revocation_dependency_closure"] == "FAIL"
    assert gates["latency_real_p50_ms"] is gates["latency_real_p95_ms"] is None
    assert all(
        gates["evidence"][kind] == []
        for kind in ("LIVE_ONEBOT", "LIVE_QQ_HUMAN", "PAID_MODEL", "WINDOWS_DEVICE")
    )
