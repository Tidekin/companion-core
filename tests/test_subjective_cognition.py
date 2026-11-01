"""Multi-day reads, her own judgements, views that change, and the choices they inform."""

import json
from datetime import date, datetime

import pytest
from cryptography.fernet import Fernet

from companion import interest, watch_ledger
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.diary import DiaryDraft, DiaryRepository, render_draft
from companion.goals import CommitmentRequest, GoalProposal, GoalScope
from companion.life_context import decision_context
from companion.life_protocol import LifeDecision, LifeLoopConfig
from companion.life_topics import interest_shift_supported
from companion.store import after, encode
from tests.conftest import ingest, run
from tests.test_goals import result_for
from tests.test_observations import enable_observations, observation
from tests.test_watch_ledger import ready

ASTRONOMY, BAKING = "天文研究", "烘焙尝试"


def prepare(core):
    core.settings.goals_enabled = core.settings.exploration_enabled = True
    core.settings.wake_enabled = core.settings.memory_enabled = True
    core.settings.watch_ledger = core.settings.watch_ledger.model_copy(update={"enabled": True})
    core.settings.life_loop = LifeLoopConfig(autonomy_enabled=True)
    enable_observations(core)


def read(core, number, topic, url, *, said="想找点东西看看", commitment=False):
    """One real chain: owner message -> native goal -> leased step -> Reader receipt."""
    ingest(core, number, said)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE event_id=?",
                       (f"evt_test_{number}",))
    proposal = GoalProposal(topic=topic, source_event_ids=[f"evt_test_{number}"],
                            scope=GoalScope(operation="read_content", url=url))
    if commitment:
        goal = core.goals.accept_commitment(CommitmentRequest(
            proposal=proposal, accepted_event_id=f"evt_test_{number}",
            deadline=datetime.fromisoformat(after(core.clock(), 86400))))["goal_id"]
    else:
        goal = core.goals.propose(proposal)
        core.goals.accept(goal)
    step = core.goals.claim()
    core.goals.finish(step, result_for(core, goal, number))
    return goal, f"obs_test_{number}"


def judge(core, event_id, rating, take):
    """A settled Watch Ledger judgement; the paid path itself is covered below."""
    entry_id = "watch_" + event_id
    core.store.execute(
        "INSERT INTO watch_ledger(entry_id,source_event_id,platform,item_ref,depth,state,rating,"
        "take,topic_tags_json,created_at,updated_at) SELECT ?,event_id,'web',canonical_url,"
        "'text','JUDGED',?,?,'[]',?,? FROM observations WHERE event_id=?",
        (entry_id, rating, take, core.clock(), core.clock(), event_id))
    return entry_id


def item(core, topic):
    return next(row for row in interest.profile(core.store, core.settings, core.clock(),
                                                core.settings.owner_qq_id)["self.interest"]
                if row["topic"] == topic)


def next_day(core):
    core.clock.advance(86400)


def test_views_form_strengthen_change_and_explain_their_causes(core):
    prepare(core)
    _, first = read(core, 1, ASTRONOMY, "https://example.org/nebula")
    assert item(core, ASTRONOMY)["view"]["stance"] == "undecided"  # A read is not a view.
    judge(core, first, 2, "星云的颜色原来是不同元素发光")
    view = item(core, ASTRONOMY)["view"]
    assert (view["stance"], view["certainty"]) == ("positive", "tentative")
    next_day(core)
    judge(core, read(core, 2, ASTRONOMY, "https://example.org/spectra")[1], 1, "光谱线有点难懂")
    next_day(core)
    judge(core, read(core, 3, ASTRONOMY, "https://example.org/stars")[1], 2, "恒星颜色和温度有关")
    current = item(core, ASTRONOMY)
    assert (current["view"]["stance"], current["view"]["certainty"]) == ("positive", "settled")
    assert [change["to_certainty"] for change in current["view"]["history"]] == [
        "tentative", "settled"]
    assert current["view"]["history"][-1]["because"]["event_id"] == "obs_test_3"
    # A repeated item adds no experience, however often it is read.
    next_day(core)
    read(core, 4, ASTRONOMY, "https://example.org/stars")
    assert item(core, ASTRONOMY)["independent_sessions"] == 3
    # Two disappointing reads revise the view, and the change names its cause.
    judge(core, read(core, 5, ASTRONOMY, "https://example.org/hype")[1], -2, "标题夸张内容很空")
    next_day(core)
    last = read(core, 6, ASTRONOMY, "https://example.org/hype-2")[1]
    judge(core, last, -2, "又是同样的空话")
    current = item(core, ASTRONOMY)
    assert current["view"]["stance"] == "mixed"
    # The first disappointing read already split the view; the second only confirms it.
    assert current["view"]["history"][-1]["because"]["take"] == "标题夸张内容很空"
    assert current["view"]["latest"][-1]["take"] == "又是同样的空话"
    explanation = "\n".join(interest.explain(current))
    assert "从喜欢变成看法有分歧" in explanation and "标题夸张内容很空" in explanation
    assert "https://example.org/hype-2" in explanation
    assert "自己挑着看了5次" in explanation
    assert explanation in core.life.command("/为什么形成偏好", "evt_test_6")


