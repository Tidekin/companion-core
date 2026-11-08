"""P9 synthetic prompt contracts; no provider or platform transport is used."""

import pytest
from cryptography.fernet import Fernet

from companion import budget
from companion.backup import backup, restore
from companion.contracts import Candidate, CoreError
from companion.core import Core
from companion.store import encode
from companion.token_measurement import measure_request
from companion.transport import enqueue
from tests.conftest import run
from tests.support.dialogue_context import current_job, sent_history


@pytest.mark.parametrize(
    "state",
    [
        None,
        "PENDING",
        "LEASED",
        "SENDING",
        "RETRY_WAIT",
        "FAILED",
        "CANCELLED",
        "DELIVERY_UNCERTAIN",
    ],
)
def test_history_requires_the_complete_sent_turn(core, state):
    sent_history(core, 1, "还未可靠送达的旧输入", state=state)
    messages = core.context(current_job(core))
    assert messages[1:] == [{"role": "user", "content": "刚才聊到哪了？"}]
    assert core.model.calls == []
    if state:
        assert core.store.one("SELECT state FROM outbox")["state"] == state


@pytest.mark.parametrize("question", ["现在说的这个呢？", "刚才聊到哪了？", "上次喝的还是那个吗？"])
def test_indirect_reference_keeps_the_sent_pair_in_the_actual_chat_prompt(core, question):
    sent_history(core, 1, "下午选了茉莉绿茶", "那就继续聊这杯茶。")
    current_job(core, text=question)
    assert run(core.run_once())
    assert len(core.model.calls) == 1
    messages, scenario = core.model.calls[0]
    assert scenario == "chat"
    assert messages[1:] == [
        {"role": "user", "content": "下午选了茉莉绿茶"},
        {"role": "assistant", "content": "那就继续聊这杯茶。"},
        {"role": "user", "content": question},
    ]
    assert core.store.rows("SELECT * FROM memories") == []


def test_budget_removes_the_whole_turn_instead_of_leaving_an_assistant(core):
    sent_history(core, 1, "长输入" * 500, "短回复")
    job = current_job(core)
    full = core.context(job)
    # Enough room for the old assistant alone, but not its associated input.
    core.settings.context_soft_tokens = budget.token_upper_bound([full[0], full[-2], full[-1]])
    messages = core.context(job)
    assert messages == [full[0], full[-1]]
    assert budget.token_upper_bound(messages) <= core.settings.context_soft_tokens
    assert core.model.calls == []


def test_budget_keeps_the_newest_complete_turn_and_complete_current_input(core):
    core.settings.recent_protected_turns = 1
    sent_history(core, 1, "第一轮的旧内容" * 100, "第一轮回复")
    sent_history(core, 2, "第二轮内容", "第二轮回复")
    job = current_job(core)
    full = core.context(job)
    core.settings.context_soft_tokens = measure_request(
        core.settings, [full[0], *full[-3:]]).reserved_input_tokens
    assert core.context(job) == [full[0], *full[-3:]]
    current = "当前完整输入" * 1000
    job = current_job(core, number=100, text=current)
    assert core.context(job) == [full[0], {"role": "user", "content": current}]


@pytest.mark.parametrize("max_messages,first_kept", [(0, 3), (1, 3), (2, 2), (3, 2), (4, 1)])
def test_recent_message_cap_counts_complete_turns(core, max_messages, first_kept):
    sent_history(core, 1, "第一轮", "第一答")
    sent_history(core, 2, "第二轮", "第二答")
    core.settings.max_recent_messages = max_messages
    messages = core.context(current_job(core))
    expected = [
        {"role": role, "content": text}
        for number, user, reply in [(1, "第一轮", "第一答"), (2, "第二轮", "第二答")]
        if number >= first_kept
        for role, text in [("user", user), ("assistant", reply)]
    ]
    assert messages[1:-1] == expected


