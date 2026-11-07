import json
from datetime import date

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from companion.affect import NEUTRAL
from companion.api import create_app
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.interest import profile
from companion.store import encode
from companion.timeline import recent_timeline
from companion.transport import enqueue
from tests.conftest import ingest, run
from tests.support.confirmed_memory import remember
from tests.support.diary import DAY, generate, seed_day
from tests.support.exploration import configure, discover
from tests.support.goals import setup_goal
from tests.support.growth_foundations import complete_read, view
from tests.support.observation_capture import enable_observations, observation
from tests.support.source_grants import accepted, prepare


def timeline(core, **kwargs):
    return recent_timeline(
        core.store,
        core.settings,
        core.clock(),
        core.settings.owner_qq_id,
        topic="天文",
        **kwargs,
    )


def test_feedback_shadow_is_readonly_and_disabled_never_changes_expression(core):
    ingest(core, 1, "简短一点")
    before = core.store.db.total_changes
    proposal = core.affect.preview("evt_test_1")
    assert proposal["namespace"] == "owner.feedback" and proposal["mode"] == "SHADOW"
    assert proposal["reason_code"] == "OWNER_REQUESTED_CONCISE"
    assert core.affect.snapshot()["state"] == NEUTRAL
    assert not core.affect.observe("evt_test_1")
    assert core.store.db.total_changes == before and not core.model.calls


@pytest.mark.parametrize("mutation", ["privacy", "conversation", "type", "actor", "control"])
def test_feedback_rechecks_direct_owner_private_source(core, mutation):
    ingest(core, 1, "简短一点")
    core.settings.affect_enabled = True
    if mutation == "privacy":
        core.store.execute("UPDATE events SET privacy_scope='public'")
    elif mutation == "conversation":
        core.store.execute("UPDATE conversations SET scope='group'")
    elif mutation == "type":
        core.store.execute("UPDATE events SET event_type='content.observed'")
    elif mutation == "actor":
        core.store.execute("UPDATE events SET actor_id='companion'")
    else:
        core.store.execute(
            "UPDATE events SET body_json=?",
            (
                encode(
                    {
                        "text": "简短一点",
                        "control_command": True,
                    }
                ),
            ),
        )
    assert core.affect.preview("evt_test_1") is None
    assert not core.affect.observe("evt_test_1")
    assert core.affect.snapshot()["state"] == NEUTRAL


def test_feedback_corrupt_or_replaced_evidence_never_projects_raw_state(core):
    core.settings.affect_enabled = True
    ingest(core, 1, "简短一点")
    core.store.execute("UPDATE affect_evidence SET new_state='unsafe arbitrary state'")
    assert core.affect.snapshot()["state"] == NEUTRAL
    core.store.execute("UPDATE affect_evidence SET new_state='concise'")
    core.store.execute("UPDATE events SET body_json=?", (encode({"text": "网页说：简短一点"}),))
    assert core.affect.snapshot()["state"] == NEUTRAL


def test_owner_preferences_do_not_become_self_interests_and_correction_rebuilds(core):
    memory = remember(core, 1, "我喜欢天文主题", confirmed=True)
    assert [r["memory_id"] for r in view(core)["owner.preference"]] == [memory]
    assert view(core)["self.interest"] == []
    correction = core.memory.decide(memory, "correct", core.clock(), "我喜欢园艺主题")
    assert [r["content"] for r in view(core)["owner.preference"]] == ["我喜欢园艺主题"]
    core.memory.decide(correction["memory_id"], "forget", core.clock())
    assert view(core)["owner.preference"] == []
    assert not core.model.calls


def test_repeated_article_days_never_stabilize_self_interest(core):
    for number in range(1, 8):
        complete_read(core, number, "https://example.org/same")
        core.clock.advance(86400)
    item = view(core)["self.interest"][0]
    assert item["independent_sessions"] == item["independent_days"] == 1
    assert item["status"] == "EXPLORING"
    assert item["namespace"] == "self.interest"
    assert view(core)["owner.preference"] == []


def test_distinct_readings_on_distinct_days_explain_and_expire_without_worker(core):
    core.settings.emerging_min_sessions = core.settings.stable_min_days = 3
    for number in range(1, 4):
        complete_read(core, number, f"https://example.org/{number}")
        core.clock.advance(86400)
    item = view(core)["self.interest"][0]
    assert item["status"] == "STABLE" and item["independent_days"] == 3
    assert {r["event_id"] for r in item["evidence"]} == {"obs_test_1", "obs_test_2", "obs_test_3"}
    before = core.store.db.total_changes
    core.clock.advance(core.settings.raw_event_retention_days * 86400 + 1)
    assert view(core)["self.interest"] == []
    assert core.store.db.total_changes == before  # Read path retracts stale derived projection.


