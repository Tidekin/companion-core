"""Run after Root applies the proposed native Ledger/Core/config hooks."""
import json

import pytest
from cryptography.fernet import Fernet

from companion import affect, reply_revision
from companion.affect import NEUTRAL
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.conversation_policy import PARTICIPATION_V1, ConversationPolicyConfig
from companion.core import Core
from tests.conftest import ingest, run


def v1_policy(**values):
    return ConversationPolicyConfig(strategy="participation_v1", **values)


def test_reset_native_ledger_old_backup_and_idempotent_retry(empty_database, core, tmp_path):
    core.settings.affect_enabled = True
    ingest(core, 1, "简短一点")
    key, archive = Fernet.generate_key(), tmp_path / "before.enc"
    restored_state = tmp_path / "restored-state"
    backup(core.settings.database, archive, key)
    first = core.affect.reset(core.settings.owner_qq_id, "reset-1", core.ledger)
    assert core.affect.snapshot()["state"] == NEUTRAL
    assert core.expression_learning.snapshot(core.settings.owner_qq_id)["items"] == []
    assert core.store.one("SELECT redacted FROM events")["redacted"] == 0
    records = core.ledger.read()
    assert records[-1]["target_type"] == "affect_reset"
    core.clock.advance(1)
    ingest(core, 2, "详细解释")
    assert core.affect.reset(core.settings.owner_qq_id, "reset-1", core.ledger) == first
    assert core.ledger.read() == records
    assert core.affect.snapshot()["state"]["interaction_tone"] == "detailed"
    restore(archive, empty_database, key, core.settings.erasure_ledger,
            state_directory=restored_state)
    with Core(core.settings.model_copy(update={
        "database": empty_database, "state_directory": str(restored_state),
    }),
              core.model, core.clock) as restored:
        assert restored.affect.snapshot()["state"] == NEUTRAL
        assert not restored.affect.observe("evt_test_1")
        assert restored.store.one("SELECT redacted FROM events")["redacted"] == 0


def test_ledger_first_reset_survives_database_rollback(core, monkeypatch):
    core.settings.affect_enabled = True
    ingest(core, 1, "简短一点")
    def fail(*args):
        raise RuntimeError("synthetic crash after fsync")
    with monkeypatch.context() as patch:
        patch.setattr(affect, "apply_feedback_reset", fail)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            core.affect.reset(core.settings.owner_qq_id, "crash-reset", core.ledger)
    assert core.ledger.read()[-1]["target_type"] == "affect_reset"
    assert core.store.one('SELECT 1 FROM runtime_meta WHERE key=%s', (affect.RESET_KEY,)) is None
    core.ledger.replay(core.store)
    assert core.affect.snapshot()["state"] == NEUTRAL
    assert not core.affect.observe("evt_test_1")


def test_native_owner_reset_command_needs_no_generation(core):
    core.settings.affect_enabled = True
    accepted = ingest(core, 1, "简短一点")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (accepted["job_id"],))
    command = ingest(core, 2, "/重置互动")
    assert core.affect.snapshot()["state"] == NEUTRAL
    source = core.store.one('SELECT body_json,memory_processed FROM events WHERE event_id=%s',
                            (command["event_id"],))
    assert json.loads(source["body_json"])["control_command"]
    assert source["memory_processed"] == 1
    assert run(core.run_once())
    assert core.model.calls == []
    assert core.store.one("SELECT count(*) n FROM privacy_erasure_ledger")["n"] == 1
    assert "已重置互动反馈" in core.store.one("SELECT content_json FROM outbox")["content_json"]


@pytest.mark.parametrize("mode", ["OFF", "ENABLED"])
def test_core_shadow_preserves_payload_and_reset_fences_source(core, mode):
    # Compare policy payloads with room for the added guidance; budget pruning has
    # its own dialogue-context tests and may legitimately drop optional references.
    core.settings.context_soft_tokens += len(PARTICIPATION_V1.encode("utf-8"))
    core.settings.affect_enabled = True
    accepted = ingest(core, 1, "简短一点")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (accepted["job_id"],))
    core.clock.advance(1)
    ingest(core, 2, "怎么处理这个问题")
    job = core.claim_job("p1-context")
    original = core.context(job)
    core.settings.conversation_policy = v1_policy(mode="SHADOW")
    assert core.context(job) == original
    core.settings.conversation_policy = v1_policy(mode=mode)
    snapshots = []
    candidate = core.context(job, snapshots=snapshots)
    assert candidate[1:] == original[1:]
    assert any(item["kind"] == "affect" for item in snapshots)
    assert any(item["kind"] == "conversation_policy" for item in snapshots) == (mode == "ENABLED")
    core.affect.reset(core.settings.owner_qq_id, "context-reset", core.ledger)
    with pytest.raises(CoreError) as error:
        core._check_context_snapshots(job, snapshots)
    assert error.value.code == reply_revision.CANCELLED


def test_control_reset_preserves_unknown_and_owner_authority_custody(core, tmp_path):
    from pathlib import Path

    import httpx
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from companion import evaluation, paid_budget
    from companion.api import create_app
    from companion.api_store import ApiStore
    from tests.support.evaluation import authorize, prepare, work

    manifest = prepare(core, tmp_path)
    evaluation.submit(core, authorize(core, manifest))

    def uncertain(request):
        raise httpx.ReadTimeout("synthetic uncertainty", request=request)

    work(core, uncertain)
    usage = core.store.rows("SELECT * FROM model_usage")
    assert len(usage) == 1 and usage[0]["state"] == "UNKNOWN"
    revisions = core.store.rows("SELECT * FROM owner_authority_revisions")
    journal = Path(core.settings.paid_model.journal_path).read_bytes()
    core.settings.control_token = SecretStr("synthetic-reset-budget-control")
    app = create_app(core.settings)
    app.state.core = core
    app.state.api_store = ApiStore(core.database, core.settings.state_directory)
    response = TestClient(app).post("/internal/v2/affect/reset",
        json={"request_id": "reset-uncertain"},
        headers={"Authorization": "Bearer synthetic-reset-budget-control"})
    assert response.status_code == 200, response.text
    assert core.store.rows("SELECT * FROM model_usage") == usage
    assert core.store.rows("SELECT * FROM owner_authority_revisions") == revisions
    assert Path(core.settings.paid_model.journal_path).read_bytes() == journal
    with core.store.transaction():
        paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True)
