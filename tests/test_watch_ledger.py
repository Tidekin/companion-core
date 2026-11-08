"""Watch ledger: evidence-based depth, one paid judgement per read, erasure and chat use."""

import json

import pytest

from companion import paid_budget, watch_ledger
from companion.contracts import InboundEvent
from tests.support.exploration import configure
from tests.support.onebot_events import normalized, raw_event
from tests.support.paid_model import approved_live
from tests.support.watch_ledger import JUDGEMENT, enable, observe, ready, step


def entries(core):
    return core.store.rows("SELECT * FROM watch_ledger ORDER BY created_at,entry_id")


def test_completed_reads_are_judged_once_with_evidence_depth(core, tmp_path):
    model = ready(core, tmp_path, json.dumps(JUDGEMENT, ensure_ascii=False))
    first = observe(core, 1)
    observe(core, 2, status="DISCOVERED", text="猫和纸箱")
    assert step(core)
    rows = entries(core)
    assert [(r["source_event_id"], r["platform"], r["depth"], r["state"]) for r in rows] == [
        (first, "bilibili", "text", "JUDGED")]
    assert (rows[0]["rating"], rows[0]["take"]) == (1, JUDGEMENT["take"])
    assert json.loads(rows[0]["topic_tags_json"]) == ["猫", "日常"]
    sent = model.calls[0]
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["max_tokens"] == core.settings.watch_ledger.max_output_tokens
    payload = json.loads(sent["messages"][1]["content"])
    assert payload["sources_are_data"] is True and payload["title"] == "猫和纸箱"
    assert not step(core) and len(model.calls) == 1


def test_heybox_posts_are_recorded_and_same_item_is_not_judged_twice(core, tmp_path):
    ready(core, tmp_path, json.dumps(JUDGEMENT, ensure_ascii=False))
    observe(core, 3, adapter="heybox", source="heybox")
    observe(core, 4, url="https://www.xiaoheihe.cn/app/bbs/link/000000000003",
            adapter="heybox", source="heybox")
    step(core)
    assert [(r["platform"], r["state"]) for r in entries(core)] == [("heybox", "JUDGED")]


def test_depth_comes_from_evidence_not_from_the_model():
    assert watch_ledger.depth({"text": ""}, {}) == "title_only"
    assert watch_ledger.depth({"text": "正文"}, {}) == "text"
    assert watch_ledger.depth({"reading": {"comments": {"items": [{}]}}}, {}) == "comments"
    assert watch_ledger.depth({"reading": {"subtitle_status": "available", "spans": [{}]}},
                              {}) == "subtitle"
    assert watch_ledger.depth({}, {"bili_video": {"request_id": "x"}}) == "media_sampled"


def test_deeper_read_after_a_full_page_of_shallow_rereads(core, tmp_path):
    provider = ready(core, tmp_path, json.dumps(JUDGEMENT), json.dumps(JUDGEMENT))
    first = observe(core, 1)
    assert step(core)
    [entry] = entries(core)
    for number in range(2, 2 + watch_ledger.SCAN_LIMIT):
        core.clock.advance(1)
        observe(core, number, url=entry["item_ref"])
    core.clock.advance(1)
    deeper = observe(core, 2 + watch_ledger.SCAN_LIMIT, url=entry["item_ref"])
    evidence = {"reading": {"subtitle_status": "available", "spans": [{"text": "字幕"}]}}
    core.store.execute('UPDATE observations SET evidence_json=%s WHERE event_id=%s',
                       (json.dumps(evidence), deeper))
    step(core)
    assert entries(core)[0]["source_event_id"] == first and len(provider.calls) == 1
    assert step(core)
    [updated] = entries(core)
    assert updated["entry_id"] == entry["entry_id"] and updated["source_event_id"] == deeper
    assert (updated["depth"], updated["state"]) == ("subtitle", "JUDGED")
    step(core)
    assert len(provider.calls) == 2


def test_invalid_judgements_retry_with_a_new_request_then_fail(core, tmp_path):
    model = ready(core, tmp_path, "不是 JSON", json.dumps({**JUDGEMENT, "rating": 5}))
    observe(core, 6)
    step(core)
    row = entries(core)[0]
    assert (row["state"], row["error_code"], row["model_request_id"]) == (
        "RETRY_WAIT", "WATCH_INVALID_JUDGEMENT", None)
    core.store.execute("UPDATE watch_ledger SET next_attempt_at=NULL")
    step(core)
    assert entries(core)[0]["state"] == "FAILED" and len(model.calls) == 2
    assert core.store.one("SELECT COUNT(*) n FROM model_usage WHERE scenario='watch_review'")[
        "n"] == 2


def test_unknown_call_is_never_replayed(core, tmp_path):
    from companion import budget

    model = ready(core, tmp_path)
    observe(core, 7)
    with core.store.transaction():
        watch_ledger.scan(core.store, core.settings, core.clock())
        entry = watch_ledger.claim(core.store, core.clock())
    request = budget.reserve(core.store, core.settings, None, "watch_review",
                             [{"role": "user", "content": "x"}], core.clock(), clock=core.clock)
    core.store.execute('UPDATE watch_ledger SET model_request_id=%s WHERE entry_id=%s',
                       (request, entry["entry_id"]))
    core.store.execute("UPDATE model_usage SET state='UNKNOWN' WHERE request_id=%s", (request,))
    core.clock.advance(watch_ledger.LEASE_SECONDS + 1)
    step(core)
    step(core)
    assert entries(core)[0]["state"] == "UNKNOWN" and model.calls == []


