"""Synthetic safety regressions; advancing this clock is not elapsed-day product evidence."""

from datetime import date

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from companion.affect import NEUTRAL
from companion.api import create_app
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from tests.conftest import ingest, run
from tests.postgres_fixture import logical_state
from tests.support.consolidation import enable_nightly
from tests.support.goals import setup_goal
from tests.support.growth_foundations import complete_read, view
from tests.support.observation_capture import enable_observations, observation


@pytest.mark.parametrize(
    "text,dimension,value",
    [
        ("更简短", "interaction_tone", "concise"),
        ("更详细", "interaction_tone", "detailed"),
        ("目前不想聊", "engagement", "pause"),
        ("欢迎继续", "engagement", "continue"),
    ],
)
def test_explicit_feedback_shadow_has_bounded_lifetime_without_enabling_expression(
    core, text, dimension, value
):
    ingest(core, 1, text)
    ingest(core, 2, "引用里说：“" + text + "”")
    before = logical_state(core.store)
    proposal = core.affect.preview("evt_test_1")
    assert proposal["mode"] == "SHADOW" and proposal["namespace"] == "owner.feedback"
    assert proposal["dimension"] == dimension and proposal["new_state"] == value
    assert proposal["event_id"] == "evt_test_1" and proposal["expires_at"]
    assert core.affect.preview("evt_test_2") is None
    assert core.affect.snapshot()["state"] == NEUTRAL
    assert logical_state(core.store) == before and not core.model.calls
    core.clock.advance(core.settings.affect_decay_minutes * 60)
    assert core.affect.preview("evt_test_1") is None


def test_same_content_hash_at_different_targets_does_not_add_independent_interest(core):
    for number in range(1, 4):
        goal = setup_goal(core, number, topic="天文研究", url=f"https://example.org/{number}")
        core.goals.accept(goal)
        step = core.goals.claim()
        item = observation(
            core, number, text="同一篇文章的镜像内容",
            source_event_ids=core.goals.source_ids(goal),
            payload={"text": "同一篇文章的镜像内容", "url": f"https://example.org/{number}"},
        )
        core.observations.append("reader", item)
        core.goals.finish(step, item.event_id)
        core.clock.advance(86400)
    interest = view(core)["self.interest"][0]
    assert interest["independent_sessions"] == interest["independent_days"] == 1
    assert interest["evidence"][0]["evidence_level"] == "SIMULATED_FETCHED"
    assert interest["evidence"][0]["source"] == "observation.simulation"


def test_retained_demo_results_do_not_become_live_personality_evidence(core):
    complete_read(core, 1, "https://example.org/1")
    assert view(core)["self.interest"][0]["evidence"][0]["evidence_level"] == "SIMULATED_FETCHED"
    core.settings.mode = "live"
    assert view(core)["self.interest"] == []
    assert not core.model.calls


@pytest.mark.parametrize("inspect_first", [True, False])
def test_reflection_transitive_expiry_prevents_read_or_decision_and_survives_restore(
    empty_database,
    core, tmp_path, inspect_first
):
    core.settings.raw_event_retention_days = 2
    core.settings.reflection_min_sources = 1
    ingest(core, 1, "一条较早的天文阅读来源")
    core.clock.advance(86400)
    enable_observations(core)
    core.observations.append(
        "reader", observation(core, source_event_ids=["evt_test_1"], text="来源的后续天文阅读结果")
    )
    enable_nightly(core, seed=False)
    core.clock.advance(86399)
    day = date(2026, 10, 20)
    assert run(core.reflections.run_day(day))["state"] == "COMMITTED"
    suggestion = core.reflections.recent()[0]["suggestion_id"]
    assert core.reflections.decide(suggestion, "ACCEPT_AS_HYPOTHESIS")
    key, archive = Fernet.generate_key(), tmp_path / "reflection.enc"
    restored_state = tmp_path / "restored-state"
    backup(core.settings.database, archive, key)
    # Only the upstream event expires; the derived result is still recent.
    core.clock.advance(2)
    if inspect_first:
        assert core.reflections.recent() == []
    with pytest.raises(CoreError, match="REFLECTION_NOT_FOUND"):
        core.reflections.decide(suggestion, "ACCEPT_AS_HYPOTHESIS")
    restore(archive, empty_database, key, core.settings.erasure_ledger,
            state_directory=restored_state)
    restored = Core(core.settings.model_copy(update={
        "database": empty_database, "state_directory": str(restored_state),
    }), clock=core.clock)
    try:
        assert restored.reflections.recent() == []
        assert restored.store.one("SELECT redacted FROM events WHERE event_id='obs_test_1'")[
            "redacted"
        ]
        assert restored.store.rows("SELECT * FROM reflection_evidence") == []
    finally:
        restored.close()


def test_owner_can_veto_reflection_without_mutating_facts_traits_or_permissions(core):
    core.settings.control_token = SecretStr("owner-control-" * 4)
    enable_nightly(core)
    assert run(core.reflections.run_day(date(2026, 10, 18)))["state"] == "COMMITTED"
    suggestion = core.reflections.recent()[0]["suggestion_id"]
    protected = ("memories", "traits", "consents", "goals", "outbox", "model_usage")
    before = {table: core.store.rows(f"SELECT * FROM {table}") for table in protected}
    with TestClient(create_app(core.settings)) as client:
        client.app.state.core.reflections.clock = core.clock
        path = f"/internal/v2/reflections/{suggestion}/decision"
        bridge = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
        owner = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
        assert client.post(path, headers=bridge, json={"decision": "REJECT"}).status_code == 401
        for decision in ("ACCEPT_AS_HYPOTHESIS", "NEED_MORE_EVIDENCE", "REJECT"):
            response = client.post(path, headers=owner, json={"decision": decision})
            assert response.status_code == 200 and response.json()["state"] == decision
    assert {table: core.store.rows(f"SELECT * FROM {table}") for table in protected} == before
    assert len(core.model.calls) == 1