def test_owner_links_start_a_topic_but_are_not_her_own_pick(core):
    prepare(core)
    url = "https://example.org/bread"
    _, owner_read = read(core, 1, BAKING, url, said="你看看这个 " + url)
    judge(core, owner_read, -1, "步骤太多，看着就累")
    baking = item(core, BAKING)
    assert (baking["independent_sessions"], baking["owner_suggested_sessions"]) == (0, 1)
    assert baking["status"] == "EXPLORING"
    # She may disagree with the owner's pick; it is still only a tentative view.
    assert (baking["view"]["stance"], baking["view"]["certainty"]) == ("negative", "tentative")
    assert baking["view"]["latest"][0]["origin"] == "owner_suggested"
    assert "另有1次是主人提到后去看的" in "\n".join(interest.explain(baking))
    # Her own later pick on the same topic counts as hers.
    next_day(core)
    judge(core, read(core, 2, BAKING, "https://example.org/scones")[1], 2, "司康做法简单多了")
    baking = item(core, BAKING)
    assert (baking["independent_sessions"], baking["owner_suggested_sessions"]) == (1, 1)
    # An owner-assigned commitment never becomes an interest at all.
    read(core, 3, "主人安排的阅读", "https://example.org/assigned", commitment=True)
    assert not core.store.one("SELECT 1 FROM traits WHERE topic='主人安排的阅读'")
    # Owner preferences stay in their own namespace.
    profile = interest.profile(core.store, core.settings, core.clock(), core.settings.owner_qq_id)
    assert all(row["namespace"] == "self.interest" for row in profile["self.interest"])


def test_guidance_offers_choices_between_interests_without_granting_anything(core):
    prepare(core)
    goal_a, a1 = read(core, 1, ASTRONOMY, "https://example.org/a1")
    judge(core, a1, 2, "很喜欢这组星图")
    next_day(core)
    judge(core, read(core, 2, ASTRONOMY, "https://example.org/a2")[1], 2, "黑洞照片看得入迷")
    next_day(core)
    goal_a3, a3 = read(core, 3, ASTRONOMY, "https://example.org/a3")
    judge(core, a3, 1, "望远镜原理讲得清楚")
    goal_b, b1 = read(core, 4, BAKING, "https://example.org/b1")
    judge(core, b1, -2, "全是广告")
    ids = ["evt_test_3", a3, "evt_test_4", b1]
    before = core.store.db.total_changes
    context = decision_context(core.store, core.settings, [{"event_id": key} for key in ids],
                               "synthetic", core.clock())
    assert core.store.db.total_changes == before
    guidance = {row["topic"]: row for row in context["autonomy_guidance"]["depth_control"]}
    astronomy, baking = guidance[ASTRONOMY], guidance[BAKING]
    # Earlier experiences under the same approval inform the view; IDs stay the anchor's.
    assert astronomy["independent_sessions"] == 3
    assert astronomy["source_event_ids"] == sorted(["evt_test_3", a3])
    assert astronomy["view"] == {"stance": "positive", "certainty": "settled"}
    assert [row["take"] for row in astronomy["why"]][-1] == "望远镜原理讲得清楚"
    assert [option["action"] for option in astronomy["options"]] == [
        "continue", "reflect", "rest"]
    assert [option["action"] for option in baking["options"]] == ["pause", "reflect", "rest"]
    assert "权限" not in encode(guidance) and "priority" not in encode(guidance)
    # Life choices themselves never count: an accepted but unfinished goal changes nothing.
    ingest(core, 9, "随便看看")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE event_id='evt_test_9'")
    pending = core.goals.propose(GoalProposal(
        topic=ASTRONOMY, source_event_ids=["evt_test_9"],
        scope=GoalScope(operation="read_content", url="https://example.org/a4")))
    core.goals.accept(pending)
    assert decision_context(core.store, core.settings, [{"event_id": key} for key in ids],
                            "synthetic", core.clock()) == context
    # A described change of view needs a recorded change within the cited sources.
    shift = LifeDecision(action="SHARE", destination="PRIVATE_DIARY", source_event_ids=ids,
                         reason="读后想法", text="今天读了几篇", interest_shift="烘焙那篇让我泄气")
    assert interest_shift_supported(shift, context)
    narrow = shift.model_copy(update={"source_event_ids": ["evt_test_3", "evt_test_4"]})
    assert not interest_shift_supported(narrow, context)
    assert not interest_shift_supported(shift, {**context, "autonomy_guidance": {
        "depth_control": [{**row, "changes": []} for row in guidance.values()]}})
    # Without the exploration purpose her judgements are not read at all.
    core.settings.watch_ledger = core.settings.watch_ledger.model_copy(update={"enabled": False})
    hidden = decision_context(core.store, core.settings, [{"event_id": key} for key in ids],
                              "synthetic", core.clock())
    assert all(row["view"]["stance"] == "undecided"
               for row in hidden["autonomy_guidance"]["depth_control"])
    assert goal_a and goal_a3 and goal_b


