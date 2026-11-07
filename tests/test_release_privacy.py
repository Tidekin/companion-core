"""Release gates for historical diary erasure and shared autonomy boundaries."""

import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet

from companion import budget
from companion.backup import backup, restore
from companion.contracts import CoreError, LeaseRequest
from companion.core import Core
from companion.model import ModelResponse
from companion.privacy import advance_epoch
from companion.store import after, encode, uid
from companion.transport import begin_send, enqueue, lease_deliveries
from tests.conftest import ingest, run
from tests.support.diary import DAY, generate, seed_day
from tests.support.life_topics import configure as configure_life
from tests.support.life_topics import proposal as life_proposal
from tests.support.observation_capture import enable_observations, observation


@pytest.mark.parametrize("day_alias", ["20261018", "2026-W42-7"])
@pytest.mark.parametrize("operation", ["read", "correct"])
def test_historical_diary_date_aliases_are_erased_live_and_after_restore(
    core, tmp_path, day_alias, operation
):
    seed_day(core)
    generate(core)
    correction = "这是该日期的主人修订内容"
    verb = "读日记" if operation == "read" else "修改日记"
    suffix = "" if operation == "read" else " " + correction
    ingest(core, 10, f"/{verb} {DAY}{suffix}")
    assert run(core.run_once())
    # Seed the exact pre-fix persisted form; rejecting new aliases cannot repair old backups.
    legacy_body = {
        "text": f"{verb} {day_alias}{suffix}",
        "media": [],
        "diary_command": True,
        "diary_correction": operation == "correct",
        "diary_timezone": core.settings.timezone,
    }
    core.store.execute(
        "UPDATE events SET body_json=%s WHERE event_id='evt_test_10'", (encode(legacy_body),)
    )
    archive, destination = tmp_path / "before-erasure.enc", tmp_path / "restored.db"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    core.diary.forget(DAY, "10001")
    restore(archive, destination, key, core.settings.erasure_ledger)
    restored = Core(
        core.settings.model_copy(update={"database": str(destination)}), clock=core.clock
    )
    try:
        copies = {}
        for label, instance in (("live", core), ("restored", restored)):
            assert instance.diary.get_day(DAY, "10001") is None
            copies[label] = {
                **instance.store.one(
                    "SELECT redacted,body_json FROM events WHERE event_id='evt_test_10'"
                ),
                **instance.store.one(
                    "SELECT content_json FROM outbox WHERE origin_event_id='evt_test_10'"
                ),
            }
        assert copies == {
            label: {"redacted": 1, "body_json": "{}", "content_json": "{}"}
            for label in ("live", "restored")
        }
    finally:
        restored.store.close()


def prepare_idle_conversation(core, text="今天计划读一本小说"):
    core.settings.proactive_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    ingest(core, text=text)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.clock.advance(core.settings.proactive_idle_seconds + 1)
    return core.store.one("SELECT conversation_id FROM events WHERE event_id='evt_test_1'")[
        "conversation_id"
    ]


def test_life_pause_prevents_new_timer_generation(core):
    prepare_due_life_topic(core)
    core.goals.pause_autonomy(True)
    assert core.life_topics.schedule() is None
    assert not run(core.run_once())
    assert core.model.calls == []
    assert core.store.rows("SELECT * FROM model_usage") == []
    assert core.store.rows("SELECT * FROM outbox") == []


def prepare_due_life_topic(core):
    configure_life(core)
    prepare_idle_conversation(core, "导出时还有一个未解决的Bug，今天先到这里")
    core.life_topics.synchronize()
    assert core.store.one("SELECT state FROM life_topics")["state"] == "WAITING"
    core.clock.advance(86400)