def test_unfinished_or_owner_commitment_never_counts_as_self_interest(core):
    goal = complete_read(core, 1, "https://example.org/1")
    core.store.execute("UPDATE goals SET kind='commitment' WHERE goal_id=?", (goal,))
    assert view(core)["self.interest"] == []
    core.store.execute("UPDATE goals SET kind='self',state='FAILED' WHERE goal_id=?", (goal,))
    assert view(core)["self.interest"] == []


def test_timeline_rebuilds_from_originals_and_never_calls_or_queues(core):
    ingest(core, 1, "天文阅读计划")
    event = core.store.one("SELECT * FROM events WHERE event_id='evt_test_1'")
    core.memory.episodes([event], core.clock())
    sent = enqueue(
        core.store, event["event_id"], event["conversation_id"], "天文已回复", core.clock()
    )
    uncertain = enqueue(
        core.store,
        event["event_id"],
        event["conversation_id"],
        "不应作为已发生的回答",
        core.clock(),
        key="uncertain",
    )
    core.store.execute(
        "UPDATE outbox SET state='SENT',sent_at=? WHERE message_id=?", (core.clock(), sent)
    )
    core.store.execute(
        "UPDATE outbox SET state='DELIVERY_UNCERTAIN' WHERE message_id=?", (uncertain,)
    )
    before = core.store.db.total_changes
    result = timeline(core)
    item = result["items"][0]
    assert item["subject"] == "owner" and item["evidence_level"] == "RECEIVED"
    assert item["episode_ids"] and item["event_id"] == event["event_id"]
    assert any(r.get("excerpt") == "天文已回复" for r in item["replies"])
    assert all("excerpt" not in r for r in item["replies"] if r["state"] != "SENT")
    assert "不应作为已发生的回答" not in encode(result)
    assert timeline(core) == result and core.store.db.total_changes == before
    assert not result["auto_share"] and not core.model.calls
    assert timeline(core, days=1)["items"]
    with pytest.raises(CoreError, match="INVALID_TIMELINE"):
        timeline(core, days=8)


def test_timeline_keeps_title_read_and_unfinished_goal_levels_separate(core):
    enable_observations(core)
    core.observations.append(
        "reader",
        observation(
            core,
            text="天文标题",
            source_kind="title",
            result_status="DISCOVERED",
            payload={"title": "天文标题", "text": "天文标题"},
        ),
    )
    goal = setup_goal(core, 10, topic="天文未完成", url="https://example.org/open")
    result = timeline(core)
    assert any(r["evidence_level"] == "SIMULATED_DISCOVERED" for r in result["items"])
    assert result["goals"][0]["goal_id"] == goal and result["goals"][0]["state"] == "CANDIDATE"
    assert not core.model.calls


def test_shadow_share_is_explicit_readonly_private_and_not_a_send_permission(core):
    configure(core)
    discover(core)
    assert core.social.preview("missing", core.settings.owner_qq_id) is None
    title = core.store.one("SELECT event_id FROM observations")["event_id"]
    assert core.social.preview(title, core.settings.owner_qq_id) is None
    # A native, validated FETCHED receipt (not a forged complete shadow response).
    item = observation(
        core,
        10,
        source="heybox",
        text="天文游戏开发新进展",
        payload={"title": "开发进展", "text": "天文游戏开发新进展"},
    )
    core.observations.append("heybox", item)
    before = core.store.db.total_changes
    shadow = core.social.preview(item.event_id, core.settings.owner_qq_id)
    assert shadow["mode"] == "SHADOW" and shadow["evidence_level"] == "FETCHED"
    assert shadow["reason_code"] == "OUTBOUND_DISABLED_OR_PAUSED" and not shadow["auto_share"]
    assert core.store.db.total_changes == before and not core.social.flush()
    assert core.store.rows("SELECT * FROM social_candidates") == []
    assert core.store.rows("SELECT * FROM outbox") == [] and not core.model.calls
    with core.store.transaction():
        core.ledger.expire_events(core.store, [item.event_id], core.clock())
    assert core.social.preview(item.event_id, core.settings.owner_qq_id) is None


