"""New examples separate from the frozen714; only external model transport is synthetic."""

import json

import pytest
from cryptography.fernet import Fernet

from companion import memory_references as refs
from companion.backup import backup, restore
from companion.contracts import Candidate, CoreError
from companion.memory_context import MemorySources
from companion.memory_work import checkpoint_state
from companion.store import encode
from tests.conftest import ingest, run
from tests.test_voice_flow import finish_node, flow_fixture, upload

TEXT = "青栎资料卡写着：入场日期是十二月九日，座位为B17。"


def prepared(core, kind="document"):
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    ingest(core, 1, "这是一份别人整理的资料")
    assert run(core.run_once())
    value = refs.capture(core.memory, "evt_test_1", kind, TEXT,
                         {"fixture": "native-reference-contract"}, core.clock())
    item = Candidate(subject="project", kind="fact", fact_key="visit", content=TEXT,
        evidence_event_id="evt_test_1", evidence_quote="入场日期是十二月九日",
        evidence_reference_id=value["reference_id"])
    return value, item


@pytest.mark.parametrize("kind", ["document", "image", "transcript", "quote", "assistant"])
def test_reference_requires_exact_quote_owner_binding_and_cannot_confirm_or_create_self(core, kind):
    value, item = prepared(core, kind)
    # Generic capture itself grants no provenance: assistant/quote require their native receipt.
    if kind in {"quote", "assistant"}:
        with pytest.raises(CoreError):
            core.memory.add(item, core.clock())
        return
    with pytest.raises(CoreError):
        core.memory.add(item, core.clock(), confirmed=True)
    with pytest.raises(CoreError):
        core.memory.add(item.model_copy(update={"subject": "self"}), core.clock())
    with pytest.raises(CoreError):
        core.memory.add(item.model_copy(update={"evidence_quote": "模型瞎猜的内容"}), core.clock())
    memory_id = core.memory.add(item, core.clock())
    row = core.store.one("SELECT * FROM memories WHERE memory_id=?", (memory_id,))
    assert row["status"] == "HYPOTHESIS" and row["origin"] == "event_observed"
    assert "未由主人确认为事实" in row["content"] and TEXT in row["content"]
    assert MemorySources(core.memory, core.clock()).current(row)
    assert not core.store.rows("SELECT * FROM traits")
    assert refs.for_memory(core.memory, row, core.clock())["kind"] == kind


def test_extraction_includes_reference_then_recall_pins_it_and_erasure_replays(core, tmp_path):
    value, item = prepared(core)
    core.model.extraction = encode({"memories": [item.model_dump(mode="json")]})
    assert run(core.extract_once())
    payload = json.loads(next(messages for messages, scenario in core.model.calls
                              if scenario == "memory_extract")[-1]["content"])
    assert payload["events"][0]["references"][0]["text"] == TEXT
    assert payload["events"][0]["references"][0]["ref"] == "r0"
    row = core.memory.search("十二月九日")[0]
    assert row["origin"] == "event_observed"
    proof = core.memory_context.pin(core.clock(), rows=[row])
    assert proof["source_event_ids"] == ["evt_test_1"]
    key = Fernet.generate_key().decode()
    snapshot = tmp_path / "references.backup"
    backup(core.settings.database, str(snapshot), key)
    usage = core.store.rows("SELECT * FROM model_usage")
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    assert not core.memory.search("十二月九日")
    assert not core.store.rows("SELECT * FROM runtime_meta WHERE key LIKE ?", (refs.INPUT + "%",))
    assert core.store.rows("SELECT * FROM model_usage") == usage
    core.store.close()
    restored_path = str(tmp_path / "restored.db")
    restore(str(snapshot), restored_path, key, core.settings.erasure_ledger)
    # The existing Core startup replays the Ledger before exposing memory.
    from companion.core import Core

    restored_settings = core.settings.model_copy(update={"database": restored_path})
    with Core(restored_settings, core.model, core.clock) as recovered:
        assert not recovered.memory.search("十二月九日")
        assert recovered.store.rows("SELECT * FROM model_usage") == usage


def test_reference_changed_during_extraction_prevents_summary_and_fact_commit(core):
    value, item = prepared(core)
    core.model.extraction = encode({"memories": [item.model_dump(mode="json")]})
    core.model.callback = lambda: core.store.execute(
        "DELETE FROM runtime_meta WHERE key=?", (refs.INPUT + value["reference_id"],))
    assert not run(core.extract_once())
    assert not core.store.rows("SELECT * FROM memories")
    assert not core.store.rows("SELECT * FROM episodes")
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    assert checkpoint_state(core.store)["batch"]["error"] == "MEMORY_REFERENCE_CHANGED"


def test_erasing_derived_observation_clears_pending_reference_candidates(core):
    from tests.test_observations import enable_observations, observation

    _, item = prepared(core)
    enable_observations(core)
    observed = core.observations.append("reader", observation(
        core, text=TEXT, source_event_ids=["evt_test_1"]))["event_id"]
    value = refs.capture(core.memory, "evt_test_1", "document", TEXT,
                         {"observation_event_id": observed}, core.clock(), sources=[observed])
    item = item.model_copy(update={"evidence_reference_id": value["reference_id"]})
    core.model.extraction = encode({"memories": [item.model_dump(mode="json")]})

    def withdraw():
        with core.store.transaction():
            core.ledger.expire_events(core.store, [observed], core.clock())

    core.model.callback = withdraw
    assert not run(core.extract_once())
    assert checkpoint_state(core.store) is None
    assert not core.store.rows("SELECT * FROM memories")
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                              (refs.INPUT + value["reference_id"],))
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"


def test_asr_snapshot_comes_from_successful_receipt_and_outlives_audio_ttl(core, monkeypatch):
    from tests import voice_owner_fixtures

    original_activate = voice_owner_fixtures.activate

    def activate_with_memory(core, policy):
        policy = policy.model_copy(update={"purposes": [*policy.purposes, "memory"],
                                           "memory_scope": "owner_private"})
        return original_activate(core, policy)

    monkeypatch.setattr(voice_owner_fixtures, "activate", activate_with_memory)
    core.settings.memory_enabled = True
    flow, jobs, _, event_id, key = flow_fixture(core, monkeypatch, make_reply=False)
    upload(core, flow, event_id, key)
    finish_node(jobs, operation="asr", text="我刚才说的集合地点是南门")
    job = core.claim_job("transcript-reference")
    context = flow.for_context(event_id, job)
    stored = core.store.rows("SELECT value FROM runtime_meta WHERE key LIKE ?", (refs.INPUT + "%",))
    assert len(stored) == 1
    value = json.loads(stored[0]["value"])
    assert value["kind"] == "transcript" and value["text"] == "我刚才说的集合地点是南门"
    assert value["receipt"]["result"]["result_sha256"] == context["proof"]["voice_results"][0][
        "result_sha256"]
    core.clock.advance(core.settings.voice.result_ttl_seconds + 1)
    assert refs.read(core.memory, value["reference_id"], core.clock())["text"] == value["text"]
    item = Candidate(subject="project", kind="fact", fact_key="meeting",
        content="转写提到集合在南门",
        evidence_event_id=event_id, evidence_quote="集合地点是南门",
        evidence_reference_id=value["reference_id"])
    memory_id = core.memory.add(item, core.clock())
    assert core.memory.search("集合地点南门")[0]["memory_id"] == memory_id