def test_life_pause_during_activity_model_prevents_new_proactive_output(core):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        prepare_due_life_topic(core)
        life_proposal(core)
        draft = core.model.activity

        class WaitingModel:
            async def complete(self, messages, kind):
                if kind == "activity":
                    started.set()
                    await release.wait()
                return ModelResponse(draft if kind == "activity" else "继续正常聊天。", 20, 20, 0)

        core.model = WaitingModel()
        job = core.life_topics.schedule()
        assert job is not None
        work = asyncio.create_task(core.run_once(job_id=job))
        try:
            await asyncio.wait_for(started.wait(), 2)
            core.goals.pause_autonomy(True)
        finally:
            release.set()
            await work
        usage = core.store.rows("SELECT state,billed_micro_usd FROM model_usage "
                               "WHERE scenario='activity'")
        assert len(usage) == 1 and usage[0]["state"] == "SETTLED"
        assert usage[0]["billed_micro_usd"] > 0
        assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (job,)) == {
            "state": "CANCELLED"}
        assert not core.store.one(
            "SELECT 1 FROM life_activity_results WHERE job_id=%s AND text<>''", (job,))
        assert not core.store.one(
            "SELECT 1 FROM outbox WHERE action_type='qq.private.proactive' "
            "AND state IN ('PENDING','LEASED','SENDING','RETRY_WAIT')"
        )
        ingest(core, 2, "继续正常聊天")
        assert await core.run_once()
        items = lease_deliveries(
            core.store, core.settings, LeaseRequest(worker_id="release-test"), core.clock()
        )["items"]
        assert len(items) == 1 and items[0]["action_type"] == "qq.private.reply"
        assert core.store.rows("SELECT state,billed_micro_usd FROM model_usage "
                               "WHERE scenario='activity'") == usage

    run(scenario())


def test_paused_pending_proactive_does_not_block_normal_reply(core):
    conversation = prepare_idle_conversation(core)
    core.goals.pause_autonomy(True)
    # A pre-fix worker or old snapshot can leave this row after the pause transaction.
    enqueue(
        core.store,
        "evt_test_1",
        conversation,
        "先前生成的主动消息",
        core.clock(),
        action="qq.private.proactive",
        key="release-test:legacy-proactive",
    )
    ingest(core, 2, "继续正常聊天")
    assert run(core.run_once())
    ordinary = core.store.one("SELECT message_id FROM outbox WHERE origin_event_id='evt_test_2'")
    items = []
    for _ in range(2):
        items.extend(
            lease_deliveries(
                core.store, core.settings, LeaseRequest(worker_id="release-test"), core.clock()
            )["items"]
        )
    assert [item["message_id"] for item in items] == [ordinary["message_id"]]


def test_affect_pause_prevents_timer_generation(core):
    core.settings.affect_enabled = True
    prepare_due_life_topic(core)
    ingest(core, 2, "先不聊了")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE event_id='evt_test_2'")
    assert core.affect.snapshot()["state"]["engagement"] == "pause"
    assert core.life_topics.schedule() is None
    assert core.model.calls == []
    assert core.store.rows("SELECT * FROM model_usage") == []
    assert core.store.rows("SELECT * FROM outbox") == []


def test_affect_pause_rechecked_after_proactive_delivery_lease(core):
    core.settings.affect_enabled = True
    conversation = prepare_idle_conversation(core)
    message = enqueue(
        core.store,
        "evt_test_1",
        conversation,
        "已排队的主动分享",
        core.clock(),
        action="qq.private.proactive",
        key="release-test:leased-proactive",
    )
    item = lease_deliveries(
        core.store, core.settings, LeaseRequest(worker_id="release-test"), core.clock()
    )["items"][0]
    assert item["message_id"] == message
    ingest(core, 2, "先不聊了")
    assert core.affect.snapshot()["state"]["engagement"] == "pause"
    with pytest.raises(CoreError, match="PROACTIVE_SEND_PAUSED"):
        begin_send(core.store, message, item["lease_token"], core.clock(), core.settings)


