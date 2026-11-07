import asyncio
import json
from datetime import datetime

import pytest
from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.model import ModelResponse
from companion.store import after, encode
from tests.conftest import ingest, run
from tests.support.consolidation import enable_nightly
from tests.support.diary import DAY, generate
from tests.support.observation_capture import enable_observations, observation


def test_nightly_off_and_empty_day_skip_without_model(core):
    assert core.consolidation.schedule_due() == []
    assert not run(core.consolidation.run_once())
    enable_nightly(core, seed=False)
    core.consolidation.schedule_day(DAY)
    assert run(core.consolidation.run_once())
    assert core.consolidation.status(DAY)["state"] == "SKIPPED"
    assert core.model.calls == []


def test_nightly_one_day_one_diary_one_reflection_and_hypothesis_only(core):
    model = enable_nightly(core)
    generate(core)  # The independent Diary Worker already finished this date.
    identifiers = {core.consolidation.schedule_day(DAY) for _ in range(100)}
    assert len(identifiers) == 1
    assert run(core.consolidation.run_once())
    assert not run(core.consolidation.run_once())
    assert core.consolidation.status(DAY)["state"] == "COMPLETE"
    assert [scenario for _, scenario in model.calls].count("diary") == 1
    assert [scenario for _, scenario in model.calls].count("reflection") == 1
    suggestion = core.reflections.recent()[0]
    core.reflections.decide(suggestion["suggestion_id"], "ACCEPT_AS_HYPOTHESIS")
    assert (
        core.store.rows("SELECT * FROM memories") == core.store.rows("SELECT * FROM traits") == []
    )
    assert core.store.rows("SELECT * FROM outbox") == []


@pytest.mark.parametrize("forget_after", [False, True])
def test_nightly_own_memory_commit_can_advance_to_diary_and_reflection(
    core, monkeypatch, forget_after
):
    model = enable_nightly(core)
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 3
    core.settings.extraction_idle_seconds = 0
    model.extraction = encode(
        {
            "memories": [
                {
                    "subject": "user",
                    "kind": "preference",
                    "fact_key": "reading-plan",
                    "content": "我喜欢先列出章节再开始阅读。",
                    "evidence_event_id": "evt_test_3",
                    "evidence_quote": "我喜欢先列出章节再开始阅读。",
                }
            ]
        }
    )
    original = core.extract_step

    async def extract_then_forget(day, **kwargs):
        result = await original(day, **kwargs)
        from companion.memory_work import checkpoint_state

        if checkpoint_state(core.store)["batch"].get("phase") == "COMPLETE":
            with core.store.transaction():
                core.ledger.expire_events(core.store, ["evt_test_2"], core.clock())
        return result

    if forget_after:
        monkeypatch.setattr(core.consolidation, "extract_step", extract_then_forget)
    core.consolidation.schedule_day(DAY)
    assert run(core.consolidation.run_once())
    assert core.consolidation.status(DAY)["state"] == ("CANCELLED" if forget_after else "COMPLETE")
    assert len(core.store.rows("SELECT * FROM memories")) == 1
    assert len(core.reflections.recent()) == (0 if forget_after else 1)


def test_nightly_crash_resume_reuses_completed_diary(core, monkeypatch):
    model = enable_nightly(core)
    core.consolidation.schedule_day(DAY)

    async def crash(day):
        raise SystemExit("simulated process death")

    monkeypatch.setattr(core.reflections, "run_day", crash)
    with pytest.raises(SystemExit):
        run(core.consolidation.run_once())
    core.clock.advance(core.settings.lease_seconds + 1)
    restarted = Core(core.settings, model, core.clock)
    try:
        assert run(restarted.consolidation.run_once())
        assert restarted.consolidation.status(DAY)["state"] == "COMPLETE"
        assert [scenario for _, scenario in model.calls].count("diary") == 1
        assert [scenario for _, scenario in model.calls].count("reflection") == 1
    finally:
        restarted.store.close()


