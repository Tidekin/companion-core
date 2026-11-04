"""User-facing command and extraction flows, with the real pinned Chinese encoder.

Only DeepSeek responses are substituted here. scripts/verify_memory_live.py repeats
the workflows using the real provider and records replies, states, and usage.
"""

import json

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.model import MEMORY_HEADING
from tests.conftest import ingest, run


def message(core, number, text, origin="qq:FriendMessage:10001"):
    core.settings.memory_enabled = True
    ingest(core, number, text, origin=origin)
    assert run(core.run_once())
    return core.store.one("SELECT * FROM jobs WHERE event_id=?", (f"evt_test_{number}",))


def reply(core, number):
    row = core.store.one(
        "SELECT content_json FROM outbox WHERE origin_event_id=?", (f"evt_test_{number}",)
    )
    return json.loads(row["content_json"])["text"]


def relation(payload, *, same=False, conflict=False):
    ref = payload["existing"][0]["ref"]
    return json.dumps(
        {
            "relations": [
                {
                    "supported": True, "update_authorized": False, "claims_replacement": False,
                    "same": ref if same else None,
                    "conflicts": [ref] if conflict else [],
                    "replaces": [],
                }
            ]
        }
    )


@pytest.mark.parametrize(
    "question,answer",
    [
        ("我平时最爱喝什么？", "茉莉绿茶"),
        ("休息日一般做什么运动？", "周六上午去骑行"),
        ("我的猫叫什么？", "布丁"),
    ],
)
def test_paraphrased_question_in_new_session_with_old_fact_and_distractors(core, question, answer):
    facts = [
        "用户最喜欢的饮料是茉莉绿茶。",
        "用户通常在周六上午去骑行。",
        "用户的猫叫布丁。",
        "用户最喜欢的颜色是蓝色。",
        "用户的项目代号是银杏。",
        "用户的键盘是茶轴。",
        *[f"第{i}号项目的归档标签是编号{i:02}" for i in range(20)],
    ]
    for i, fact in enumerate(facts):
        assert message(core, i, "/记住这个：" + fact)["state"] == "SUCCEEDED"
    core.settings.max_recent_messages = 0
    assert message(core, 100, question, "new:FriendMessage:10001")["state"] == "SUCCEEDED"
    context = next(
        messages for messages, scenario in reversed(core.model.calls) if scenario == "chat"
    )
    references = [m for m in context if m["content"].startswith(MEMORY_HEADING + "\n")]
    assert len(references) == 1 and answer in references[0]["content"]
    assert len(core.memory.search(question)) <= 6
    assert core.memory.search("今天下午下雨了吗？") == []


def test_contradictory_remember_commands_mark_both_and_tell_owner(core):
    message(core, 1, "/记住这个：我喜欢绿茶")
    core.model.review = lambda payload: relation(payload, conflict=True)
    assert message(core, 2, "/记住这个：我不喜欢绿茶")["state"] == "SUCCEEDED"
    rows = core.store.rows("SELECT content,needs_review FROM memories")
    assert {r["content"] for r in rows} == {"我喜欢绿茶", "我不喜欢绿茶"}
    assert all(r["needs_review"] for r in rows)
    assert "冲突" in reply(core, 2)
    assert (
        core.store.one("SELECT COUNT(*) AS n FROM model_usage WHERE scenario='memory_review'")["n"]
        == 1
    )


def test_compatible_preferences_are_not_mistaken_for_conflicts(core):
    message(core, 1, "/记住这个：我喜欢绿茶")
    message(core, 2, "/记住这个：我喜欢红茶")
    assert core.store.one("SELECT COUNT(*) AS n FROM memories")["n"] == 2
    assert not any(
        row["needs_review"] for row in core.store.rows("SELECT needs_review FROM memories")
    )


@pytest.mark.parametrize("explicit", ["用户最喜欢的颜色是蓝色。", "我最喜欢的颜色是蓝色"])
def test_remember_promotes_extracted_candidate_and_keeps_id_and_both_evidence(core, explicit):
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    message(core, 1, "我最喜欢的颜色是蓝色")
    core.model.extraction = json.dumps(
        {
            "memories": [
                {
                    "subject": "user",
                    "kind": "preference",
                    "fact_key": "favorite_color",
                    "content": "用户最喜欢的颜色是蓝色。",
                    "evidence_event_id": "evt_test_1",
                    "evidence_quote": "我最喜欢的颜色是蓝色",
                }
            ]
        },
        ensure_ascii=False,
    )
    assert run(core.extract_once())
    original = core.store.one("SELECT * FROM memories")
    assert original["status"] == "HYPOTHESIS"
    core.model.review = lambda payload: relation(payload, same=True)
    message(core, 2, "/记住这个：" + explicit)
    rows = core.store.rows("SELECT * FROM memories")
    assert len(rows) == 1
    assert rows[0]["memory_id"] == original["memory_id"]
    assert rows[0]["status"] == "CONFIRMED" and rows[0]["confidence"] == 1
    assert rows[0]["origin"] == "user_confirmed"
    assert core.store.one("SELECT COUNT(*) AS n FROM memory_evidence")["n"] == 2
    assert "已记住" in reply(core, 2)


