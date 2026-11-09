"""Real Core memory lifecycle and finite API sessions on the native database."""

import asyncio

import pytest

from companion.api_store import ApiStore
from companion.config import Settings
from companion.contracts import Candidate
from companion.core import Core
from companion.privacy import epoch
from companion.runtime import Runtime
from tests.conftest import Clock, FakeModel, ingest
from tests.postgres_fixture import isolated_native


@pytest.fixture
def native_settings(tmp_path):
    with isolated_native() as (_, conninfo):
        yield Settings(
            owner_qq_id="10001",
            bot_qq_id="10002",
            bridge_token="test-token-" * 4,
            database=conninfo,
            state_directory=str(tmp_path / "state"),
            erasure_ledger=str(tmp_path / "erasures.jsonl"),
            memory_enabled=False,
            operator_hot_reload=False,
            proactive_enabled=False,
            input_rate=300000,
            cached_input_rate=6000,
            output_rate=1200000,
        )


def test_runtime_bootstrap_reopen_and_finite_api_lease(native_settings):
    with Runtime(native_settings, Clock()) as runtime:
        original = runtime.store.one("SELECT value FROM runtime_meta WHERE key='voice_incarnation'")
        operations = ApiStore(runtime.database, native_settings.state_directory)

        async def parallel_reads():
            return await asyncio.gather(
                *(
                    operations.run(
                        lambda store: store.one(
                            "SELECT value FROM runtime_meta WHERE key='owner_qq_id'"
                        )
                    )
                    for _ in range(4)
                )
            )

        assert asyncio.run(parallel_reads()) == [{"value": "10001"}] * 4
    with Runtime(native_settings, Clock()) as runtime:
        assert (
            runtime.store.one("SELECT value FROM runtime_meta WHERE key='voice_incarnation'")
            == original
        )


def test_core_native_ingest_memory_correction_and_erasure(native_settings, monkeypatch):
    with Core(native_settings, FakeModel(), Clock()) as core:
        core.settings.memory_enabled = True
        # This checks the native lexical/provenance path; semantic quality has separate checks.
        monkeypatch.setattr(core.memory.index, "nominate", lambda *_a, **_kw: [])
        monkeypatch.setattr(core.memory.index, "scores", lambda *_a, **_kw: {})
        source = ingest(core, 1, "相机保存在湖蓝色硬壳箱")
        duplicate = ingest(core, 1, "相机保存在湖蓝色硬壳箱")
        assert duplicate["duplicate"]
        assert duplicate["event_id"] == source["event_id"]
        assert duplicate["job_id"] == source["job_id"]
        initial = epoch(core.store)
        with core.store.transaction(control_write=True):
            identity = core.memory.add(
                Candidate(
                    subject="project",
                    kind="fact",
                    content="相机保存在湖蓝色硬壳箱",
                    fact_key="camera_case",
                    evidence_event_id=source["event_id"],
                    evidence_quote="相机保存在湖蓝色硬壳箱",
                ),
                core.clock(),
                confirmed=True,
            )
        assert epoch(core.store) > initial
        hits = core.memory.search("相机", now=core.clock())
        assert [row["memory_id"] for row in hits] == [identity]
        core.clock.advance(1)
        changed = core.memory.decide(identity, "correct", core.clock(), "相机改放橙色背包")
        assert changed["memory_id"] != identity
        hits = core.memory.search("相机", now=core.clock())
        assert [row["memory_id"] for row in hits] == [changed["memory_id"]]
        assert hits[0]["content"] == "相机改放橙色背包"
        core.memory.decide(changed["memory_id"], "forget", core.clock())
        assert core.memory.search("相机", now=core.clock()) == []
        assert core.store.one(
            "SELECT state FROM privacy_erasure_ledger WHERE target_id=%s", (changed["memory_id"],)
        ) == {"state": "COMPLETE"}


