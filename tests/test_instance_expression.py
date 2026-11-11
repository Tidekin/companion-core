"""One codebase, independently selected expression and source-backed characters."""

import json

import pytest
from pydantic import ValidationError

from companion import history_import
from companion.config import Settings
from companion.contracts import CoreError
from companion.conversation_policy import CHARACTER_V1, PARTICIPATION_V2, ConversationPolicyConfig
from companion.model import NATURAL_EXPRESSION
from tests.conftest import ingest
from tests.test_history_import import package

COMMON = "不推断健康、身份等敏感属性，遵守主人提出的边界。"


def context(core, number=1, text="鱼鱼，🍚"):
    ingest(core, number, text)
    job = core.store.one("SELECT j.*,e.* FROM jobs j JOIN events e USING(event_id) "
                         "WHERE j.event_id=%s", (f"evt_test_{number}",))
    snapshots = []
    messages = core.context(job, memories=[], snapshots=snapshots)
    return job, messages, snapshots


def test_two_instances_keep_their_own_expression_and_character(core_factory):
    with core_factory() as ximi, core_factory() as natural:
        ximi.settings.conversation_policy = ConversationPolicyConfig(strategy="character_v1")
        natural.settings.conversation_policy = ConversationPolicyConfig()
        history_import.set_character(ximi, package()[0].character)
        job, role_messages, snapshots = context(ximi)
        _, natural_messages, _ = context(natural)
        role_rules, natural_rules = role_messages[0]["content"], natural_messages[0]["content"]
        assert CHARACTER_V1 in role_rules and NATURAL_EXPRESSION not in role_rules
        assert PARTICIPATION_V2 in natural_rules and NATURAL_EXPRESSION in natural_rules
        assert CHARACTER_V1 not in natural_rules
        assert COMMON in role_rules and COMMON in natural_rules
        assert "汐米" in str(role_messages) and "汐米" not in str(natural_messages)
        assert ximi.context(job, memories=[]) == role_messages
        ximi.settings.conversation_policy = ConversationPolicyConfig()
        with pytest.raises(CoreError):
            ximi._check_context_snapshots(job, snapshots)
        assert natural.settings.conversation_policy == ConversationPolicyConfig()


@pytest.mark.parametrize("mode", ["OFF", "SHADOW"])
def test_character_strategy_off_or_shadow_keeps_default_base_prompt(core, mode):
    core.settings.conversation_policy = ConversationPolicyConfig(mode=mode, strategy="character_v1")
    _, messages, _ = context(core)
    assert NATURAL_EXPRESSION in messages[0]["content"]
    assert CHARACTER_V1 not in messages[0]["content"]
    assert "汐米" not in str(messages)


def test_instance_expression_loads_from_toml_and_environment(monkeypatch, tmp_path):
    path = tmp_path / "instance.toml"
    path.write_text('mode="demo"\nowner_qq_id="10001"\nbot_qq_id="10002"\n'
                    'bridge_token="synthetic-instance-token-long-enough"\n'
                    '[conversation_policy]\nmode="ENABLED"\nstrategy="character_v1"\n',
                    encoding="utf-8")
    monkeypatch.setenv("COMPANION_CONFIG", str(path))
    monkeypatch.setenv("COMPANION_DATABASE", str(tmp_path / "not-created.db"))
    monkeypatch.delenv("COMPANION_CONVERSATION_POLICY", raising=False)
    selected = Settings.load(apply_operator_config=False)
    assert selected.conversation_policy.strategy == "character_v1"
    assert Settings.model_validate(selected.model_dump()).conversation_policy == (
        selected.conversation_policy)
    monkeypatch.setenv("COMPANION_CONVERSATION_POLICY", json.dumps({
        "mode": "ENABLED", "strategy": "participation_v2"}))
    assert Settings.load(apply_operator_config=False).conversation_policy == (
        ConversationPolicyConfig())
    monkeypatch.setenv("COMPANION_CONVERSATION_POLICY", json.dumps({
        "mode": "ENABLED", "strategy": "character_v1", "system_prompt": "extra instructions"}))
    with pytest.raises(ValidationError):
        Settings.load(apply_operator_config=False)