def test_invalid_reconciliation_cannot_confirm_or_modify_memory(core):
    message(core, 1, "/记住这个：我喜欢绿茶")
    core.model.review = lambda payload: '{"relations":[{"same":"m999","conflicts":[]}]}'
    job = message(core, 2, "/记住这个：我不喜欢绿茶")
    assert job["state"] == "RETRY_WAIT" and job["last_error_code"] == "MEMORY_REVIEW_INVALID"
    for _ in range(2):
        core.clock.advance(30)
        assert run(core.run_once())
    job = core.store.one("SELECT state,attempts FROM jobs WHERE event_id='evt_test_2'")
    assert job == {"state": "FAILED", "attempts": 3}
    assert core.store.one("SELECT COUNT(*) AS n FROM memories")["n"] == 1
    assert not core.store.one("SELECT needs_review FROM memories")["needs_review"]


def test_reconciliation_timeout_keeps_new_fact_uncommitted(core):
    message(core, 1, "/记住这个：我喜欢绿茶")
    core.model.error = CoreError("MODEL_RETRYABLE", 503)
    job = message(core, 2, "/记住这个：我不喜欢绿茶")
    assert job["state"] == "RETRY_WAIT"
    assert core.store.one("SELECT COUNT(*) AS n FROM memories")["n"] == 1
    assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"


def test_forgetting_during_reconciliation_cannot_reintroduce_old_fact(core):
    message(core, 1, "/记住这个：我喜欢绿茶")
    mid = core.store.one("SELECT memory_id FROM memories")["memory_id"]
    core.model.review = lambda payload: relation(payload, same=True)
    core.model.callback = lambda: core.memory.decide(mid, "forget", core.clock())
    job = message(core, 2, "/记住这个：我爱喝绿茶")
    assert job["state"] == "RETRY_WAIT" and job["last_error_code"] == "PRIVACY_CHANGED"
    assert core.store.one("SELECT COUNT(*) AS n FROM memories WHERE status!='FORGOTTEN'")["n"] == 0


def test_expired_job_cannot_write_memory_after_model_review(core):
    message(core, 1, "/记住这个：我喜欢绿茶")
    core.model.callback = lambda: core.clock.advance(121)
    message(core, 2, "/记住这个：我不喜欢绿茶")
    assert core.store.one("SELECT COUNT(*) AS n FROM memories")["n"] == 1


def test_same_extraction_batch_detects_conflicting_candidates(core):
    core.settings.extraction_min_events = 2
    core.settings.extraction_idle_seconds = 0
    for n, text in [(1, "我喜欢绿茶"), (2, "我不喜欢绿茶")]:
        message(core, n, text)
    core.model.extraction = json.dumps(
        {
            "memories": [
                {
                    "subject": "user",
                    "kind": "preference",
                    "fact_key": f"model-key-{n}",
                    "content": text,
                    "evidence_event_id": f"evt_test_{n}",
                    "evidence_quote": text,
                }
                for n, text in [(1, "我喜欢绿茶"), (2, "我不喜欢绿茶")]
            ]
        },
        ensure_ascii=False,
    )
    core.model.review = lambda payload: json.dumps(
        {
            "relations": [
                {"supported": True, "update_authorized": False, "claims_replacement": False,
                    "same": None, "conflicts": [], "replaces": []},
                {"supported": True, "update_authorized": False, "claims_replacement": False,
                    "same": None, "conflicts": ["c0"], "replaces": []},
            ]
        }
    )
    assert run(core.extract_once())
    assert all(row["needs_review"] for row in core.store.rows("SELECT needs_review FROM memories"))
    assert core.store.one("SELECT COUNT(*) AS n FROM memories")["n"] == 2


