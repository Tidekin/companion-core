"""Cross-module context contracts through real Core assembly and SDK selection."""

from copy import deepcopy

import pytest

from companion import context_planner as planner
from companion import context_selection as selection
from companion import watch_ledger
from companion.contracts import Candidate, CoreError
from companion.goals import GoalProposal, GoalScope
from companion.store import encode
from companion.token_measurement import measure_request
from tests.conftest import ingest
from tests.support.context_planner import initial, request_context
from tests.support.dialogue_context import current_job, sent_history
from tests.support.mood import feedback
from tests.support.owner_runtime import chat
from tests.support.subjective_cognition import ASTRONOMY, BAKING, judge, prepare, read


def test_exact_fact_coalescing_keeps_subject_status_and_all_evidence():
    refs = selection.References()
    fact = "这篇星光资料讲得很清楚"
    first = refs.add("Life", [{"verified_result": fact, "state": "DONE"}], {"observation", "owner"},
                     fact=("self", fact), module="life")
    second = refs.add("Watch", [{"take": fact, "rating": 1, "origin": "主人推荐"}],
                      {"observation"}, fact=("self", fact), module="watch")
    third = refs.add("Memory", {"content": fact, "status": "CONFIRMED"}, {"owner"},
                     fact=("self", fact), module="memory", required=True)
    assert first is second is third
    assert encode(refs.messages).count(fact) == 1
    assert all(value in encode(first) for value in ("DONE", "CONFIRMED", "主人推荐", "rating"))
    assert refs.sources == [(first, {"observation", "owner"})]
    assert id(first) in refs.required
    refs.add("Other subject", {"content": fact}, {"owner"}, fact=("user", fact))
    refs.add("Other evidence", {"content": fact}, {"independent"}, fact=("self", fact))
    refs.add("Contrary", {"take": "这篇资料全是广告"}, {"observation"},
             fact=("self", "这篇资料全是广告"))
    assert len(refs.messages) == 4


def test_current_topic_and_short_continuation_use_actual_owner_history():
    sources = [{"user_text": "天文光谱资料", "reply_text": "烘焙广告"}]
    assert selection.focus("继续", sources) == selection.terms("天文光谱资料")
    assert selection.focus("继续聊烘焙", sources) == selection.terms("继续聊烘焙")
    assert selection.relevance(selection.focus("继续", sources), "天文研究") == selection.RELATED


def test_life_read_plan_and_watch_redirect_keep_owner_recommendation(core):
    prepare(core)
    urls = ["https://example.org/one", "https://example.org/two"]
    ingest(core, 1, "你可以看看 " + urls[1])
    core.goals.propose(GoalProposal(topic=ASTRONOMY, source_event_ids=["evt_test_1"],
        scope=GoalScope(operation="read_plan", urls=urls)))
    assert core.life.context()[0]["origin"] == watch_ledger.ORIGINS["owner_suggested"]
    original = "https://example.org/original"
    _, source = read(core, 2, BAKING, "https://example.org/resolved", said="看看 " + original)
    judge(core, source, -1, "步骤太复杂")
    core.store.execute('UPDATE observations SET evidence_json=%s WHERE event_id=%s',
                       (encode({"requested_url": original}), source))
    assert watch_ledger.recent(core.store)[0]["origin"] == watch_ledger.ORIGINS["owner_suggested"]
    assert core.life.context()[0]["origin"] == watch_ledger.ORIGINS["owner_suggested"]


def test_source_only_proof_does_not_enable_memory_fact_access(core):
    prepare(core)
    _, source = read(core, 1, ASTRONOMY, "https://example.org/source")
    core.settings.memory_enabled = False
    proof = core.memory_context.pin(core.clock(), event_ids=[source])
    assert proof["rows"] == []
    core.memory_context.revalidate(proof, core.clock())
    with pytest.raises(CoreError, match="MEMORY_ACCESS_DENIED"):
        core.memory_context.recall("天文", core.clock())


def test_core_coalesces_memory_watch_and_life_without_losing_proofs(core):
    prepare(core)
    fact = "实际获得的天文内容 1"
    _, source = read(core, 1, ASTRONOMY, "https://example.org/shared",
                     said="你刚才" + fact)
    judge(core, source, 1, fact)
    mid = core.memory.add(Candidate(subject="self", kind="fact", fact_key="star-view", content=fact,
        evidence_event_id="evt_test_1", evidence_quote="你刚才" + fact),
        core.clock(), confirmed=True)
    row = core.store.one('SELECT * FROM memories WHERE memory_id=%s', (mid,))
    row["evidence_ids"] = ["evt_test_1"]
    snapshots = []
    job = current_job(core, text="天文资料你当时怎么看？")
    messages = core.context(job, memories=[row], snapshots=snapshots)
    assert encode(messages).count(fact) == 1
    assert {p["kind"] for p in snapshots} >= {"memory", "watch", "interest"}
    pinned = core.store.rows('SELECT event_id FROM job_sources WHERE job_id=%s', (job["job_id"],))
    assert {r["event_id"] for r in pinned} >= {source, "evt_test_1"}
    assert not core.model.calls


