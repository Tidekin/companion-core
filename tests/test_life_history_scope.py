"""Life uses the native signed history contract and retains bounded topic snapshots."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from companion import owner_authority
from companion.contracts import CoreError
from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion.life_topics import _source, authority
from companion.owner_authority_protocol import AuthorityStop, OwnerGrantPolicy
from tests.conftest import run
from tests.support.owner_authority import activate
from tests.support.owner_memory import environment
from tests.test_life_worker_integration import process_worker


def scope(core, conversation, ids=(), *, mode="owner_private_history"):
    return LifeShadowApproval(scope="owner_private_life_shadow_v1",
        authorization_sha256="e" * 64, conversation_id=conversation,
        source_scope=mode, source_event_ids=list(ids), purposes=["PRIVATE_MESSAGE_DRAFT"],
        expires_at=core.clock.now + timedelta(days=2), max_items=2)


def grant(core, reference, value, operation, *, history_bounds=None):
    view = owner_authority.current(core.store, core.settings, core.clock)
    stopped = owner_authority.stop(core.store, core.settings, core.clock,
        AuthorityStop(operation_id=operation+"-stop", authority=reference))
    policy = OwnerGrantPolicy.model_validate({**view.policy.model_dump(),
        "purposes": list(dict.fromkeys([*view.policy.purposes, "life"])),
        "history": {**view.policy.history.model_dump(), **(history_bounds or {})},
        "life_shadow": value})
    result, _ = activate(core, policy, revision=stopped["authority"]["revision"],
                         operation=operation+"-activate")
    return result["authority"], policy


def configure(core):
    core.settings.life_loop = LifeLoopConfig(mode="SHADOW", follow_up_delay_seconds=60,
        min_interval_seconds=60, autonomy_enabled=True, read_goals_enabled=True)
    core.settings.wake_enabled = True
    core.settings.memory_enabled = core.settings.diary_enabled = False
    core.settings.proactive_idle_seconds = 0
    core.settings.quiet_start = core.settings.quiet_end = 0


def test_existing_selected_approval_serialization_and_private_history_shape(core):
    selected = scope(core, "private-conversation", ["real-source"], mode="selected")
    assert "source_scope" not in selected.model_dump(mode="json")
    history = scope(core, "private-conversation")
    assert history.source_event_ids == [] and not history.qzone_draft_sources
    for changes in ({"source_event_ids": ["real-source"]}, {"source_scope": "unknown"}):
        with pytest.raises(ValidationError):
            LifeShadowApproval.model_validate({**history.model_dump(), **changes})
    with pytest.raises(ValidationError):
        LifeShadowApproval.model_validate({**selected.model_dump(), "source_event_ids": []})
    with pytest.raises(ValidationError):
        LifeShadowApproval.model_validate({**history.model_dump(), "qzone_draft_sources": [{
            "grant_id": "qzone-source", "revision": 1, "purpose": "qzone_draft",
            "event_id": "qzone-event", "source_sha256": "a" * 64,
            "expires_at": history.expires_at, "audience": "owner_only",
            "owner_qq_id": core.settings.owner_qq_id,
        }]})


def test_life_history_requires_independent_current_history_permission(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path, history=False):
            old = owner_authority.current(core.store, core.settings, core.clock).policy
            with pytest.raises(ValidationError, match="private history approval"):
                OwnerGrantPolicy.model_validate({**old.model_dump(),
                    "purposes": [*old.purposes, "life"],
                    "life_shadow": scope(core, "private-conversation")})
    run(scenario())


@pytest.mark.parametrize(("input_count", "history_bounds", "active_count"), [
    (45, {}, 8), (12, {"max_turns": 2}, 2), (12, {"max_utf8_bytes": 100}, 1),
])
def test_capped_selected_topic_upgrades_to_real_history_and_independent_worker(
    core, tmp_path, input_count, history_bounds, active_count,
):
    async def scenario():
        async with environment(core, tmp_path, additional_purposes=("proactive",)) as (
            admit, _, reference
        ):
            configure(core)
            inputs = []
            input_authority = reference
            for number in range(1, 9):
                item, _ = await admit(number, f"第{number}条实际私聊：导出问题还有新线索。")
                inputs.append(item.event_id)
            conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                          (inputs[-1],))["conversation_id"]
            reference, _ = grant(core, reference,
                scope(core, conversation, inputs[:8], mode="selected"), "selected-life",
                history_bounds=history_bounds)
            core.life_topics.synchronize()
            original = core.store.one("SELECT * FROM life_topics")
            input_authority.update(reference)
            for number in range(9, input_count + 1):
                item, _ = await admit(number, f"第{number}条实际私聊：导出问题还有新线索。")
                inputs.append(item.event_id)
            core.clock.advance(61)
            core.life_topics.synchronize()
            with pytest.raises(CoreError, match="REPLY_SUPERSEDED"):
                core.life_topics.material(original["topic_id"])
            reference, policy = grant(core, reference, scope(core, conversation), "history-life")
            assert policy.history.scope == "owner_private"
            core.life_topics.synchronize()
            old = core.store.one("SELECT * FROM life_topics WHERE topic_id=?",
                                 (original["topic_id"],))
            assert (old["state"], old["reason_code"]) == ("CLOSED", "SOURCE_LIMIT_REACHED")
            active = core.store.one("SELECT * FROM life_topics WHERE state IN ('WAITING','DUE')")
            assert {s["event_id"] for s in core.life_topics.sources(active["topic_id"])} == set(
                inputs[-active_count:])
            assert core.store.one("SELECT COUNT(*) n FROM events WHERE source='qq.onebot'")[
                "n"] == input_count
            core.clock.advance(61)
            core.life_topics.synchronize()
            assert core.store.one("SELECT topic_id FROM life_topics WHERE state='DUE'")[
                "topic_id"] == active["topic_id"]
            material = core.life_topics.material(active["topic_id"])
            assert material["read_goals_enabled"] and "autonomy_guidance" in (
                material["decision_context"])
            observed = await process_worker(core, tmp_path, "bug", inputs[-1])
            assert observed["provider_calls"] == 1
            assert core.store.one("SELECT state FROM life_activity_results")["state"] == "SHADOW"
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
            assert not core.store.rows("SELECT * FROM outbox")
            assert not core.store.rows("SELECT * FROM social_autonomy_plans")
    run(scenario())


@pytest.mark.parametrize("mutation", ["text", "not_before", "conversation", "recall"])
def test_history_sources_retain_native_identity_scope_and_recall(core, tmp_path, mutation):
    async def scenario():
        async with environment(core, tmp_path, additional_purposes=("proactive",)) as (
            admit, recall, reference
        ):
            configure(core)
            event, _ = await admit(1, "真实私人来源，稍后再聊。")
            conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                          (event.event_id,))["conversation_id"]
            reference, policy = grant(core, reference, scope(core, conversation), "private-life")
            source = _source(core.store, core.settings, event.event_id, core.clock())
            assert authority(core.store, core.settings, conversation, [source], core.clock())[
                "history_sources"]
            if mutation == "text":
                core.store.execute("UPDATE events SET body_json=json_set(body_json,'$.text',"
                                   "'changed without signed capture') WHERE event_id=?",
                                   (event.event_id,))
                source = _source(core.store, core.settings, event.event_id, core.clock())
            elif mutation == "not_before":
                changed = OwnerGrantPolicy.model_validate({**policy.model_dump(), "history": {
                    **policy.history.model_dump(), "not_before": core.clock()}})
                stopped = owner_authority.stop(core.store, core.settings, core.clock,
                    AuthorityStop(operation_id="narrow-history-stop", authority=reference))
                activate(core, changed, revision=stopped["authority"]["revision"],
                         operation="narrow-history-activate")
            elif mutation == "conversation":
                conversation = "different-private-conversation"
            else:
                response = await recall(event)
                assert response.status_code == 200
            with pytest.raises(CoreError):
                authority(core.store, core.settings, conversation, [source], core.clock())
            assert not core.store.rows("SELECT * FROM model_usage")
            assert not core.store.rows("SELECT * FROM outbox")
    run(scenario())