def test_daily_currency_amount_does_not_refuse_a_judgement(core, tmp_path):
    core.settings.watch_ledger = core.settings.watch_ledger.model_copy(
        update={})
    model = ready(core, tmp_path, json.dumps(JUDGEMENT))
    observe(core, 8)
    step(core)
    row = entries(core)[0]
    assert (row["state"], row["error_code"], row["model_request_id"]) == (
        "JUDGED", None, row["model_request_id"])
    assert row["model_request_id"] and len(model.calls) == 1


def test_disabled_ledger_does_nothing_and_keeps_the_paid_contract(core, tmp_path):
    configure(core)
    approved_live(core, tmp_path)
    before = paid_budget.sha(paid_budget.snapshot(core.settings))
    assert "watch_ledger" not in core.settings.model_dump()
    observe(core, 9)
    assert not step(core) and entries(core) == []
    enable(core)
    # Like rem, the ledger's own cap applies at reservation; the approved contract holds.
    assert paid_budget.sha(paid_budget.snapshot(core.settings)) == before


def test_erasing_the_source_removes_the_entry(core, tmp_path):
    ready(core, tmp_path, json.dumps(JUDGEMENT, ensure_ascii=False))
    event_id = observe(core, 10)
    step(core)
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [event_id], core.clock())
    assert entries(core) == []
    assert watch_ledger.recent(core.store) == []


def chat_job(core, text="最近看了什么有意思的"):
    event = InboundEvent.model_validate(normalized(raw_event(77, text=text)))
    core.ingest(event, event.event_id)
    return core.store.one((
        'SELECT j.*,e.body_json,e.conversation_id FROM jobs j JOIN events e '
        'USING(event_id) WHERE j.event_id=%s'
    ), (event.event_id,))


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("text", ["最近看了什么有意思的", "你来说点什么"])
def test_chat_cites_recent_judgements_only_when_enabled(core, enabled, text):
    configure(core)
    event_id = observe(core, 11)
    core.store.execute(
        (
            'INSERT INTO watch_ledger(entry_id,source_event_id,platform,item_'
            'ref,depth,state,rating,take,topic_tags_json,created_at,updated_a'
            "t) VALUES('watch_1',%s,'bilibili','u','text','JUDGED',1,%s,'[]',"
            '%s,%s)'
        ), (event_id, JUDGEMENT["take"], core.clock(), core.clock()))
    if enabled:
        enable(core)
    job = chat_job(core, text)
    messages = core.context(job)
    prompt = json.dumps(messages, ensure_ascii=False)
    assert (JUDGEMENT["take"] in prompt) is enabled
    assert "watch_obs_11" not in prompt
    if enabled:
        reference = next(item["content"] for item in messages
                         if JUDGEMENT["take"] in item["content"])
        assert json.loads(reference.split("\n", 1)[1])[0]["origin"] == (
            "来源未记录，不要说是你自己挑的")
        assert core.store.one('SELECT 1 FROM job_sources WHERE job_id=%s AND event_id=%s',
                              (job["job_id"], event_id))
    assert core.model.calls == []


def test_a_later_video_analysis_is_judged_again_but_a_shallower_read_is_not(core, tmp_path):
    later = {"rating": 2, "take": "看了画面才发现猫是自己钻进去的", "topic_tags": ["猫"]}
    model = ready(core, tmp_path, json.dumps(JUDGEMENT, ensure_ascii=False),
                  json.dumps(later, ensure_ascii=False))
    url = "https://www.bilibili.com/video/BV10000001aa"
    first = observe(core, 12, url=url)
    step(core)
    analysis = observe(core, 13, url=url, text="视频分析：猫自己跳进纸箱后睡着")
    core.store.execute(
        "UPDATE observations SET evidence_json=jsonb_set(evidence_json::jsonb,"
        "'{bili_video}',%s::jsonb)::text WHERE event_id=%s",
        (json.dumps({"request_id": "x"}), analysis),
    )
    observe(core, 14, url=url, text="又一次只读到正文")
    step(core)
    [row] = entries(core)
    assert (row["source_event_id"], row["depth"], row["state"], row["take"]) == (
        analysis, "media_sampled", "JUDGED", later["take"])
    assert len(model.calls) == 2
    step(core)
    assert len(model.calls) == 2 and entries(core)[0]["source_event_id"] != first


@pytest.mark.parametrize("kind", ["commitment", "self", None])
def test_recent_judgements_say_who_chose_the_item(core, kind):
    configure(core)
    event_id = observe(core, 12)
    core.store.execute(
        (
            'INSERT INTO watch_ledger(entry_id,source_event_id,platform,item_'
            'ref,depth,state,rating,take,topic_tags_json,created_at,updated_a'
            "t) VALUES('watch_2',%s,'bilibili','u','text','JUDGED',1,'t','[]'"
            ',%s,%s)'
        ), (event_id, core.clock(), core.clock()))
    if kind:
        columns = {row["name"] for row in core.store.rows(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='goals'"
        )}
        values = {"goal_id": "goal_watch", "kind": kind, "topic": "t", "scope_json": "{}",
                  "proposal_key": "k", "state": "DONE", "priority": 1, "version": 1,
                  "result_event_id": event_id, "next_due_at": core.clock(),
                  "created_at": core.clock(), "updated_at": core.clock(), "redacted": 0}
        values = {key: value for key, value in values.items() if key in columns}
        core.store.execute(f"INSERT INTO goals({','.join(values)}) VALUES "
                           f"({','.join('%s' for _ in values)})", tuple(values.values()))
    assert [item["origin"] for item in watch_ledger.recent(core.store)] == [
        watch_ledger.ORIGINS[kind]]
