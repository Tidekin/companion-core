"""18 independent fictional lifecycle instances; separate from semantic answers."""

from contextlib import closing

import pytest
from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.memory import Memory
from companion.store import Store, encode
from tests.conftest import ingest
from tests.test_memory_commands import fact

PROFILES = [
    ("P01", "林澄"),
    ("P02", "顾遥"),
    ("P03", "唐栀"),
    ("P04", "周霁"),
    ("P05", "宋岚"),
    ("P06", "许朔"),
]


@pytest.mark.parametrize("persona,name", PROFILES, ids=[p[0] for p in PROFILES])
@pytest.mark.parametrize("operation", ["correct", "recall", "forget"])
def test_fictional_suite_lifecycle(core, tmp_path, persona, name, operation, record_property):
    record_property(
        "suite_id", f"{persona}-L{['correct', 'recall', 'forget'].index(operation) + 1:02}/native"
    )
    original = f"{name}的测试样本代号是旧桥。"
    replacement = f"{name}的测试样本代号是新桥。"
    independent_text = f"{name}的独立对照箱在三楼。"
    target = fact(core, 1, original)
    independent = fact(core, 2, independent_text)
    key = Fernet.generate_key()
    archive, destination = tmp_path / "before.enc", tmp_path / "restored.sqlite3"
    backup(core.settings.database, archive, key)
    if operation == "correct":
        source = ingest(core, 3, f"/纠正这一条 {target} {replacement}")
        new_id = core.memory.decide(
            target, "correct", core.clock(), replacement, evidence_event_id=source["event_id"]
        )["memory_id"]
        hits = core.memory.search("测试样本代号", now=core.clock())
        assert new_id in {r["memory_id"] for r in hits}
        assert target not in {r["memory_id"] for r in hits}
        assert core.store.one("SELECT status FROM memories WHERE memory_id=?", (target,)) == {
            "status": "SUPERSEDED"
        }
    elif operation == "recall":
        with core.store.transaction():
            core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
        hits = core.memory.search("测试样本代号", now=core.clock())
        assert target not in {r["memory_id"] for r in hits}
        assert original not in encode(hits)
        restore(archive, destination, key, core.settings.erasure_ledger)
    else:
        core.memory.decide(target, "forget", core.clock())
        assert target not in {r["memory_id"] for r in core.memory.search("测试样本代号")}
        restore(archive, destination, key, core.settings.erasure_ledger)
    assert core.store.one("SELECT status FROM memories WHERE memory_id=?", (independent,)) == {
        "status": "CONFIRMED"
    }
    if operation != "correct":
        with closing(Store(str(destination))) as reopened:
            memory = Memory(reopened, core.settings, core.ledger)
            reopened.execute("DELETE FROM memory_vectors")
            hits = memory.search("测试样本代号", now=core.clock())
            assert target not in {r["memory_id"] for r in hits}
            assert original not in encode(hits)
            assert reopened.one(
                "SELECT status FROM memories WHERE memory_id=?", (independent,)
            ) == {"status": "CONFIRMED"}
    assert core.store.rows("SELECT * FROM model_usage") == []
