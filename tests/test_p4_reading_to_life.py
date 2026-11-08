"""Seven simulated days, synthetic Reader receipts and transport acknowledgements only.

These tests exercise the existing life services, not real platforms, model quality,
semantic-dedupe quality, QQ delivery or twenty real diary dates reviewed by an owner.
"""

import hashlib
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.config import ObservationAdapterConfig
from companion.contracts import CoreError, DeliveryResult, LeaseRequest
from companion.core import Core
from companion.exploration import ReadItem, ReadResult
from companion.model import ModelResponse
from companion.observation import ArticleEvidence, ReadingEvidence, ReadingSource, SubtitleSpan
from companion.social import proactive_counts
from companion.source_grants import SourceGrantRegistration, SourceGrantSnapshot
from companion.store import encode
from companion.transport import begin_send, delivery_result, lease_deliveries
from tests.conftest import FakeModel, ingest, run


class GroundedDiaryModel(FakeModel):
    async def complete(self, messages, scenario):
        if scenario != "diary":
            return await super().complete(messages, scenario)
        self.calls.append((messages, scenario))
        payload = json.loads(messages[-1]["content"])
        source = next(item for item in payload["sources"] if item.get("result_status") == "FETCHED")
        return ModelResponse(
            encode(
                {
                    "schema_version": 1,
                    "local_date": payload["local_date"],
                    "title": "今天的阅读记录",
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
                        },
                        {
                            "kind": "question",
                            "text": "之后可以继续了解这个话题。",
                            "evidence": [{"source_id": source["source_id"], "exact_quote": ""}],
                        },
                    ],
                }
            ),
            140,
            80,
            20,
        )


def acknowledge_one(core):
    """Settle one local Outbox item without invoking a QQ transport."""
    items = lease_deliveries(
        core.store, core.settings, LeaseRequest(worker_id="p4-simulated-transport"), core.clock()
    )["items"]
    assert len(items) == 1
    item = items[0]
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock(), core.settings)
    delivery_result(
        core.store,
        core.settings,
        item["message_id"],
        DeliveryResult(lease_token=item["lease_token"], result="SENT"),
        core.clock(),
    )
    return item


def read_item(core, adapter, day, url):
    text = f"第{day + 1}份独立材料介绍了地图、谜题和探索路线的设计。"
    evidence = {}
    if adapter == "heybox":
        evidence["article"] = ArticleEvidence(
            method="source_api",
            fetched_at=datetime.fromisoformat(core.clock()),
            response_sha256=hashlib.sha256(text.encode()).hexdigest(),
            response_bytes=1200,
        )
    else:
        # The synthetic L2 receipt covers only these two non-contiguous spans.
        evidence["reading"] = ReadingEvidence(
            level="L2",
            method="account_api",
            fetched_at=datetime.fromisoformat(core.clock()),
            subtitle_status="available",
            spans=[
                SubtitleSpan(start_seconds=3, end_seconds=5),
                SubtitleSpan(start_seconds=12, end_seconds=15),
            ],
            sources=[
                ReadingSource(
                    role="subtitle",
                    url=f"https://aisubtitle.hdslb.com/bfs/subtitle/p4-{day}.json",
                    sha256=hashlib.sha256(text.encode()).hexdigest(),
                    bytes=1200,
                )
            ],
        )
    return ReadItem(title=f"探索设计材料{day + 1}", text=text, url=url, **evidence)