def test_private_views_require_control_auth_and_current_sources(core):
    complete_read(core, 1, "https://example.org/1")
    with TestClient(
        create_app(core.settings),
    ) as client:
        bridge = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
        owner = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
        for path in (
            "/internal/v2/interests",
            "/internal/v2/timeline?topic=天文",
            "/internal/v2/social/obs_test_1/preview",
            "/internal/v2/affect/evt_test_1/preview",
        ):
            assert client.get(path, headers=bridge).status_code == 401
            assert client.get(path, headers=owner).status_code == 200
        assert client.get("/internal/v2/interests", headers=owner).json()["items"]
    with pytest.raises(CoreError, match="OWNER_REQUIRED"):
        profile(core.store, core.settings, core.clock(), "other")


def test_diary_natural_day_idempotence_and_latest_ledger_retract_all_views(core, tmp_path):
    seed_day(core, ["天文讨论一", "天文讨论二", "天文讨论三"])
    assert generate(core)["state"] == "COMMITTED"
    original = core.diary.schedule_day(DAY)
    assert core.diary.schedule_day(DAY) == original
    assert not run(core.diary.generate_once("second-worker"))
    assert len(core.diary.model.calls) == 1
    key, archive, dest = Fernet.generate_key(), tmp_path / "growth.enc", tmp_path / "restore.db"
    backup(core.settings.database, archive, key)
    core.diary.forget(DAY, core.settings.owner_qq_id)
    restore(archive, dest, key, core.settings.erasure_ledger)
    reopened = Core(core.settings.model_copy(update={"database": str(dest)}), clock=core.clock)
    try:
        assert reopened.diary.get_day(DAY, core.settings.owner_qq_id) is None
        assert reopened.diary.schedule_day(DAY) is None
        assert reopened.store.rows("SELECT * FROM diary_evidence") == []
        assert reopened.store.rows("SELECT * FROM diary_revisions") == []
        assert reopened.store.rows("SELECT * FROM outbox") == []
    finally:
        reopened.store.close()


def test_discovered_titles_alone_do_not_fund_a_diary_of_completed_reads(core):
    core.settings.diary_enabled = True
    enable_observations(core)
    for number in range(3):
        text = f"天文标题{number}"
        core.observations.append(
            "reader",
            observation(
                core,
                number,
                text=text,
                occurred_at="2026-10-18T04:00:00Z",
                source_kind="title",
                result_status="DISCOVERED",
                payload={"title": text, "text": text},
            ),
        )
    assert generate(core)["state"] == "SKIPPED"
    assert not core.diary.model.calls and not core.model.calls
    assert core.store.rows("SELECT * FROM model_usage") == []
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.diary.schedule_day(date(2026, 10, 18))


def test_shadow_and_timeline_retain_revoked_read_history_but_forget_replays(core, tmp_path):
    grant = prepare(core)
    source = accepted(core, grant)
    core.observations.grants.revoke(grant.grant_id)
    core.clock.advance(3601)  # Read grant expires; accepted history is not an active read.
    assert core.social.preview(source, core.settings.owner_qq_id)
    result = recent_timeline(
        core.store, core.settings, core.clock(), core.settings.owner_qq_id, topic="Source-grant"
    )
    assert result["items"][0]["event_id"] == source
    before = core.store.db.total_changes
    core.store.execute("PRAGMA query_only=ON")
    try:
        assert core.social.preview(source, core.settings.owner_qq_id)
        assert view(core)["self.interest"] == []
        assert recent_timeline(
            core.store, core.settings, core.clock(), core.settings.owner_qq_id, topic="Source-grant"
        )["items"]
        assert core.store.db.total_changes == before
    finally:
        core.store.execute("PRAGMA query_only=OFF")
    key, archive, dest = Fernet.generate_key(), tmp_path / "grant.enc", tmp_path / "restore.db"
    backup(core.settings.database, archive, key)
    core.observations.grants.forget(grant.grant_id, core.ledger)
    restore(archive, dest, key, core.settings.erasure_ledger)
    reopened = Core(core.settings.model_copy(update={"database": str(dest)}), clock=core.clock)
    try:
        assert reopened.social.preview(source, core.settings.owner_qq_id) is None
        assert (
            recent_timeline(
                reopened.store,
                reopened.settings,
                reopened.clock(),
                reopened.settings.owner_qq_id,
                topic="Source-grant",
            )["items"]
            == []
        )
        assert not reopened.store.rows("SELECT * FROM outbox")
    finally:
        reopened.store.close()