def test_nightly_resume_after_memory_commit_before_checkpoint(core, monkeypatch):
    model = enable_nightly(core)
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 3
    core.settings.extraction_idle_seconds = 0
    model.extraction = encode(
        {
            "memories": [
                {
                    "subject": "user",
                    "kind": "preference",
                    "fact_key": "reading-plan",
                    "content": "我喜欢先列出章节再开始阅读。",
                    "evidence_event_id": "evt_test_3",
                    "evidence_quote": "我喜欢先列出章节再开始阅读。",
                }
            ]
        }
    )

    def crash(run):
        raise SystemExit("crash after committed Memory write")

    monkeypatch.setattr(core.consolidation, "after_memory_commit", crash)
    core.consolidation.schedule_day(DAY)
    with pytest.raises(SystemExit):
        run(core.consolidation.run_once())
    core.clock.advance(core.settings.lease_seconds + 1)
    restarted = Core(core.settings, model, core.clock)
    try:
        assert run(restarted.consolidation.run_once())
        assert restarted.consolidation.status(DAY)["state"] == "COMPLETE"
        assert [scenario for _, scenario in model.calls].count("memory_extract") == 1
        assert len(core.store.rows("SELECT * FROM memories")) == 1
    finally:
        restarted.store.close()


def test_nightly_waiting_model_does_not_block_chat_or_allow_second_lease(core):
    original = enable_nightly(core)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class WaitingModel:
            async def complete(self, messages, kind):
                if kind == "reflection":
                    entered.set()
                    await release.wait()
                return await original.complete(messages, kind)

        core.reflections.model = WaitingModel()
        core.consolidation.schedule_day(DAY)
        task = asyncio.create_task(core.consolidation.run_once())
        await asyncio.wait_for(entered.wait(), 5)
        second = Core(core.settings, original, core.clock)
        try:
            assert not await second.consolidation.run_once()
            ingest(second, 100, "现在继续正常聊天")
            assert await second.run_once()
            assert (
                second.store.one("SELECT state FROM jobs WHERE event_id='evt_test_100'")["state"]
                == "SUCCEEDED"
            )
        finally:
            second.store.close()
            release.set()
        await task

    run(scenario())
    assert core.consolidation.status(DAY)["state"] == "COMPLETE"


def test_nightly_source_erasure_during_model_discards_result_and_restored_projections(
    core, tmp_path
):
    model = enable_nightly(core)

    def forget():
        with core.store.transaction():
            core.ledger.expire_events(core.store, ["evt_test_2"], core.clock())

    model.reflection_callback = forget
    core.consolidation.schedule_day(DAY)
    assert run(core.consolidation.run_once())
    assert core.consolidation.status(DAY)["state"] == "CANCELLED"
    assert not core.reflections.recent()
    assert not core.diary.get_day(DAY, "10001")


def test_reflection_full_provenance_erasure_after_commit_and_restore(core, tmp_path):
    enable_nightly(core)
    assert run(core.reflections.run_day(DAY))["state"] == "COMMITTED"
    key, archive, dest = (
        Fernet.generate_key(),
        tmp_path / "reflection.enc",
        tmp_path / "restored.db",
    )
    backup(core.settings.database, archive, key)
    with core.store.transaction():
        core.ledger.expire_events(
            core.store, ["evt_test_3"], core.clock()
        )  # Unquoted input still counts.
    restore(archive, dest, key, core.settings.erasure_ledger)
    restored = Core(core.settings.model_copy(update={"database": str(dest)}), clock=core.clock)
    try:
        for instance in (core, restored):
            assert instance.reflections.recent() == []
            row = instance.store.one("SELECT * FROM reflection_suggestions")
            assert row["content"] == "" and row["evidence_json"] == "[]"
            assert run(instance.reflections.run_day(DAY))["state"] == "CANCELLED"
    finally:
        restored.store.close()


@pytest.mark.parametrize(
    "defect,code",
    [
        ("quote", "INVALID_EVIDENCE"),
        ("fact", "UNSUPPORTED_FACT"),
        ("activity", "UNSUPPORTED_ACTIVITY"),
    ],
)
def test_reflection_invalid_evidence_and_unsupported_activity_rejected(core, defect, code):
    model = enable_nightly(core)

    def damage(draft):
        item = draft["suggestions"][0]
        if defect == "quote":
            item["evidence"][0]["exact_quote"] = "凭空编造的原话"
        elif defect == "fact":
            item["kind"] = "observation"
        else:
            item["text"] = "我今天去了公园。"

    model.reflection_transform = damage
    result = run(core.reflections.run_day(DAY))
    assert result["state"] == "FAILED" and code in result["last_error_code"]
    assert not core.reflections.recent()