def test_aliased_review_references_cannot_hide_a_conflict_or_commit_partial_batch(core):
    message(core, 1, "/记住这个：我喜欢绿茶")
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    for n, text in [(2, "我爱喝绿茶"), (3, "我不喜欢绿茶")]:
        message(core, n, text)
    core.model.extraction = json.dumps(
        {
            "memories": [
                {
                    "subject": "user",
                    "kind": "preference",
                    "fact_key": "tea",
                    "content": text,
                    "evidence_event_id": f"evt_test_{n}",
                    "evidence_quote": text,
                }
                for n, text in [(2, "我爱喝绿茶"), (3, "我不喜欢绿茶")]
            ]
        },
        ensure_ascii=False,
    )
    core.model.review = lambda payload: json.dumps(
        {
            "relations": [
                {"supported": True, "update_authorized": False, "claims_replacement": False,
                    "same": "m0", "conflicts": [], "replaces": []},
                {"supported": True, "update_authorized": False, "claims_replacement": False,
                    "same": "c0", "conflicts": ["m0"], "replaces": []},
            ]
        }
    )
    assert not run(core.extract_once())
    assert core.store.one("SELECT COUNT(*) AS n FROM memories")["n"] == 1
    assert core.store.one("SELECT COUNT(*) AS n FROM memory_evidence")["n"] == 1
    assert core.store.one("SELECT needs_review FROM memories")["needs_review"] == 0


def test_semantic_index_is_erased_and_old_backup_cannot_restore_it(core, tmp_path):
    message(core, 1, "/记住这个：用户最喜欢的饮料是茉莉绿茶。")
    found = core.memory.search("我平时最爱喝什么？")
    assert found and core.store.one("SELECT COUNT(*) AS n FROM memory_vectors")["n"] == 1
    key = Fernet.generate_key()
    backup(core.settings.database, tmp_path / "before.enc", key)
    core.memory.decide(found[0]["memory_id"], "forget", core.clock())
    assert core.store.one("SELECT COUNT(*) AS n FROM memory_vectors")["n"] == 0
    destination = tmp_path / "restored.db"
    restore(tmp_path / "before.enc", destination, key, core.settings.erasure_ledger)
    reopened = Core(core.settings.model_copy(update={"database": str(destination)}), core.model)
    try:
        assert reopened.memory.search("我平时最爱喝什么？") == []
        assert reopened.store.one("SELECT COUNT(*) AS n FROM memory_vectors")["n"] == 0
    finally:
        reopened.store.close()


def test_similar_wording_does_not_replace_the_requested_attribute(core):
    message(core, 1, "/记住这个：用户最喜欢的饮料是茉莉绿茶。")
    message(core, 2, "/记住这个：用户最喜欢的颜色是蓝色。")
    hits = core.memory.search("我最喜欢什么颜色？")
    assert "蓝色" in hits[0]["content"]
    core.memory.decide(hits[0]["memory_id"], "forget", core.clock())
    assert all("蓝色" not in row["content"] for row in core.memory.search("我最喜欢什么颜色？"))


def test_irrelevant_candidates_are_removed_before_the_chat_call(core):
    message(core, 1, "/记住这个：用户最喜欢的饮料是茉莉绿茶。")
    core.settings.max_recent_messages = 0
    core.model.recall = lambda payload: '{"relevant":[]}'
    message(core, 2, "我最喜欢什么颜色？", "new:FriendMessage:10001")
    messages, scenario = core.model.calls[-1]
    assert scenario == "chat"
    assert "茉莉绿茶" not in json.dumps(messages, ensure_ascii=False)


@pytest.mark.parametrize("count", [10, 11])
def test_recall_selection_accepts_the_prompt_limit_and_rejects_overflow(core, monkeypatch, count):
    from tests.test_m1 import remember

    for number in range(count):
        remember(core, number, f"项目编号{number:02}偏好是蓝色", confirmed=True)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.settings.max_recent_messages = 0
    hits = core.memory.search("*", limit=count)
    assert len(hits) == count

    async def recall(*_args, **_kwargs):
        return {"rows": hits}

    monkeypatch.setattr(core.memory_context, "recall_async", recall)
    job = message(core, 100, "项目偏好", "new:FriendMessage:10001")
    assert [scenario for _, scenario in core.model.calls] == (
        ["memory_review", "chat"] if count == 10 else ["memory_review"])
    assert job["state"] == ("SUCCEEDED" if count == 10 else "RETRY_WAIT")
    if count > 10:
        assert job["last_error_code"] == "MEMORY_REVIEW_INVALID"
        assert core.store.one("SELECT 1 FROM outbox WHERE origin_event_id='evt_test_100'") is None


