"""One guard shares original rows; custody and the next boundary remain live."""

from pathlib import Path

import pytest

from companion import authority_custody, owner_authority
from companion.agent_runtime import check_conversation
from companion.contracts import CoreError
from companion.conversation_read import ConversationRead
from tests.support.owner_runtime import chat


def test_guard_reads_original_policy_once_but_rechecks_external_custody(
    core, tmp_path, monkeypatch,
):
    _, job = chat(core, tmp_path)
    queries, custody = [], []
    one, check = core.store.one, authority_custody.check

    def observed(statement, parameters=()):
        if "AS custody_erasure" in str(statement):
            queries.append(1)
        return one(statement, parameters)

    def checked(*args, **kwargs):
        custody.append(1)
        return check(*args, **kwargs)

    monkeypatch.setattr(core.store, "one", observed)
    monkeypatch.setattr(authority_custody, "check", checked)
    check_conversation(core, job)
    assert len(queries) == 1 and len(custody) == 2
    core.store.execute("UPDATE jobs SET lease_token='changed' WHERE job_id=%s", (job["job_id"],))
    with pytest.raises(CoreError, match="JOB_LEASE_LOST"):
        check_conversation(core, job)


@pytest.mark.parametrize("boundary", ["stop", "expiry", "journal", "erasure"])
def test_guard_rechecks_live_boundary_after_all_context_proofs(
    core, tmp_path, monkeypatch, boundary,
):
    _, job = chat(core, tmp_path)
    check = core._check_context_snapshots
    saved = {}

    def change(*args, **kwargs):
        result = check(*args, **kwargs)
        if boundary == "stop":
            core.settings.owner_runtime.mode = "OFF"
        elif boundary == "expiry":
            core.clock.advance(core.settings.lease_seconds + 1)
        else:
            path = Path(core.settings.paid_model.journal_path if boundary == "journal"
                        else core.settings.erasure_ledger)
            saved[path] = path.read_bytes()
            path.write_bytes(saved[path] + b"\n")
        return result

    monkeypatch.setattr(core, "_check_context_snapshots", change)
    try:
        # Hold an owned read snapshot so read_or_write cannot retry the injected mutation.
        with core.store.read_snapshot(), pytest.raises(CoreError):
            check_conversation(core, job)
    finally:
        for path, content in saved.items():
            path.write_bytes(content)


def test_guard_rows_end_at_close_and_cannot_cross_store_or_snapshot(core, tmp_path):
    _, job = chat(core, tmp_path)
    with pytest.raises(RuntimeError, match="CONVERSATION_READ_SNAPSHOT_REQUIRED"):
        ConversationRead(core.store)
    with core.store.read_snapshot():
        read = ConversationRead(core.store)
        first = owner_authority.check_job(core.store, core.settings, core.clock, job,
                                          purpose="chat", read=read)
        first.policy.purposes.clear()
        assert "chat" in owner_authority.check_job(
            core.store, core.settings, core.clock, job, purpose="chat", read=read).policy.purposes
        with pytest.raises(RuntimeError, match="CONVERSATION_READ_SNAPSHOT_REQUIRED"):
            read.require(object())
        read.close()
        with pytest.raises(RuntimeError, match="CONVERSATION_READ_SNAPSHOT_REQUIRED"):
            owner_authority.check_job(core.store, core.settings, core.clock, job,
                                      purpose="chat", read=read)
    with core.store.read_snapshot(), pytest.raises(RuntimeError,
            match="CONVERSATION_READ_SNAPSHOT_REQUIRED"):
        read.one("SELECT 1")
    with core.store.transaction(), pytest.raises(RuntimeError,
            match="CONVERSATION_READ_SNAPSHOT_REQUIRED"):
        ConversationRead(core.store)


def test_writer_guard_observes_its_own_changed_source(core, tmp_path):
    _, job = chat(core, tmp_path)
    with core.store.transaction():
        check_conversation(core, job)
        core.store.execute("UPDATE events SET actor_id='companion' WHERE event_id=%s",
                           (job["event_id"],))
        with pytest.raises(CoreError):
            check_conversation(core, job)


@pytest.mark.parametrize("ceiling", [6, 96])
def test_guard_read_keeps_individual_proof_allowances_when_the_union_is_too_large(
    core, monkeypatch, ceiling,
):
    from companion import retrieval_work
    from tests.conftest import ingest
    from tests.test_memory_proof_batches import facts

    facts(core, count=9, chars=40)
    proofs = [core.memory_context.pin(core.clock(), event_ids={
        f"proof_{number}" for number in range(start, start + 6)}) for start in (1, 4)]
    admitted = ingest(core, 2, "核对两组来源")
    job = core.store.one("SELECT * FROM jobs WHERE job_id=%s", (admitted["job_id"],))
    snapshots = [{"kind": "memory", "proof": proof} for proof in proofs]
    monkeypatch.setattr(retrieval_work, "SOURCE_NODES", ceiling)
    with core.store.read_snapshot():
        read = ConversationRead(core.store)
        try:
            core._check_context_snapshots(job, snapshots, read=read)
        finally:
            read.close()