def test_fsynced_erasure_survives_failed_projection_and_runtime_reopen(
    native_settings,
    monkeypatch,
):
    from companion import erasure
    from companion.contracts import CoreError

    with Core(native_settings, FakeModel(), Clock()) as core:
        core.settings.memory_enabled = True
        source = ingest(core, 1, "行李放在青色柜子")
        with core.store.transaction(control_write=True):
            identity = core.memory.add(
                Candidate(
                    subject="project",
                    kind="fact",
                    content="行李放在青色柜子",
                    fact_key="luggage_location",
                    evidence_event_id=source["event_id"],
                    evidence_quote="行李放在青色柜子",
                ),
                core.clock(),
                confirmed=True,
            )

        def fail_projection(*_args):
            raise RuntimeError("synthetic projection interruption")

        with monkeypatch.context() as patch:
            patch.setattr(erasure, "erase_derivatives", fail_projection)
            with pytest.raises(RuntimeError, match="synthetic projection interruption"):
                core.memory.decide(identity, "forget", core.clock())
        assert core.store.one("SELECT status FROM memories WHERE memory_id=%s", (identity,)) == {
            "status": "CONFIRMED"
        }
        with pytest.raises(CoreError, match="ERASURE_REPLAY_REQUIRED"):
            core.memory.search("*", now=core.clock())
    native_settings.memory_enabled = False
    with Core(native_settings, FakeModel(), Clock()) as recovered:
        recovered.settings.memory_enabled = True
        assert recovered.memory.search("*", now=recovered.clock()) == []
        assert recovered.store.one(
            "SELECT status FROM memories WHERE memory_id=%s", (identity,)
        ) == {"status": "FORGOTTEN"}
        assert len(recovered.ledger.read()) == 2