@pytest.mark.parametrize("chain", ["heybox", "bili", "cross-platform"])
def test_seven_simulated_days_read_interest_diary_share_and_erasure(
    empty_database, core, tmp_path, monkeypatch, chain
):
    cfg = core.settings
    cfg.observation_enabled = cfg.goals_enabled = cfg.exploration_enabled = True
    cfg.wake_enabled = cfg.social_enabled = cfg.proactive_enabled = True
    cfg.consolidation_enabled = cfg.diary_enabled = True
    cfg.diary_min_source_events = 2
    cfg.exploration_daily_requests = 8
    cfg.exploration_monthly_requests = 100
    cfg.exploration_daily_bytes = 8 * cfg.exploration_max_response_bytes
    cfg.proactive_idle_seconds = 0
    cfg.proactive_daily_limit = 1
    cfg.quiet_start = cfg.quiet_end = 0
    sources = ["heybox", "bili"] if chain == "cross-platform" else [chain]
    cfg.observation_adapters = {
        name: ObservationAdapterConfig(
            source="bilibili" if name == "bili" else "heybox",
            token=("synthetic-" + name + "-reader-") * 3,
        )
        for name in sources
    }
    # Avoid downloading embeddings: this suite tests orchestration and exact
    # duplicate suppression, while semantic similarity has dedicated coverage.
    monkeypatch.setattr("companion.social.semantic_duplicate", lambda *args: False)
    diary_model = GroundedDiaryModel()
    core.diary.model = diary_model
    active, started = core, core.clock.now
    grants = {}
    for name in sources:
        snapshot = SourceGrantSnapshot(
            grant_id="grant_" + ("1" if name == "heybox" else "2") * 32,
            epoch=1,
            fingerprint=("a" if name == "heybox" else "b") * 64,
            expires_at=started + timedelta(days=8),
        )
        core.observations.grants.register(
            SourceGrantRegistration(adapter_id=name, **snapshot.snapshot())
        )
        cfg.observation_adapters[name].source_grant_required = True
        grants[name] = snapshot
    records = []
    try:
        for day in range(7):
            active.clock.now = started + timedelta(days=day)
            local_day = active.clock.now.astimezone(ZoneInfo(cfg.timezone)).date()
            decisions = active.wake.evaluate()
            assert len(decisions) == 1 and decisions[0]["decision"] == "OBSERVE"
            assert active.wake.evaluate() == []
            active.exploration.schedule_wake(decisions)
            adapter = sources[day % len(sources)]
            url = (
                f"https://www.bilibili.com/video/BV1xx411c7m{day}"
                if adapter == "bili"
                else f"https://www.xiaoheihe.cn/app/bbs/link/{1000 + day}"
            )
            lease = active.exploration.lease(adapter, grants[adapter])
            assert lease and lease["operation"] == "discover"
            discovered = active.exploration.complete(
                adapter,
                lease["request_id"],
                ReadResult(
                    lease_token=lease["lease_token"],
                    status="SUCCESS",
                    used_bytes=500,
                    source_grant=grants[adapter],
                    items=[ReadItem(title=f"探索设计材料{day + 1}", topic="探索设计", url=url)],
                ),
            )["event_ids"][0]
            if day == 0:
                assert not active.store.rows("SELECT * FROM traits")
            active.clock.advance(cfg.wake_min_interval_minutes * 60)
            decisions = active.wake.evaluate()
            assert len(decisions) == 1 and decisions[0]["decision"] == "EXPLORE"
            goal_id = decisions[0]["goal_id"]
            assert active.exploration.schedule_goal()
            lease = active.exploration.lease(adapter, grants[adapter])
            item = read_item(active, adapter, day, url)
            receipt = ReadResult(
                lease_token=lease["lease_token"],
                status="SUCCESS",
                used_bytes=1200,
                source_grant=grants[adapter],
                items=[item],
            )
            completed = active.exploration.complete(adapter, lease["request_id"], receipt)
            assert active.exploration.complete(adapter, lease["request_id"], receipt)["duplicate"]
            fetched = completed["event_ids"][0]
            assert active.goals.get(goal_id)["state"] == "DONE"
            assert (
                active.store.one(
                    'SELECT source_event_id FROM event_dependencies WHERE event_id=%s', (fetched,)
                )["source_event_id"]
                == discovered
            )
            trait = active.store.one("SELECT * FROM traits WHERE category='interest'")
            assert trait["independent_days"] == day + 1
            assert trait["independent_sessions"] == day + 1
            assert (trait["status"] == "STABLE") == (day == 6)
            assert not active.store.rows("SELECT * FROM memories WHERE subject='user'")

            cfg.quiet_start, cfg.quiet_end = 0, 23
            assert not active.social.flush()
            cfg.quiet_start = cfg.quiet_end = 0
            active.goals.pause_autonomy(True)
            assert not active.social.flush()
            active.goals.pause_autonomy(False)
            ingest(active, day + 1, "继续聊今天的计划")
            assert not active.social.flush()  # Ordinary chat owns the next turn.
            assert run(active.run_once("p4-simulated-chat"))
            assert acknowledge_one(active)["action_type"] == "qq.private.reply"
            assert active.social.flush()
            job = active.store.one(
                "SELECT job_id FROM jobs WHERE kind='activity' AND state='QUEUED'")
            assert job and run(active.run_once("p4-share-wording", job_id=job["job_id"]))
            assert not active.social.flush()
            # Social wording and native Life delivery use this same daily counter.
            count, _ = proactive_counts(active.store, cfg, active.clock())
            assert count == cfg.proactive_daily_limit
            message = active.store.one(
                (
                    'SELECT o.* FROM outbox o JOIN social_candidates c ON '
                    'c.message_id=o.message_id WHERE c.event_id=%s'
                ), (fetched,),
            )
            assert message["action_type"] == "qq.private.proactive"
            assert json.loads(message["content_json"])["text"] == core.model.activity
            assert any(json.loads(messages[-1]["content"]).get("text") == item.text
                       for messages, scenario in core.model.calls if scenario == "activity")
            assert "看完" not in message["content_json"]
            if day < 6:
                assert acknowledge_one(active)["message_id"] == message["message_id"]
                assert acknowledge_one(active)["content"]["text"] == item.url

            active.clock.now = started + timedelta(days=day + 1)
            active.consolidation.schedule_day(local_day)
            assert run(active.consolidation.run_once("p4-simulated-life"))
            assert active.consolidation.status(local_day)["state"] == "COMPLETE"
            entry = active.diary.get_day(local_day, cfg.owner_qq_id)
            assert entry is not None
            assert "获取的来源内容" in entry["body_md"] and item.text in entry["body_md"]
            assert "还想知道" in entry["body_md"]
            records.append((discovered, fetched, goal_id, message["message_id"], local_day))
            if day == 2:
                active = Core(cfg, core.model, core.clock)
                active.diary.model = diary_model
                assert active.store.one("SELECT COUNT(*) n FROM traits")["n"] == 1
                assert (
                    active.store.one(
                        "SELECT COUNT(*) n FROM wake_decisions WHERE decision<>'SKIP'"
                    )["n"]
                    == 6
                )

        assert len(diary_model.calls) == 7
        assert len(active.store.rows("SELECT * FROM diary_entries WHERE status='COMMITTED'")) == 7
        key, archive = Fernet.generate_key(), tmp_path / "synthetic-life-before-erasure.enc"
        backup(cfg.database, archive, key)
        source, fetched, goal_id, message_id, day = records[-1]
        with active.store.transaction(control_write=True):
            active.ledger.expire_events(active.store, [source], active.clock())
        restored_state = tmp_path / "restored-state"
        restore(archive, empty_database, key, cfg.erasure_ledger,
                state_directory=restored_state)
        restored = Core(cfg.model_copy(update={
            "database": empty_database, "state_directory": str(restored_state),
        }), clock=active.clock)
        try:
            for instance in (active, restored):
                for event_id in (source, fetched):
                    event = instance.store.one((
                        'SELECT * FROM events WHERE event_id=%s'
                    ), (event_id,))
                    assert event["redacted"] and event["body_json"] == "{}"
                with pytest.raises(CoreError, match="GOAL_NOT_FOUND"):
                    instance.goals.get(goal_id)
                assert instance.store.one(
                    'SELECT topic,scope_json FROM goals WHERE goal_id=%s', (goal_id,)
                ) == {"topic": "", "scope_json": "{}"}
                assert not instance.store.rows("SELECT * FROM traits")
                assert (
                    instance.store.one(
                        'SELECT state FROM social_candidates WHERE event_id=%s', (fetched,)
                    )["state"]
                    == "ERASED"
                )
                message = instance.store.one(
                    'SELECT * FROM outbox WHERE message_id=%s', (message_id,)
                )
                assert message["state"] == "CANCELLED" and message["content_json"] == "{}"
                assert instance.diary.get_day(day, cfg.owner_qq_id) is None
                assert (
                    instance.store.one(
                        'SELECT status FROM diary_entries WHERE local_date=%s', (day.isoformat(),)
                    )["status"]
                    == "REDACTED"
                )
                assert not instance.store.one(
                    "SELECT 1 FROM pg_constraint WHERE connamespace=current_schema()::regnamespace "
                    "AND NOT convalidated"
                )
                assert instance.store.schema_capabilities() and not instance.store.one(
                    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                    "WHERE c.relnamespace=current_schema()::regnamespace "
                    "AND (NOT i.indisvalid OR NOT i.indisready)"
                )
        finally:
            restored.close()
    finally:
        if active is not core:
            active.close()