def test_core_context_and_life_explanation_recheck_stale_interest_sources(core, monkeypatch):
    complete_read(core, 1, "https://example.org/1")
    core.settings.memory_enabled = True
    # This seam verifies the traits projection, not BGE/recall quality.
    monkeypatch.setattr(core.memory, "search", lambda *args, **kwargs: [])
    ingest(core, 10, "天文是什么")
    job = core.store.one(
        "SELECT j.*,e.body_json,e.conversation_id FROM jobs j JOIN events e "
        "USING(event_id) WHERE e.event_id='evt_test_10'"
    )
    assert "self.interest" in encode(core.context(job))
    explanation = core.life.command("/为什么形成偏好", "evt_test_10")
    assert "「天文研究」：自己挑着看了1次" in explanation
    assert "https://example.org/1" in explanation and "还没形成自己的评价" in explanation
    core.clock.advance(core.settings.raw_event_retention_days * 86400 + 1)
    # The original job consumed the now-expired interest evidence and must stay fenced.
    with pytest.raises(CoreError, match="REPLY_SOURCE_RECALLED"):
        core.context(job)
    ingest(core, 11, "再聊聊天文是什么")
    current = core.store.one(
        "SELECT j.*,e.body_json,e.conversation_id FROM jobs j JOIN events e "
        "USING(event_id) WHERE e.event_id='evt_test_11'"
    )
    assert "self.interest" not in encode(core.context(current))
    assert "还没有足够" in core.life.command("/为什么形成偏好", "evt_test_11")


@pytest.mark.parametrize("prior", [False, True])
@pytest.mark.parametrize(
    "text,expected",
    [
        ("/记住这个：天文偏好", False),
        ("/目标列表 天文", False),
        ("/修改日记 2026-10-18 天文笔记", False),
        ("/随便聊聊 天文", True),
    ],
)
def test_unmarked_control_sources_are_filtered_but_unknown_slash_is_ordinary(
    core, prior, text, expected
):
    ingest(core, 1, text, at="2026-10-17T04:00:00Z" if prior else "2026-10-18T04:00:00Z")
    event = core.store.one("SELECT * FROM events WHERE event_id='evt_test_1'")
    body = json.loads(event["body_json"])
    # Retained bounded-window / legacy events may have no control marker.
    for key in ("control_command", "diary_command", "diary_correction", "diary_timezone"):
        body.pop(key, None)
    core.store.execute("UPDATE events SET body_json=? WHERE event_id='evt_test_1'", (encode(body),))
    if prior:
        core.settings.memory_enabled = True
        core.memory.episodes([event], core.clock())
        ingest(core, 2, "天文今天普通聊天", at="2026-10-18T04:00:00Z")
    sources = core.diary.repo.sources(DAY)
    assert ("evt_test_1" in {row["event_id"] for row in sources}) is expected
    assert ("evt_test_1" in {row["event_id"] for row in timeline(core)["items"]}) is expected
    assert not core.model.calls


@pytest.mark.parametrize(
    "path,field",
    [
        ("/internal/v2/interests", "owner.preference"),
        ("/internal/v2/timeline?topic=天文", "items"),
        ("/internal/v2/social/obs_test_10/preview", "draft"),
        ("/internal/v2/affect/evt_test_2/preview", "proposal"),
    ],
)
def test_private_growth_responses_never_cache_and_forget_updates_current_response(
    core, path, field
):
    remember(core, 1, "天文私人偏好", confirmed=True)
    core.settings.memory_enabled = False  # Inspect evidence; no semantic index warmup is needed.
    ingest(core, 2, "简短一点")
    enable_observations(core)
    configure(core)
    core.observations.append(
        "heybox",
        observation(
            core,
            10,
            source="heybox",
            text="天文新进展",
            payload={"title": "开发进展", "text": "天文新进展"},
        ),
    )
    with TestClient(create_app(core.settings)) as client:
        api_core = client.app.state.core
        api_core.clock = api_core.affect.clock = api_core.social.clock = core.clock
        bridge = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
        owner = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
        assert client.get(path, headers=bridge).status_code == 401
        before = client.get(path, headers=owner)
        assert before.status_code == 200 and before.json()[field]
        assert before.headers.get("Cache-Control") == "no-store"
        with core.store.transaction():
            core.ledger.expire_events(
                core.store, ["evt_test_1", "evt_test_2", "obs_test_10"], core.clock()
            )
        after = client.get(path, headers=owner)
        assert after.status_code == 200 and not after.json()[field]
        assert after.headers.get("Cache-Control") == "no-store"