@pytest.mark.parametrize("action", ["qq.private.proactive", "qzone.publish", "qzone.comment"])
def test_only_a_private_reply_can_complete_a_history_turn(core, action):
    sent_history(core, 1, "不是普通回复的输入")
    core.store.execute('UPDATE outbox SET action_type=%s', (action,))
    assert len(core.context(current_job(core))) == 2
    assert core.model.calls == []


@pytest.mark.parametrize(
    "excluded",
    [
        "other_session",
        "reply_session",
        "redacted",
        "diary",
        "control",
        "source",
        "event_type",
        "privacy_scope",
    ],
)
def test_history_stays_within_session_and_eligible_owner_events(core, excluded):
    sent_history(core, 1, "需要排除的旧输入")
    if excluded in {"other_session", "reply_session"}:
        sent_history(core, 2, "另一个会话", origin="other:FriendMessage:10001")
        other = core.store.one("SELECT conversation_id FROM events WHERE event_id='evt_test_2'")
        table = "events" if excluded == "other_session" else "outbox"
        key = "event_id" if table == "events" else "origin_event_id"
        core.store.execute(
            f"UPDATE {table} SET conversation_id=%s WHERE {key}='evt_test_1'",
            (other["conversation_id"],),
        )
    elif excluded == "redacted":
        core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_1'")
    elif excluded in {"source", "event_type", "privacy_scope"}:
        replacement = {
            "source": "owner.decision",
            "event_type": "memory.corrected",
            "privacy_scope": "internal",
        }[excluded]
        core.store.execute(
            f"UPDATE events SET {excluded}=%s WHERE event_id='evt_test_1'",
            (replacement,),
        )
    else:
        core.store.execute(
            "UPDATE events SET body_json=jsonb_set(body_json::jsonb,%s,'1')::text "
            "WHERE event_id='evt_test_1'",
            ([f"{excluded}_command"],),
        )
    assert len(core.context(current_job(core))) == 2
    assert core.model.calls == []


def test_current_and_future_inputs_are_not_history_even_if_they_have_a_sent_reply(core):
    sent_history(core, 1, "之前的输入", "之前的回复")
    job = current_job(core)
    message_id = enqueue(
        core.store,
        job["event_id"],
        job["conversation_id"],
        "当前的回复",
        core.clock(),
    )
    core.store.execute("UPDATE outbox SET state='SENT' WHERE message_id=%s", (message_id,))
    sent_history(core, 100, "后来的输入", "后来的回复")
    assert core.context(job)[1:-1] == [
        {"role": "user", "content": "之前的输入"},
        {"role": "assistant", "content": "之前的回复"},
    ]


def test_optional_reference_is_removed_as_a_block_before_recent_turns(core, monkeypatch):
    sent_history(core, 1, "之前的输入", "之前的回复")
    monkeypatch.setattr(core.life, "context", lambda: [{
        "topic": "可选目标资料" * 1000, "state": "COMPLETE", "commitment_state": None,
        "result_event_id": "evt_test_1",
    }])
    messages = core.context(current_job(core))
    assert messages[1:-1] == [
        {"role": "user", "content": "之前的输入"},
        {"role": "assistant", "content": "之前的回复"},
    ]
    assert budget.token_upper_bound(messages) <= core.settings.context_soft_tokens
    assert core.model.calls == []


def test_current_shared_content_survives_complete_history_trimming(core):
    from tests.support.content import enable
    from tests.support.content_integration import finish_read, share

    sent_history(core, 1, "很长的历史输入" * 300, "很长的历史回复" * 300)
    enable(core)
    share(core, 99, "你怎么看这个观点？")
    result = finish_read(core, "当前文章的可靠内容" * 600)
    job = core.claim_job("synthetic-p9")
    messages = core.context(job)
    assert len(messages) == 3
    assert result["event_ids"][0] in messages[-2]["content"]
    assert "当前文章的可靠内容" in messages[-2]["content"]
    assert messages[-1] == {"role": "user", "content": "你怎么看这个观点？"}
    assert budget.token_upper_bound(messages) <= core.settings.context_soft_tokens
    assert core.model.calls == []