def test_resting_interests_fade_without_losing_their_history(core):
    prepare(core)
    judge(core, read(core, 1, ASTRONOMY, "https://example.org/old")[1], 1, "还挺有意思")
    assert item(core, ASTRONOMY)["status"] == "EXPLORING"
    core.clock.advance((interest.FADE_DAYS + 1) * 86400)
    rested = item(core, ASTRONOMY)
    assert rested["status"] == "FADING" and rested["view"]["stance"] == "positive"


def test_withdrawn_or_forgotten_sources_reevaluate_and_backups_do_not_restore(core, tmp_path):
    prepare(core)
    _, first = read(core, 1, ASTRONOMY, "https://example.org/w1")
    judge(core, first, 2, "第一篇很喜欢")
    next_day(core)
    _, second = read(core, 2, ASTRONOMY, "https://example.org/w2")
    entry = judge(core, second, -2, "第二篇基于错的资料")
    assert item(core, ASTRONOMY)["view"]["stance"] == "mixed"
    # A reflection hypothesis citing the corrected judgement goes back to review.
    core.store.insert("reflection_runs", run_id="run_1", identity_id="companion",
                      local_date="2026-01-01", timezone=core.settings.timezone,
                      state="COMMITTED", source_set_sha256="x", captured_privacy_epoch=0,
                      created_at=core.clock(), updated_at=core.clock())
    core.store.insert("reflection_evidence", run_id="run_1", event_id=second,
                      source_id="take:" + entry, outbox_message_id=None)
    core.store.insert("reflection_suggestions", suggestion_id="suggestion_1", run_id="run_1",
                      kind="possible_preference", content="我可能不喜欢天文",
                      evidence_json=encode([{"source_id": "take:" + entry,
                                             "exact_quote": "第二篇"}]),
                      state="ACCEPT_AS_HYPOTHESIS", created_at=core.clock(),
                      updated_at=core.clock())
    with core.store.transaction():
        watch_ledger.withdraw(core.store, entry, core.clock())
    with pytest.raises(CoreError, match="WATCH_ENTRY_NOT_FOUND"):
        watch_ledger.withdraw(core.store, entry, core.clock())
    assert core.store.one("SELECT state FROM reflection_suggestions")["state"] == (
        "NEED_MORE_EVIDENCE")
    corrected = item(core, ASTRONOMY)["view"]
    assert (corrected["stance"], corrected["judged_experiences"]) == ("positive", 1)
    # The withdrawn item is never judged again by a later scan.
    assert watch_ledger.scan(core.store, core.settings, core.clock()) == 0
    archive, destination = tmp_path / "before.enc", tmp_path / "restored.db"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    with core.store.transaction():
        core.ledger.expire_events(core.store, [first], core.clock())
    assert not core.store.one("SELECT 1 FROM watch_ledger WHERE source_event_id=?", (first,))
    restore(archive, destination, key, core.settings.erasure_ledger)
    with Core(core.settings.model_copy(update={"database": str(destination),
                                               "memory_enabled": False}),
              clock=core.clock) as restored:
        assert not restored.store.one(
            "SELECT 1 FROM watch_ledger WHERE source_event_id=?", (first,))
        views = interest.profile(restored.store, restored.settings, restored.clock(),
                                 restored.settings.owner_qq_id)["self.interest"]
        assert all(row["view"]["judged_experiences"] == 0 for row in views)


