"""Native authority uses the original budget journal and owner decision flow."""

from copy import deepcopy

import pytest

import companion_delivery_protocol as wire
from companion import delivery_reconciliation as reconciliation
from companion import owner_authority, paid_budget
from companion.contracts import CoreError
from tests.support.owner_delivery_reconciliation import pending


@pytest.mark.parametrize("outcome", ["SENT", "CANCELLED"])
@pytest.mark.parametrize("quota", [False, True])
def test_native_resolution_retains_original_evidence_and_replays(
    core, tmp_path, monkeypatch, outcome, quota,
):
    if quota:
        from tests.support import owner_input as test_owner_input
        from tests.support.cumulative_budget import policy

        original_prepare = test_owner_input.prepare

        def configure(core, directory):
            grant = original_prepare(core, directory)
            policy(core)
            return grant.model_copy(update={
                "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings))})

        monkeypatch.setattr(test_owner_input, "prepare", configure)
    proof, _ = pending(core, tmp_path)
    usages = deepcopy(core.store.rows("SELECT * FROM model_usage"))
    before = paid_budget.journal(core.store, core.settings.paid_model,
                                 allow_settlement=True)["cumulative"]
    prepared = reconciliation.prepare(core.store, core.settings, proof, core.clock())
    assert prepared["state"] == "READY"
    result = reconciliation.resolve(core.store, core.settings, proof["message_id"], outcome,
        core.clock(), operation_id="owner-native-resolution", operation_sha256="e" * 64)
    assert wire.validate_receipt(result["receipt"])["quiescence"] == proof
    assert result["receipt"]["input_closure"]["terminal"]
    assert core.store.rows("SELECT * FROM model_usage") == usages
    receipt = reconciliation.lookup(core.store, core.settings, proof["message_id"])
    assert receipt == result["receipt"]
    after = paid_budget.journal(core.store, core.settings.paid_model,
                                allow_settlement=True)["cumulative"]
    assert before["policy_sha256"] == after["policy_sha256"]
    if quota:
        key = "send:" + wire.digest(proof["message_id"])
        assert after["operations"][key]["state"] == (
            "OWNER_SENT" if outcome == "SENT" else "OWNER_CLOSED")
        for operation, prior in before["operations"].items():
            assert {k: v for k, v in after["operations"][operation].items() if k != "state"} == {
                k: v for k, v in prior.items() if k != "state"}
        assert before["models"] == after["models"]
        assert before["policies"] == after["policies"]


@pytest.mark.parametrize("mutation", ["authority", "core_stop_record_sha256", "restored"])
def test_native_resolution_rejects_changed_binding(core, tmp_path, mutation):
    proof, key = pending(core, tmp_path)
    if mutation == "authority":
        proof["authority"] = {**proof["authority"], "revision": 2}
    elif mutation == "core_stop_record_sha256":
        proof[mutation] = "f" * 64
    else:
        with core.store.transaction():
            owner_authority.restore(core.store)
    proof["signature"] = key.sign(wire.signing_payload(proof)).hex()
    with pytest.raises(CoreError):
        reconciliation.prepare(core.store, core.settings, proof, core.clock())
    assert core.store.one("SELECT state FROM outbox WHERE message_id=?",
                           (proof["message_id"],))["state"] == "DELIVERY_UNCERTAIN"
