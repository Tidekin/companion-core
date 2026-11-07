"""Permission revisions cannot replace financial, erasure or native closure evidence."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from companion import model_accounting, model_carry, owner_authority, owner_input, paid_budget
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityPreview
from companion_onebot_bridge.config import BridgeConfig
from tests.conftest import run
from tests.model_carry_support import migrated
from tests.support.core_http import real_core_http
from tests.support.evaluation_authority_revision import revise, revised_policy, stop
from tests.support.model_accounting import prepared
from tests.support.model_carry_lifecycle import current_unknown
from tests.support.model_renewal import renewal
from tests.support.model_renewal_native import life_case, source_and_renewal
from tests.support.owner_authority import activate


def test_discovered_link_and_revision_share_native_terminal_contract(core, tmp_path):
    from companion import reply_revision
    from tests.support.carried_capabilities import reader_carry
    from tests.support.exploration import URL
    from tests.support.reading_followup import admit

    case = reader_carry(core, tmp_path)
    core.settings.control_token = SecretStr("synthetic-discovery-revision-control")
    config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
        bot_qq_id=core.settings.bot_qq_id,
        bridge_token=core.settings.bridge_token.get_secret_value(), onebot_token="s" * 32,
        protocol_epoch=case.policy.capture.protocol_epoch)
    plan = SimpleNamespace(authority=case.successor["authority"], key=case.key, policy=case.policy)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + config.bridge_token},
        ) as client:
            event, _ = await admit(client, core, config, plan, 2, "看看 " + URL)
            _, accepted = await admit(client, core, config, plan, 3, "先不读了")
            job = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (accepted["job_id"],))
            reply_revision.remember_sources(core.store, job, [event["event_id"]])
            core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s",
                               (accepted["job_id"],))
            item = core.store.one('SELECT * FROM content_items WHERE owner_event_id=%s',
                                  (event["event_id"],))
            assert item["state"] == "WAITING" and item["read_request_id"] is None
            original = core.store.rows("SELECT * FROM model_usage")
            control = {"Authorization": "Bearer synthetic-discovery-revision-control"}
            stopped = await client.post("/internal/v1/owner-authority/stop", json={
                "operation_id": "combined-discovery-stop", "authority": plan.authority},
                headers=control)
            assert stopped.status_code == 200, stopped.text
            query = {"expected_revision": 4, "policy": revised_policy(case).model_dump(mode="json")}
            preview = await client.post("/internal/v1/owner-authority/preview",
                                         json=query, headers=control)
            assert preview.status_code == 200, preview.text
            active = await client.post("/internal/v1/owner-authority/activate", json={
                **query, "operation_id": "combined-discovery-revision",
                "preview_sha256": preview.json()["preview_sha256"]}, headers=control)
            assert active.status_code == 200, active.text
            assert core.store.rows("SELECT * FROM model_usage") == original
            assert core.store.one('SELECT * FROM content_items WHERE item_id=%s',
                                  (item["item_id"],)) == item
            assert not core.store.one("SELECT 1 FROM exploration_requests")

    run(scenario())


def test_pending_known_bill_blocks_permission_preview(core, tmp_path, monkeypatch):
    case = migrated(core, tmp_path)
    stop(core, case)
    request, evidence = prepared(core, tmp_path, case.request_id, basis={
        "kind": "provider_statement", "provider": "offline-fixture", "model": "offline-text",
        "currency": "USD", "actual_micro_usd": 5000, "provider_record_sha256": "b" * 64})
    journal = paid_budget.journal

    def unavailable(store, approval, record=None, **kwargs):
        if record and record["kind"] == model_accounting.KIND:
            raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
        return journal(store, approval, record, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(paid_budget, "journal", unavailable)
        assert model_accounting.record(core.store, core.settings, request, evidence, core.clock)[
            "state"] == "PENDING_JOURNAL"
    before = list(core.store.db.iterdump())
    with pytest.raises(CoreError, match="RECONCILIATION_REQUIRED"):
        owner_authority.preview(core.store, core.settings, core.clock,
            AuthorityPreview(expected_revision=4, policy=revised_policy(case)))
    assert list(core.store.db.iterdump()) == before
    assert core.store.one("SELECT billed_micro_usd FROM model_usage")["billed_micro_usd"] == 5000


def test_revision_fsync_projection_interruption_remains_closed(core, tmp_path, monkeypatch):
    case = migrated(core, tmp_path)
    stop(core, case)
    insert = core.store.insert

    def crash(table, **values):
        if table == "owner_authority_revisions":
            raise RuntimeError("synthetic revision projection interruption")
        return insert(table, **values)

    before = core.store.rows("SELECT * FROM model_usage")
    prefix = Path(core.settings.paid_model.journal_path).read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(core.store, "insert", crash)
        with pytest.raises(RuntimeError, match="projection interruption"):
            activate(core, revised_policy(case), revision=4, operation="interrupted-revision")
    assert owner_authority._latest(core.store)["state"] == "STOPPED"
    assert core.store.rows("SELECT * FROM model_usage") == before
    assert Path(core.settings.paid_model.journal_path).read_bytes().startswith(prefix)
    with pytest.raises(CoreError, match="RECONCILIATION_REQUIRED"):
        activate(core, revised_policy(case), revision=4, operation="interrupted-revision")


@pytest.mark.parametrize("evaluation", [False, True])
def test_older_closed_unknown_renewal_keeps_explicit_evaluation_scope(
    core, tmp_path, evaluation,
):
    case = migrated(core, tmp_path)
    if evaluation:
        revise(core, case)
    current_unknown(core)
    stop(core, case, "fresh-unknown-stop")
    active, _ = activate(core, case.policy,
        revision=case.successor["authority"]["revision"] + 1, operation="fresh-unknown-resume")
    case.successor = active
    before = core.store.rows("SELECT * FROM model_usage")
    request, policy, witness = renewal(core, case)
    assert policy.purposes == case.policy.purposes
    assert policy.capability_policies == case.policy.capability_policies
    receipt = model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    with core.store.transaction():
        saved = owner_authority.custody(core.store, core.settings, settlement=True)[
            "model_carries"][receipt["carry_sha256"]]
    assert saved["manifest"]["inputs"] == []
    assert {item["request_id"] for item in saved["manifest"]["unknowns"]} == {
        row["request_id"] for row in before}
    activate(core, policy, revision=request["authority"]["revision"] + 1,
             operation="renew-retained-evaluation-history")
    assert core.store.rows("SELECT * FROM model_usage") == before
    assert not core.store.rows("SELECT * FROM outbox")


def test_native_source_erasure_keeps_revision_finance_and_blocks_new_authority(
    core, tmp_path, monkeypatch,
):
    case = life_case(core, tmp_path, monkeypatch)

    async def setup():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as client:
            source, request, policy, witness = await source_and_renewal(
                core, tmp_path, case, client)
        case.receipt = model_carry.prepare(
            core.store, core.settings, request, witness, core.clock())
        case.successor, _ = activate(core, policy, revision=4, operation="life-before-evaluation")
        case.policy = policy
        return source

    source = run(setup())
    revise(core, case)
    request, policy, witness = renewal(core, case, number=2)
    policy = policy.model_copy(update={"life_shadow": policy.life_shadow.model_copy(
        update={"authorization_sha256": policy.authorization_sha256,
                "expires_at": policy.expires_at})})
    request["successor_policy"] = policy.model_dump(mode="json")
    model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    active, _ = activate(core, policy, revision=8, operation="life-after-evaluation")
    case.policy, case.successor = policy, active
    stop(core, case, "erase-authority-stop")
    before = core.store.rows("SELECT * FROM model_usage")
    with core.store.transaction():
        core.ledger.expire_events(core.store, [source], core.clock())
    assert core.store.one('SELECT redacted FROM events WHERE event_id=%s', (source,)) == {
        "redacted": 1}
    assert len(owner_authority.custody(
        core.store, core.settings, settlement=True)["model_carries"]) == 3
    assert core.store.rows("SELECT * FROM model_usage") == before
    with pytest.raises(CoreError):
        activate(core, policy, revision=10, operation="erased-current-refused")
    with pytest.raises(CoreError):
        owner_input.approve(core.store, core.settings, core.clock, case.capture)
    assert all(row["policy_json"] == "{}" for row in core.store.rows(
        "SELECT policy_json FROM owner_authority_revisions WHERE policy_redacted=1"))


def test_second_authorization_cannot_regrant_or_remove_evaluation(core, tmp_path):
    case = migrated(core, tmp_path)
    revise(core, case)
    stop(core, case, "second-revision-stop")
    for purposes in (case.policy.purposes, [p for p in case.policy.purposes if p != "evaluation"]):
        policy = case.policy.model_copy(update={
            "purposes": purposes, "authorization_sha256": "b" * 64})
        with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_APPROVAL_CHANGED"):
            activate(core, policy, revision=6, operation="second-revision-refused")
