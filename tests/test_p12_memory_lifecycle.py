"""Native memory lifecycle preserves paid custody, source erasure and restore replay."""

import asyncio
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.contracts import Candidate
from companion.memory import Memory
from companion.postgres import Database
from companion.privacy import epoch
from companion.store import Store, encode
from companion.timeline import recent_timeline
from tests.conftest import ingest, run
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.support.native_consumer_entry import environment


def test_native_lifecycle_restore_and_rebuild_preserve_independent_fact(
    core, tmp_path, empty_database
):
    memory = Memory(core.store, core.settings.model_copy(update={"memory_enabled": True}),
                    core.ledger)
    first = ingest(core, 1, "用户最喜欢的饮料是茉莉绿茶。")
    other = ingest(core, 2, "另一个项目的封面是蓝色。")
    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    with core.store.transaction(control_write=True):
        ids = [memory.add(Candidate(subject="user", kind="fact", fact_key=key,
            content=text, evidence_event_id=source["event_id"], evidence_quote=text),
            core.clock(), confirmed=False) for key, text, source in (
                ("favorite_drink", "用户最喜欢的饮料是茉莉绿茶。", first),
                ("other_cover", "另一个项目的封面是蓝色。", other))]
    target, independent = ids
    memory.decide(target, "confirm", core.clock())
    memory.decide(independent, "confirm", core.clock())
    assert target in {row["memory_id"] for row in memory.search("我平时最爱喝什么？")}
    correction = ingest(core, 3, f"/纠正这一条 {target} 用户最喜欢的饮料是热可可。")
    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    revised = memory.decide(target, "correct", core.clock(), "用户最喜欢的饮料是热可可。",
                            evidence_event_id=correction["event_id"])["memory_id"]
    assert revised in {row["memory_id"] for row in memory.search("我最喜欢的饮料是什么？")}
    assert target not in {row["memory_id"] for row in memory.search("茉莉绿茶")}
    assert core.store.one("SELECT COUNT(*) n FROM memory_revisions")["n"] == 1
    assert core.store.one("SELECT COUNT(*) n FROM memory_vectors")["n"] >= 1
    with core.store.transaction(control_write=True):
        memory.episodes(core.store.rows('SELECT * FROM events WHERE event_id IN (%s,%s)',
                                       (first["event_id"], correction["event_id"])), core.clock())
    key = Fernet.generate_key()
    archive, restored = tmp_path / "before-forget.enc", empty_database
    backup(core.settings.database, archive, key)
    before = epoch(core.store)
    memory.decide(revised, "forget", core.clock())
    assert epoch(core.store) > before
    assert len(core.ledger.read()) == 2
    restore(archive, restored, key, core.settings.erasure_ledger,
            state_directory=tmp_path / "restored-state")
    with (
        Database(restored) as database,
        closing(Store(database, tmp_path / "restored-state")) as reopened,
    ):
        for store in (core.store, reopened):
            store.execute("DELETE FROM memory_vectors")
            store.execute("REINDEX INDEX idx_memories_lexical")
            rebuilt = Memory(store, memory.settings, core.ledger)
            for query in ("茉莉绿茶", "热可可", "我平时最爱喝什么？"):
                assert not ({target, revised} & {row["memory_id"] for row in rebuilt.search(query)})
            assert independent in {row["memory_id"] for row in rebuilt.search("项目封面颜色")}
            assert not store.rows("SELECT * FROM memory_revisions")
            assert not store.rows("SELECT * FROM memory_evidence WHERE memory_id IN (%s,%s)",
                                  (target, revised))
            assert not store.rows("SELECT * FROM memory_vectors WHERE memory_id IN (%s,%s)",
                                  (target, revised))
            assert store.one("SELECT redacted FROM events WHERE event_id=%s",
                             (other["event_id"],)) == {"redacted": 0}
            timeline = recent_timeline(store, memory.settings, core.clock(),
                                       core.settings.owner_qq_id, topic="饮料", days=7)
            assert "热可可" not in encode(timeline) and "茉莉绿茶" not in encode(timeline)
            assert "热可可" not in encode(store.rows("SELECT body_json FROM events"))
            assert not store.rows("SELECT * FROM model_usage")


def test_native_control_lifecycle_preserves_paid_and_authority_journals(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "memory")) as (entry, sends):
            model = Provider(answer("收到这条项目消息。"))
            async with worker_for(core, model) as worker:
                await entry.admit(1, "合成原生项目输入")
                assert await worker.run_once("original-paid-answer")
                assert (await entry.send(entry.client, entry.protocol))["worked"]
                assert len(model.requests) == len(sends) == 1
                journal = Path(core.settings.paid_model.journal_path)
                paid_bytes = await asyncio.to_thread(journal.read_bytes)
                usage = core.store.rows("SELECT * FROM model_usage")
                admissions = core.store.rows("SELECT * FROM model_admissions")
                windows = core.store.rows("SELECT * FROM model_budget_windows")
                authority = core.store.rows("SELECT * FROM owner_authority_revisions")

                await entry.admit(101, "/记住这个：项目的代号是青竹七号。")
                assert await worker.run_once("native-remember")
                mid = core.store.one("SELECT memory_id FROM memories")["memory_id"]
                assert (await entry.send(entry.client, entry.protocol))["worked"]
                await entry.admit(102, f"/纠正这一条 {mid} 项目的代号是白帆九号。")
                assert await worker.run_once("native-correct")
                revised = core.store.one("SELECT memory_id FROM memories WHERE status='CONFIRMED'")[
                    "memory_id"]
                assert revised != mid
                assert (await entry.send(entry.client, entry.protocol))["worked"]
                await entry.admit(103, f"/忘记这一条 {revised}")
                assert await worker.run_once("native-forget")
                assert (await entry.send(entry.client, entry.protocol))["worked"]

                assert await asyncio.to_thread(journal.read_bytes) == paid_bytes
                assert core.store.rows("SELECT * FROM model_usage") == usage
                assert core.store.rows("SELECT * FROM model_admissions") == admissions
                assert core.store.rows("SELECT * FROM model_budget_windows") == windows
                assert core.store.rows("SELECT * FROM owner_authority_revisions") == authority
                assert len(model.requests) == 1 and len(sends) == 4
                assert len(core.ledger.read()) == 2
                assert not core.store.rows((
                    'SELECT * FROM memory_evidence WHERE memory_id IN (%s,%s)'
                ),
                                           (mid, revised))

                await entry.stop()
                stopped_bytes = await asyncio.to_thread(journal.read_bytes)
                assert stopped_bytes.startswith(paid_bytes) and stopped_bytes != paid_bytes
                assert core.store.one("SELECT state FROM owner_authority_revisions "
                                      "ORDER BY revision DESC LIMIT 1") == {"state": "STOPPED"}
                denied = await entry.client.post(f"/internal/v1/memories/{mid}/decisions",
                    json={"actor_id": "owner", "decision": "confirm"})
                assert denied.status_code == 403, denied.text
                assert not await worker.run_once("stopped-cannot-reset-custody")
                assert await asyncio.to_thread(journal.read_bytes) == stopped_bytes
                assert core.store.rows("SELECT * FROM model_usage") == usage
                assert core.store.rows("SELECT * FROM model_admissions") == admissions
                assert core.store.rows("SELECT * FROM model_budget_windows") == windows
    run(scenario())