def test_only_completed_goal_web_reads_join_the_watch_ledger(core):
    prepare(core)
    _, result = read(core, 1, ASTRONOMY, "https://example.org/goal-read")

    core.observations.append("reader", observation(core, 2, text="随手抓取的页面",
                                                   payload={"text": "随手抓取的页面",
                                                            "url": "https://example.org/x"}))
    core.store.execute("UPDATE events SET source='observation.web' WHERE event_id IN (?,?)",
                       (result, "obs_test_2"))
    with core.store.transaction():
        assert watch_ledger.scan(core.store, core.settings, core.clock()) == 1
    assert [(row["source_event_id"], row["platform"]) for row in core.store.rows(
        "SELECT * FROM watch_ledger")] == [(result, "web")]


def test_diary_sources_carry_her_own_takes_as_views_not_facts(core):
    prepare(core)
    core.settings.observation_enabled = True
    _, result = read(core, 1, ASTRONOMY, "https://example.org/diary")
    entry = judge(core, result, 1, "这篇讲得很清楚")
    day = date.fromisoformat(core.clock()[:10])
    sources = DiaryRepository(core.store, core.settings, core.clock).sources(day)
    take = next(row for row in sources if row["kind"] == "own_take")
    assert take["source_id"] == "take:" + entry and take["event_id"] == result
    assert take["text"] == "我读完后的评价（还不错）：这篇讲得很清楚"
    body = render_draft(DiaryDraft.model_validate({
        "schema_version": 1, "local_date": day.isoformat(), "title": "读书", "worth_saving": True,
        "sections": [{"kind": "observation", "text": "这篇讲得很清楚",
                      "evidence": [{"source_id": take["source_id"],
                                    "exact_quote": "这篇讲得很清楚"}]}]}),
        sources, day.isoformat(), core.settings)
    assert body == "我当时的看法：“这篇讲得很清楚”"
    with core.store.transaction():
        watch_ledger.withdraw(core.store, entry, core.clock())
    assert not any(row["kind"] == "own_take" for row in DiaryRepository(
        core.store, core.settings, core.clock).sources(day))
    assert json.dumps(sources, ensure_ascii=False)


def test_paid_judgement_of_a_goal_read_becomes_her_view(core, tmp_path):
    prepare(core)
    _, result = read(core, 1, ASTRONOMY, "https://example.org/paid")
    core.store.execute("UPDATE events SET source='observation.web' WHERE event_id=?", (result,))
    take = {"rating": 2, "take": "原来星光颜色能看出温度", "topic_tags": ["天文"]}
    provider = ready(core, tmp_path, json.dumps(take, ensure_ascii=False))
    assert run(watch_ledger.run_once(core.store, core.settings, core.model, core.clock))
    assert len(provider.calls) == 1
    current = item(core, ASTRONOMY)
    assert current["view"]["latest"] == [{
        "event_id": result, "occurred_at": current["evidence"][0]["occurred_at"],
        "source_event_id": result, "entry_id": watch_ledger.recent(core.store)[0]["entry_id"],
        "judged_at": core.clock(),
        "origin": "self", "rating": 2, "take": "原来星光颜色能看出温度"}]
    assert watch_ledger.recent(core.store)[0]["origin"] == watch_ledger.ORIGINS["self"]
    # One read is one judgement: another pass makes no further call.
    assert not run(watch_ledger.run_once(core.store, core.settings, core.model, core.clock))
    assert len(provider.calls) == 1


def test_watch_ledger_migration_keeps_entries_and_refuses_lossy_downgrade(tmp_path):
    import sqlite3
    from contextlib import closing

    from alembic import command
    from alembic.config import Config

    from companion.store import migrate

    path = tmp_path / "ledger.db"
    migrate(str(path), "0055_runtime_configuration")
    now = "2026-10-31T00:00:00.000000+00:00"
    # A raw connection without foreign keys: only the ledger row's copy is under test.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO watch_ledger(entry_id,source_event_id,platform,item_ref,depth,"
                   "state,rating,take,created_at,updated_at) VALUES('watch_1','obs_1',"
                   "'bilibili','u','text','JUDGED',1,'不错',?,?)", (now, now))
    migrate(str(path), "0056_watch_ledger_goal_reads")
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute("SELECT take FROM watch_ledger").fetchone() == ("不错",)
        db.execute("UPDATE watch_ledger SET platform='web',state='WITHDRAWN'")
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + path.resolve().as_posix())
    with pytest.raises(RuntimeError, match="block downgrade"):
        command.downgrade(cfg, "0055_runtime_configuration")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE watch_ledger SET platform='bilibili',state='JUDGED'")
    command.downgrade(cfg, "0055_runtime_configuration")
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT take FROM watch_ledger").fetchone() == ("不错",)