def test_memory_review_daily_limit_does_not_block_normal_chat(core):
    message(core, 1, "/记住这个：用户最喜欢的饮料是茉莉绿茶。")
    core.settings.memory_daily_micro_usd = 0
    core.settings.max_recent_messages = 0
    job = message(core, 2, "我平时最爱喝什么？", "new:FriendMessage:10001")
    assert job["state"] == "SUCCEEDED"
    messages, scenario = core.model.calls[-1]
    assert scenario == "chat" and "核对暂不可用" in json.dumps(messages, ensure_ascii=False)
    assert "茉莉绿茶" not in json.dumps(messages, ensure_ascii=False)


def test_unknown_recall_reference_is_rejected_before_chat(core):
    message(core, 1, "/记住这个：用户最喜欢的饮料是茉莉绿茶。")
    core.model.recall = lambda payload: '{"relevant":["m999"]}'
    job = message(core, 2, "我平时最爱喝什么？", "new:FriendMessage:10001")
    assert job["state"] == "RETRY_WAIT" and job["last_error_code"] == "MEMORY_REVIEW_INVALID"
    assert not any(scenario == "chat" for _, scenario in core.model.calls)
    assert core.store.one("SELECT 1 FROM outbox WHERE origin_event_id='evt_test_2'") is None


def test_memory_enabled_lease_covers_review_and_chat(core):
    from companion.config import Settings

    values = core.settings.model_dump()
    values.update(memory_enabled=True, timeout_seconds=45, lease_seconds=60)
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_existing_m1_database_keeps_facts_across_index_upgrade_and_downgrade(core, tmp_path):
    from contextlib import closing
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from companion.contracts import Candidate
    from companion.memory import Memory
    from companion.privacy import ErasureLedger
    from companion.store import Store, encode, migrate

    path = tmp_path / "previous.db"
    migrate(str(path), "0002_m1")
    settings = core.settings.model_copy(
        update={
            "database": str(path),
            "erasure_ledger": str(tmp_path / "previous.jsonl"),
            "memory_enabled": False,
        }
    )
    # Construct historical source/fact rows without invoking current Core job writes.
    with closing(Store(str(path))) as legacy:
        ledger = ErasureLedger(settings.erasure_ledger)
        ledger.initialize(legacy)
        now, content = core.clock(), "用户最喜欢的饮料是茉莉绿茶。"
        with legacy.transaction():
            legacy.insert("principals", actor_id="owner", kind="owner", created_at=now)
            legacy.insert("conversations", conversation_id="conv_legacy", platform="qq.onebot",
                          platform_session_key="qq:FriendMessage:10001", scope="private",
                          owner_actor_id="owner", created_at=now, last_event_at=now)
            legacy.insert("events", event_id="evt_legacy", schema_version=1, source="qq.onebot",
                          source_event_id="10002:qq:FriendMessage:10001:1",
                          conversation_id="conv_legacy", actor_id="owner",
                          event_type="message.received", occurred_at=now, ingested_at=now,
                          privacy_scope="private", body_json=encode({"text": content}),
                          trace_id="trace_legacy")
            mid = Memory(legacy, settings, ledger).add(Candidate(
                subject="user", kind="preference", fact_key="drink", content=content,
                evidence_event_id="evt_legacy", evidence_quote=content,
            ), now, confirmed=True)
    migrate(str(path), "0003_memory_index")
    settings.memory_enabled = True
    with closing(Store(str(path))) as indexed:
        memory = Memory(indexed, settings, ErasureLedger(settings.erasure_ledger))
        memory.index.warmup()
        assert memory.search("我平时最爱喝什么？")[0]["memory_id"] == mid
        assert indexed.one("SELECT COUNT(*) AS n FROM memory_vectors")["n"] == 1
    migration = Config()
    migration.set_main_option("script_location", str(Path("migrations").resolve()))
    migration.set_main_option("sqlalchemy.url", "sqlite:///" + path.as_posix())
    command.downgrade(migration, "0002_m1")
    store = Store(str(path))
    try:
        assert store.one("SELECT memory_id FROM memories")["memory_id"] == mid
        assert store.one("SELECT 1 FROM sqlite_master WHERE name='memory_vectors'") is None
    finally:
        store.close()
    migrate(str(path))
    reopened = Core(settings, core.model, core.clock)
    try:
        assert reopened.memory.search("我平时最爱喝什么？")[0]["memory_id"] == mid
    finally:
        reopened.store.close()