def observation_with_older_owner_source(core):
    core.settings.raw_event_retention_days = 2
    core.settings.diary_enabled = True
    core.settings.diary_min_source_events = 1
    enable_observations(core)
    ingest(core, text="当天记录了阅读计划")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.clock.advance(13 * 3600)
    item = observation(core, text="来源事件里的计划被再次记录", source_event_ids=["evt_test_1"])
    core.observations.append("reader", item)
    day = datetime.fromisoformat(core.clock()).astimezone(ZoneInfo(core.settings.timezone)).date()
    core.clock.advance(23 * 3600)
    return day, item.event_id


class ObservedDiaryModel:
    def __init__(self, callback=None):
        self.callback = callback

    async def complete(self, messages, scenario):
        if self.callback:
            self.callback()
        payload = json.loads(messages[-1]["content"])
        source = payload["sources"][0]
        return ModelResponse(
            encode(
                {
                    "schema_version": 1,
                    "local_date": payload["local_date"],
                    "title": "内容记录",
                    "worth_saving": True,
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
            0,
        )


def test_diary_read_expires_older_ancestors_without_background_worker(core):
    day, observed = observation_with_older_owner_source(core)
    core.diary.model = ObservedDiaryModel()
    core.diary.schedule_day(day)
    assert run(core.diary.generate_once("release-test"))
    assert core.diary.get_day(day, "10001")
    core.clock.advance(13 * 3600)
    cutoff = after(core.clock(), -2 * 86400)
    assert (
        core.store.one('SELECT ingested_at FROM events WHERE event_id=%s', (observed,))[
            "ingested_at"
        ]
        > cutoff
    )
    assert core.diary.get_day(day, "10001") is None
    assert core.store.one("SELECT redacted FROM events WHERE event_id='evt_test_1'")["redacted"]
    assert core.store.one('SELECT redacted FROM events WHERE event_id=%s', (observed,))["redacted"]


def test_diary_generation_discards_result_when_ancestor_expires_during_model(core):
    day, _ = observation_with_older_owner_source(core)
    core.clock.advance(12 * 3600 - 5)
    core.diary.model = ObservedDiaryModel(lambda: core.clock.advance(10))
    core.diary.schedule_day(day)
    assert run(core.diary.generate_once("release-test"))
    assert not core.store.one("SELECT 1 FROM diary_entries WHERE status='COMMITTED'")


def seed_completed_legacy_diary_erasure(core):
    """Create the old post-delete state directly, independent of the repaired code path."""
    seed_day(core)
    generate(core)
    other_day = DAY - timedelta(days=1)
    for number in range(11, 14):
        ingest(core, number, "独立日期讨论了天文阅读。", at=f"{other_day}T04:00:00+00:00")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.diary.schedule_day(other_day)
    assert run(core.diary.generate_once("historical-fixture"))
    core.diary.correct(other_day, "独立日期的主人修订仍应保留。", "10001")
    for number, day in ((20, DAY), (21, other_day)):
        ingest(core, number, f"/读日记 {day}")
        assert run(core.run_once())
    core.store.execute(
        (
            "UPDATE outbox SET state='SENT',sent_at=%s WHERE origin_event_id "
            "IN ('evt_test_20','evt_test_21')"
        ),
        (core.clock(),),
    )
    core.store.execute(
        "UPDATE events SET body_json=%s WHERE event_id='evt_test_20'",
        (
            encode(
                {
                    "text": "读日记 20261018",
                    "media": [],
                    "diary_command": True,
                    "diary_correction": False,
                    "diary_timezone": core.settings.timezone,
                }
            ),
        ),
    )
    request_id = budget.reserve(core.store, core.settings, None, "chat", [], core.clock())
    budget.settle(core.store, core.settings, request_id)
    independent = core.diary.get_day(other_day, "10001")["entry_id"]
    unchanged = {
        "entry": core.store.one('SELECT * FROM diary_entries WHERE entry_id=%s', (independent,)),
        "evidence": core.store.rows(
            'SELECT * FROM diary_evidence WHERE entry_id=%s ORDER BY source_id', (independent,)
        ),
        "revisions": core.store.rows(
            'SELECT * FROM diary_revisions WHERE entry_id=%s ORDER BY revision_id', (independent,)
        ),
        "event": core.store.one("SELECT * FROM events WHERE event_id='evt_test_21'"),
        "reply": core.store.one("SELECT * FROM outbox WHERE origin_event_id='evt_test_21'"),
        "usage": core.store.one('SELECT * FROM model_usage WHERE request_id=%s', (request_id,)),
    }
    assert unchanged["usage"]["state"] == "UNKNOWN"
    assert unchanged["usage"]["billed_micro_usd"] > 0
    record = {
        "seq": len(core.ledger.read()),
        "erasure_id": uid("erase"),
        "memory_id": f"companion:{core.settings.timezone}:{DAY}",
        "memory_ids": [],
        "event_ids": [],  # Old matching omitted the otherwise valid compact-date readback.
        "at": core.clock(),
        "target_type": "diary",
        "diary_day": DAY.isoformat(),
        "diary_timezone": core.settings.timezone,
    }
    with core.ledger.path.open("a", encoding="utf-8") as stream:
        stream.write(encode(record) + "\n")
    with core.store.transaction():
        target = core.store.one(
            'SELECT entry_id FROM diary_entries WHERE local_date=%s', (DAY.isoformat(),)
        )["entry_id"]
        core.store.execute(
            (
                'UPDATE diary_entries SET '
                "title='',body_md='',source_set_sha256='',status='DELETED' "
                'WHERE entry_id=%s'
            ),
            (target,),
        )
        for table in ("diary_evidence", "diary_revisions"):
            core.store.execute(f"DELETE FROM {table} WHERE entry_id=?", (target,))
        core.store.execute(
            (
                "UPDATE diary_runs SET state='CANCELLED',lease_token=NULL,las"
                "t_error_code='DIARY_DELETED' WHERE local_date=%s"
            ),
            (DAY.isoformat(),),
        )
        core.store.execute(
            'INSERT INTO runtime_meta VALUES (%s,%s)',
            (f"diary_deleted:{core.settings.timezone}:{DAY}", record["erasure_id"]),
        )
        core.store.execute(
            "INSERT INTO privacy_erasure_ledger VALUES (%s,%s,%s,%s,%s,'COMPLETE')",
            (record["erasure_id"], "diary", record["memory_id"], record["at"], record["seq"]),
        )
        advance_epoch(core.store)
    assert (
        core.store.one("SELECT redacted FROM events WHERE event_id='evt_test_20'")["redacted"] == 0
    )
    assert (
        core.store.one("SELECT content_json FROM outbox WHERE origin_event_id='evt_test_20'")[
            "content_json"
        ]
        != "{}"
    )
    return unchanged


def assert_legacy_copy_repaired_without_collateral_changes(instance, unchanged):
    assert instance.diary.get_day(DAY, "10001") is None
    assert instance.store.one(
        "SELECT redacted,body_json FROM events WHERE event_id='evt_test_20'"
    ) == {
        "redacted": 1,
        "body_json": "{}",
    }
    assert instance.store.one(
        "SELECT content_json,content_sha256 FROM outbox WHERE origin_event_id='evt_test_20'"
    ) == {"content_json": "{}", "content_sha256": ""}
    entry_id = unchanged["entry"]["entry_id"]
    assert (
        instance.store.one('SELECT * FROM diary_entries WHERE entry_id=%s', (entry_id,))
        == unchanged["entry"]
    )
    assert (
        instance.store.rows(
            'SELECT * FROM diary_evidence WHERE entry_id=%s ORDER BY source_id', (entry_id,)
        )
        == unchanged["evidence"]
    )
    assert (
        instance.store.rows(
            'SELECT * FROM diary_revisions WHERE entry_id=%s ORDER BY revision_id', (entry_id,)
        )
        == unchanged["revisions"]
    )
    assert (
        instance.store.one("SELECT * FROM events WHERE event_id='evt_test_21'")
        == unchanged["event"]
    )
    assert (
        instance.store.one("SELECT * FROM outbox WHERE origin_event_id='evt_test_21'")
        == unchanged["reply"]
    )
    assert (
        instance.store.one(
            'SELECT * FROM model_usage WHERE request_id=%s', (unchanged["usage"]["request_id"],)
        )
        == unchanged["usage"]
    )


@pytest.mark.parametrize("entrypoint", ["startup", "repeat_forget", "post_delete_backup"])
def test_already_completed_legacy_erasure_repairs_copies_idempotently(core, tmp_path, entrypoint):
    unchanged = seed_completed_legacy_diary_erasure(core)
    ledger_bytes = core.ledger.path.read_bytes()
    instance = core
    try:
        if entrypoint == "startup":
            instance = Core(core.settings, core.model, core.clock)
        elif entrypoint == "repeat_forget":
            core.diary.forget(DAY, "10001")
        else:
            archive, destination = tmp_path / "after-complete.enc", tmp_path / "recovered.db"
            key = Fernet.generate_key()
            backup(core.settings.database, archive, key)
            restore(archive, destination, key, core.settings.erasure_ledger)
            instance = Core(
                core.settings.model_copy(update={"database": str(destination)}), clock=core.clock
            )
        assert_legacy_copy_repaired_without_collateral_changes(instance, unchanged)
        repaired_epoch = instance.store.one(
            "SELECT value FROM runtime_meta WHERE key='privacy_epoch'"
        )
        instance.ledger.replay(instance.store)
        instance.diary.forget(DAY, "10001")
        assert_legacy_copy_repaired_without_collateral_changes(instance, unchanged)
        assert (
            instance.store.one("SELECT value FROM runtime_meta WHERE key='privacy_epoch'")
            == repaired_epoch
        )
        assert core.ledger.path.read_bytes() == ledger_bytes
    finally:
        if instance is not core:
            instance.store.close()


@pytest.mark.parametrize("command", ["/读日记", "/日记列表"])
def test_legacy_diary_repair_preserves_newer_independent_readbacks(core, tmp_path, command):
    seed_completed_legacy_diary_erasure(core)
    core.ledger.replay(core.store)
    core.clock.advance(60)
    ingest(core, 22, command)
    assert run(core.run_once())
    core.store.execute(
        "UPDATE outbox SET state='SENT',sent_at=%s WHERE origin_event_id='evt_test_22'",
        (core.clock(),),
    )
    expected_event = core.store.one("SELECT * FROM events WHERE event_id='evt_test_22'")
    expected_reply = core.store.one("SELECT * FROM outbox WHERE origin_event_id='evt_test_22'")
    text = json.loads(expected_reply["content_json"])["text"]
    assert str(DAY - timedelta(days=1)) in text and str(DAY) not in text
    assert expected_event["ingested_at"] > core.ledger.read()[1]["at"]

    def assert_newer_copy_unchanged(store):
        assert store.one("SELECT * FROM events WHERE event_id='evt_test_22'") == expected_event
        assert (
            store.one("SELECT * FROM outbox WHERE origin_event_id='evt_test_22'") == expected_reply
        )

    core.ledger.replay(core.store)
    assert_newer_copy_unchanged(core.store)
    core.diary.forget(DAY, "10001")
    assert_newer_copy_unchanged(core.store)
    archive, destination = tmp_path / "newer-readback.enc", tmp_path / "newer-restored.db"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    restore(archive, destination, key, core.settings.erasure_ledger)
    restored = Core(
        core.settings.model_copy(update={"database": str(destination)}), clock=core.clock
    )
    try:
        assert_newer_copy_unchanged(restored.store)
    finally:
        restored.store.close()