def test_http_ingest_independent_worker_and_segmented_outbox(native_settings, tmp_path):
    import json
    import subprocess
    import sys

    from fastapi.testclient import TestClient

    from companion import operator_config
    from companion.api import create_app
    from companion.conversation_policy import ConversationPolicyConfig
    from companion.input_debounce import InputDebounceConfig
    from companion.reply_segments import SegmentedReplyConfig
    from tests.conftest import event

    native_settings.input_debounce = InputDebounceConfig(enabled=False)
    native_settings.conversation_policy = ConversationPolicyConfig(
        mode="OFF", strategy="participation_v1"
    )
    native_settings.segmented_reply = SegmentedReplyConfig(min_chars=4, interval_ms=0)
    application = create_app(native_settings)
    with TestClient(application) as client:
        application.state.core.clock = Clock()
        incoming = event(application.state.core, 77, "请解释这条合成消息")
        headers = {
            "Authorization": "Bearer " + native_settings.bridge_token.get_secret_value(),
            "X-Idempotency-Key": "native-independent-77",
        }
        response = client.post(
            "/internal/v1/inbox/events", json=incoming.model_dump(mode="json"), headers=headers
        )
        assert response.status_code == 202, response.text
        settings_path = tmp_path / "worker-settings.private.json"
        settings_path.write_text(json.dumps(operator_config._raw(native_settings)), encoding="utf8")
        program = """
import asyncio,json,sys
from pathlib import Path
from companion.config import Settings
from companion.core import Core
from companion.model import ModelResponse
from tests.conftest import Clock
class Model:
    async def complete(self,messages,scenario):
        return ModelResponse("第一段合成回复。第二段合成回复。",100,20,40)
settings=Settings.model_validate_json(Path(sys.argv[1]).read_text('utf8'))
with Core(settings,Model(),Clock()) as core:
    progressed=asyncio.run(core.run_once('native-independent-worker'))
    print(json.dumps({'progressed':progressed}))
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", program, str(settings_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["progressed"]
        store = application.state.core.store
        assert store.one(
            "SELECT state,attempts FROM jobs WHERE job_id=%s", (response.json()["job_id"],)
        ) == {"state": "SUCCEEDED", "attempts": 1}
        parts = store.rows(
            "SELECT segment_index,segment_count,state FROM outbox "
            "WHERE origin_event_id=%s ORDER BY segment_index",
            (incoming.event_id,),
        )
        assert parts == [
            {"segment_index": 0, "segment_count": 2, "state": "PENDING"},
            {"segment_index": 1, "segment_count": 2, "state": "PENDING"},
        ]


@pytest.mark.parametrize("operation", ["remember", "extract"])
def test_native_memory_worker_commits_original_sources_and_usage(
    native_settings,
    monkeypatch,
    operation,
):
    import json

    async def nominate(*_args, **_kwargs):
        return []

    async def scores(*_args, revalidate=None, **_kwargs):
        if revalidate:
            revalidate()
        return {}

    with Core(native_settings, FakeModel(), Clock()) as core:
        core.settings.memory_enabled = True
        monkeypatch.setattr(core.memory.index, "nominate_async", nominate)
        monkeypatch.setattr(core.memory.index, "scores_async", scores)
        monkeypatch.setattr(core.memory.index, "nominate", lambda *_a, **_kw: [])
        monkeypatch.setattr(core.memory.index, "scores", lambda *_a, **_kw: {})
        text = "相机放在绿色收纳箱"
        source = ingest(core, 21, "/记住这个：" + text if operation == "remember" else text)
        if operation == "remember":
            assert asyncio.run(core.run_once("native-memory-command"))
            expected = "CONFIRMED"
        else:
            core.settings.extraction_min_events = 1
            core.settings.extraction_idle_seconds = 0
            core.store.execute(
                "UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (source["job_id"],)
            )
            core.model.extraction = json.dumps(
                {
                    "memories": [
                        {
                            "subject": "project",
                            "kind": "fact",
                            "fact_key": "camera_storage",
                            "content": text,
                            "evidence_event_id": source["event_id"],
                            "evidence_quote": text,
                        }
                    ]
                }
            )
            from companion import memory_work

            failures = []
            for method in ("execute", "one", "rows"):
                original = getattr(core.store, method)

                def observed(*args, _original=original, **kwargs):
                    try:
                        return _original(*args, **kwargs)
                    except Exception as error:
                        failures.append((type(error).__name__, str(error), str(args[0])))
                        raise

                monkeypatch.setattr(core.store, method, observed)
            progressed = asyncio.run(core.extract_once())
            if not progressed:
                pytest.fail(
                    json.dumps(
                        {
                            "sql_failures": failures,
                            "checkpoint": memory_work.checkpoint_state(core.store),
                            "usage": core.store.rows(
                                "SELECT scenario,state,request_id FROM model_usage"
                            ),
                        },
                        ensure_ascii=False,
                    )
                )
            expected = "HYPOTHESIS"
        assert core.store.one("SELECT content,status FROM memories WHERE kind='fact'") == {
            "content": text,
            "status": expected,
        }
        assert core.store.one("SELECT event_id FROM memory_evidence LIMIT 1") == {
            "event_id": source["event_id"]
        }
        usage = core.store.rows("SELECT state,billed_micro_usd FROM model_usage")
        if operation == "remember":
            assert usage == [] and core.model.calls == []
        else:
            assert usage and all(row["state"] == "SETTLED" for row in usage)
            assert all(row["billed_micro_usd"] > 0 for row in usage)


def test_native_core_factory_isolates_database_and_local_state(core_factory):
    with core_factory() as first, core_factory() as second:
        assert first.settings.database != second.settings.database
        assert first.store.state_directory != second.store.state_directory
        ingest(first, 1, "第一份独立状态")
        assert second.store.one("SELECT COUNT(*) AS n FROM events") == {"n": 0}
        ingest(second, 1, "第二份独立状态")
        assert first.store.one("SELECT COUNT(*) AS n FROM events") == {"n": 1}
        assert second.store.one("SELECT COUNT(*) AS n FROM events") == {"n": 1}


def test_memory_add_owns_atomic_control_write_and_refuses_partial_ordinary_write(core, monkeypatch):
    source = ingest(core, 1, "相机放在湖蓝色箱子里")
    candidate = Candidate(subject="project", kind="fact", fact_key="camera",
                          content="相机放在湖蓝色箱子里", evidence_quote="相机放在湖蓝色箱子里",
                          evidence_event_id=source["event_id"])
    core.settings.memory_enabled = True
    with core.store.transaction():
        with pytest.raises(RuntimeError, match="ERASURE_CONTROL_WRITE_REQUIRED"):
            core.memory.add(candidate, core.clock(), confirmed=True)
    assert core.store.one("SELECT COUNT(*) AS n FROM memories") == {"n": 0}
    before = epoch(core.store)

    def interrupted(_):
        raise ValueError("synthetic interruption after fact/evidence mutation")

    with monkeypatch.context() as fault:
        fault.setattr("companion.memory.advance_epoch", interrupted)
        with pytest.raises(ValueError, match="synthetic interruption"):
            core.memory.add(candidate, core.clock(), confirmed=True)
    assert core.store.one("SELECT COUNT(*) AS n FROM memories") == {"n": 0}
    assert core.store.one("SELECT COUNT(*) AS n FROM memory_evidence") == {"n": 0}
    assert epoch(core.store) == before
    identity = core.memory.add(candidate, core.clock(), confirmed=True)
    assert core.store.one("SELECT status FROM memories WHERE memory_id=%s", (identity,)) == {
        "status": "CONFIRMED"}