def test_one_input_with_multiple_sent_rows_is_only_one_turn(core):
    sent_history(core, 1, "唯一旧输入", "较早回复")
    row = core.store.one("SELECT * FROM events WHERE event_id='evt_test_1'")
    message_id = enqueue(
        core.store,
        row["event_id"],
        row["conversation_id"],
        "较新已送达回复",
        core.clock(),
        key="synthetic-second-reply",
    )
    core.store.execute("UPDATE outbox SET state='SENT' WHERE message_id=%s", (message_id,))
    messages = core.context(current_job(core))
    assert messages[1:-1] == [
        {"role": "user", "content": "唯一旧输入"},
        {"role": "assistant", "content": "较新已送达回复"},
    ]


@pytest.mark.parametrize("user,reply", [("", "回复"), ("输入", "")])
def test_empty_text_cannot_form_a_partial_turn_or_consume_the_turn_cap(core, user, reply):
    sent_history(core, 1, "完整旧输入", "完整旧回复")
    sent_history(core, 2, user, reply)
    core.settings.max_recent_messages = 2
    messages = core.context(current_job(core))
    assert messages[1:-1] == [
        {"role": "user", "content": "完整旧输入"},
        {"role": "assistant", "content": "完整旧回复"},
    ]


@pytest.mark.parametrize("decision", ["correct", "forget"])
def test_sent_history_does_not_revive_after_memory_decision_and_reopen(core, decision):
    core.settings.memory_enabled = True
    sent_history(core, 1, "旧事实只喝绿茶", "我会参考这个偏好。")
    memory_id = core.memory.add(
        Candidate(
            subject="user",
            kind="preference",
            fact_key="茶",
            content="旧事实只喝绿茶",
            evidence_event_id="evt_test_1",
            evidence_quote="旧事实只喝绿茶",
        ),
        core.clock(),
        confirmed=True,
    )
    job = current_job(core)
    assert "旧事实只喝绿茶" in encode(core.context(job, memories=[]))
    core.memory.decide(memory_id, decision, core.clock(), "新事实改喝红茶")
    if decision == "forget":
        with pytest.raises(CoreError, match="REPLY_SOURCE_RECALLED"):
            core.context(job, memories=[])
        independent_job = current_job(core, number=100, text="新的独立话题")
    else:
        independent_job = job
    assert "旧事实只喝绿茶" not in encode(core.context(independent_job, memories=[]))
    reopened = Core(
        core.settings.model_copy(update={"memory_enabled": False}),
        core.model,
        core.clock,
    )
    try:
        if decision == "forget":
            with pytest.raises(CoreError, match="REPLY_SOURCE_RECALLED"):
                reopened.context(job)
        messages = reopened.context(independent_job)
        assert len(messages) == 2
        assert "旧事实只喝绿茶" not in encode(messages)
        assert core.model.calls == []
    finally:
        reopened.close()


def test_old_sent_turn_is_erased_when_restoring_an_old_backup(empty_database, core, tmp_path):
    core.settings.memory_enabled = True
    sent_history(core, 1, "要遗忘的送达事实", "要遗忘的回复")
    memory_id = core.memory.add(
        Candidate(
            subject="user",
            kind="fact",
            fact_key="事实",
            content="要遗忘的送达事实",
            evidence_event_id="evt_test_1",
            evidence_quote="要遗忘的送达事实",
        ),
        core.clock(),
        confirmed=True,
    )
    job = current_job(core)
    key = Fernet.generate_key()
    archive = tmp_path / "before.enc"
    backup(core.settings.database, archive, key)
    core.memory.decide(memory_id, "forget", core.clock())
    restored_state = tmp_path / "restored-state"
    restore(archive, empty_database, key, core.settings.erasure_ledger,
            state_directory=restored_state)
    reopened = Core(
        core.settings.model_copy(update={
            "database": empty_database, "state_directory": str(restored_state),
            'memory_enabled': False,
        }),
        core.model,
        core.clock,
    )
    try:
        assert len(reopened.context(job)) == 2
        assert core.model.calls == []
    finally:
        reopened.close()