@pytest.mark.parametrize("limit", ["disabled", "day", "month", "calls", "unknown"])
def test_reflection_budget_is_independent_and_hard_bounded(core, limit):
    model = enable_nightly(core)
    if limit == "disabled":
        core.settings.reflection_daily_micro_usd = 0
    elif limit == "day":
        core.settings.reflection_daily_micro_usd = 1
    elif limit == "month":
        core.settings.reflection_monthly_micro_usd = 1
    elif limit == "unknown":
        core.settings.input_rate = None
    else:
        from companion import budget

        budget.reserve(core.store, core.settings, None, "reflection", [], core.clock())
    result = run(core.reflections.run_day(DAY))
    expected = "RETRY_WAIT" if limit == "calls" else "FAILED"
    assert result["state"] == expected
    assert not model.calls
    assert (
        run(core.reflections.run_day(DAY))["state"] == expected
    )  # Repeated ticks do not call again.


def test_reflection_unknown_usage_and_expired_lease_preserve_reservation(core):
    model = enable_nightly(core)
    model.reflection_error = CoreError("MODEL_RETRYABLE", 503)
    assert run(core.reflections.run_day(DAY))["state"] == "FAILED"
    usage = core.store.one("SELECT * FROM model_usage WHERE scenario='reflection'")
    assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0
    assert len(model.calls) == 1


def test_shared_memory_lease_prevents_parallel_extraction_and_stale_commit(core):
    model = enable_nightly(core)
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 3
    core.settings.extraction_idle_seconds = 0
    lease = encode({"token": "other-process", "until": after(core.clock(), 60)})
    core.store.execute("INSERT INTO runtime_meta VALUES ('memory_extraction_lease',%s)", (lease,))
    assert not run(core.extract_once(DAY))
    assert not model.calls
    core.clock.advance(61)
    model.callback = lambda: core.clock.advance(core.settings.lease_seconds + 1)
    assert not run(core.extract_once(DAY))
    assert core.store.rows("SELECT * FROM episodes") == []
    model.callback = None
    assert run(core.extract_once(DAY))
    assert len(core.store.rows("SELECT * FROM episodes")) == 1


@pytest.mark.parametrize(
    "now,expected",
    [("2026-03-09T07:30:00+00:00", "2026-03-08"), ("2028-03-01T08:30:00+00:00", "2028-02-29")],
)
def test_nightly_calendar_dst_and_leap_day(core, now, expected):
    core.settings.timezone = "America/New_York"
    core.settings.consolidation_enabled = True
    core.clock.now = datetime.fromisoformat(now)
    core.consolidation.schedule_due()
    assert (
        core.store.one("SELECT MAX(local_date) AS day FROM consolidation_runs")["day"] == expected
    )


def test_observed_content_can_be_diary_evidence_without_pretending_it_was_watched(core):
    enable_observations(core)
    core.settings.diary_enabled = True
    core.settings.diary_min_source_events = 1
    for i in range(3):
        text = f"独立游戏的开发记录{i}"
        core.observations.append(
            "reader",
            observation(
                core,
                i,
                text=text,
                source_kind="title",
                result_status="DISCOVERED",
                occurred_at="2026-10-18T04:00:00Z",
                payload={"title": text, "text": text},
            ),
        )

    core.observations.append(
        "reader", observation(
            core, 99, text="实际取得的开发记录", occurred_at="2026-10-18T04:00:00Z",
        ),
    )

    class ObservedDiary:
        async def complete(self, messages, kind):
            source = json.loads(messages[-1]["content"])["sources"][0]
            return ModelResponse(
                encode(
                    {
                        "schema_version": 1,
                        "worth_saving": True,
                        "local_date": DAY.isoformat(),
                        "title": "内容发现",
                        "sections": [
                            {
                                "kind": "observation",
                                "text": source["text"],
                                "evidence": [
                                    {
                                        "source_id": source["source_id"],
                                        "exact_quote": source["text"],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                100,
                30,
            )

    core.diary.model = ObservedDiary()
    assert generate(core)["state"] == "COMMITTED"
    body = core.diary.get_day(DAY, "10001")["body_md"]
    assert "发现的标题" in body and "我回复" not in body and "看完" not in body
    assert len(core.store.rows("SELECT * FROM diary_evidence")) == 4