def test_related_old_judgement_beats_recent_unrelated_reads_and_keeps_origin(core):
    prepare(core)
    core.settings.context_soft_tokens = 8000
    url = "https://example.org/owner-star"
    _, source = read(core, 1, ASTRONOMY, url, said="你看看这个 " + url)
    judge(core, source, -1, "天文图谱标注太含糊")
    for number in range(2, 6):
        core.clock.advance(1)
        judge(core, read(core, number, BAKING, f"https://example.org/bread-{number}")[1],
              2, f"烘焙步骤很简单{number}")
    messages = core.context(current_job(core, text="天文图谱你之前有什么看法？"), memories=[])
    prompt = "\n".join(m["content"] for m in messages)
    assert prompt.count("天文图谱标注太含糊") == 1
    assert "主人发给你后你决定去看的" in prompt
    assert '"owner_suggested_sessions":1' in prompt
    assert '"independent_sessions":0' in prompt
    assert '"judged_at"' in prompt and '"negative"' in prompt
    assert not core.model.calls


def test_core_drops_mood_before_evidence_and_preserves_current_conversation(core):
    prepare(core)
    core.settings.context_soft_tokens = 50000
    feedback(core, 30)
    _, source = read(core, 1, ASTRONOMY, "https://example.org/critical")
    judge(core, source, -2, "天文资料错误很多")
    sent_history(core, 20, "旧的烘焙话题" * 400, "旧回答" * 300)
    sent_history(core, 21, "天文资料里那张图有问题", "对，标注与光谱不符")
    job = current_job(core, text="天文资料再说说？")
    full = core.context(job, memories=[])
    assert "你的心情参考" in encode(full)
    assert "天文资料错误很多" in encode(full)
    relevant = [m for m in full if "天文资料错误很多" in m["content"]]
    core.settings.context_soft_tokens = measure_request(
        core.settings, [full[0], *relevant, *full[-3:]]).reserved_input_tokens
    messages = core.context(job, memories=[])
    assert "你的心情参考" not in encode(messages)
    assert "天文资料错误很多" in encode(messages)
    assert "对，标注与光谱不符" in encode(messages)
    assert "旧的烘焙话题" not in encode(messages)
    assert messages[-1] == full[-1] and not core.model.calls


@pytest.mark.parametrize("mutation", ["withdraw", "source", "interest"])
def test_frozen_judgement_revalidates_before_any_new_use(core, mutation):
    prepare(core)
    core.settings.context_soft_tokens = 8000
    _, source = read(core, 1, ASTRONOMY, "https://example.org/frozen")
    entry = judge(core, source, 1, "星光资料很好懂")
    job, snapshots = current_job(core, text="天文资料怎么样？"), []
    core.context(job, memories=[], snapshots=snapshots)
    if mutation == "withdraw":
        watch_ledger.withdraw(core.store, entry, core.clock())
    elif mutation == "source":
        core.store.execute('UPDATE events SET body_json=%s WHERE event_id=%s',
                           (encode({"text": "修订后的资料"}), source))
    else:
        core.store.execute("UPDATE traits SET topic='修订后的兴趣'")
    with pytest.raises(CoreError, match="REPLY_CANCELLED"):
        core._check_context_snapshots(job, snapshots)
    assert not core.model.calls


def test_assembly_and_sdk_share_order_and_v1_remains_readable(core, tmp_path):
    _, job = chat(core, tmp_path)
    messages, snapshots = initial(core, job, references=2, turns=1)
    groups = planner.check(core, job, snapshots)["groups"]
    groups[1]["priority"], groups[2]["priority"], groups[3]["priority"] = 3, 0, 4
    limit = measure_request(core.settings, [messages[0], messages[1], *messages[3:]]
                            ).reserved_input_tokens
    core.settings.context_soft_tokens = limit
    selected = planner.plan_request(core, job, snapshots, request_context(messages))
    from companion.agent_messages import to_wire_messages

    assert to_wire_messages(selected) == [messages[0], messages[1], *messages[3:]]
    assert planner.check(core, job, snapshots)["pending"]["dropped"][0]["group_id"] == "g2"
    _, legacy = initial(core, job)
    legacy = deepcopy(legacy)
    plan = legacy[-1]["metadata"]["core_context"]
    plan["version"] = 1
    plan.pop("assembly_dropped")
    for group in plan["groups"]:
        group.pop("priority")
    core.settings.context_soft_tokens = 1
    assert len(to_wire_messages(planner.plan_request(
        core, job, legacy, request_context(initial(core, job)[0])))) == 2
