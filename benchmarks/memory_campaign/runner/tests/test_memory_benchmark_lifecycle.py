"""18 independent fictional lifecycle instances; separate from semantic answers."""

from contextlib import closing

import pytest
from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.memory import Memory
from companion.postgres import Database
from companion.store import Store, encode
from tests.postgres_fixture import isolated_database
from tests.conftest import ingest
from tests.support.memory_commands import fact

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
    record_property("suite_id", f"{persona}-L{['correct', 'recall', 'forget'].index(operation) + 1:02}/native")
    original = f"{name}的测试样本代号是旧桥。"
    replacement = f"{name}的测试样本代号是新桥。"
    independent_text = f"{name}的独立对照箱在三楼。"
    target = fact(core, 1, original)
    independent = fact(core, 2, independent_text)
    key = Fernet.generate_key()
    archive = tmp_path / "before.enc"
    state_directory = tmp_path / "restored-state"
    backup(core.settings.database, archive, key)
    if operation == "correct":
        source = ingest(core, 3, f"/纠正这一条 {target} {replacement}")
        new_id = core.memory.decide(target, "correct", core.clock(), replacement, evidence_event_id=source["event_id"])[
            "memory_id"
        ]
        hits = core.memory.search("测试样本代号", now=core.clock())
        assert new_id in {r["memory_id"] for r in hits}
        assert target not in {r["memory_id"] for r in hits}
        assert core.store.one("SELECT status FROM memories WHERE memory_id=%s", (target,)) == {"status": "SUPERSEDED"}
    elif operation == "recall":
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
        hits = core.memory.search("测试样本代号", now=core.clock())
        assert target not in {r["memory_id"] for r in hits}
        assert original not in encode(hits)
    else:
        core.memory.decide(target, "forget", core.clock())
        assert target not in {r["memory_id"] for r in core.memory.search("测试样本代号")}
    assert core.store.one("SELECT status FROM memories WHERE memory_id=%s", (independent,)) == {"status": "CONFIRMED"}
    if operation != "correct":
        with isolated_database() as destination:
            restore(archive, destination, key, core.settings.erasure_ledger, state_directory=state_directory)
            with Database(destination) as database, closing(Store(database, state_directory)) as reopened:
                configuration = core.settings.model_copy(
                    update={"database": destination, "state_directory": str(state_directory)}
                )
                memory = Memory(reopened, configuration, core.ledger)
                reopened.execute("DELETE FROM memory_vectors")
                hits = memory.search("测试样本代号", now=core.clock())
                assert target not in {r["memory_id"] for r in hits}
                assert original not in encode(hits)
                assert reopened.one(
                    "SELECT status FROM memories WHERE memory_id=%s",
                    (independent,),
                ) == {"status": "CONFIRMED"}
    assert core.store.rows("SELECT * FROM model_usage") == []
