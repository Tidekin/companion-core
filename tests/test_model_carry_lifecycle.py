"""Repeated native STOP preserves the original policy, liability and window scope."""

import copy
import hashlib
import json

import pytest

from companion import model_carry, owner_authority, owner_input, owner_input_state
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop, OwnerCapture
from companion_delivery_protocol import digest
from companion_delivery_protocol import model_carry as wire
from companion_delivery_protocol.authority import capture_signing_payload
from tests.model_carry_support import migrated
from tests.support.model_carry_lifecycle import current_unknown, stop_current
from tests.support.owner_authority import activate


def test_same_approval_can_resume_after_second_stop(core, tmp_path):
    case = migrated(core, tmp_path)
    before = core.store.rows("SELECT * FROM model_usage")
    owner_authority.stop(
        core.store,
        core.settings,
        core.clock,
        AuthorityStop(operation_id="second-normal-stop", authority=case.successor["authority"]),
    )
    result, _ = activate(core, case.policy, revision=4, operation="second-normal-activation")
    assert result["authority"] == case.successor["authority"] | {"revision": 5}
    assert core.store.rows("SELECT * FROM model_usage") == before


def carried_authority_chain(core, tmp_path):
    case = migrated(core, tmp_path)
    for index in range(2):
        stopped = owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id=f"chain-batch-stop-{index}", authority=case.successor["authority"],
        ))
        active, _ = activate(core, case.policy, revision=stopped["authority"]["revision"],
                             operation=f"chain-batch-resume-{index}")
        case.successor = active
    with core.store.transaction():
        view = owner_authority.custody(core.store, core.settings)
    saved = view["model_carries"][case.receipt["carry_sha256"]]
    revisions = core.store.rows("SELECT * FROM owner_authority_revisions "
                               "WHERE revision>=3 ORDER BY revision")
    return saved, revisions


def test_authority_chain_batches_rows_and_equal_policy_bodies_per_call(core, tmp_path, monkeypatch):
    from companion.owner_authority_protocol import OwnerGrantPolicy

    saved, revisions = carried_authority_chain(core, tmp_path)
    assert len(revisions) == 5
    before = digest([saved, revisions])
    parsed, queries = [], []
    original = OwnerGrantPolicy.model_validate_json

    def parse(value, *args, **kwargs):
        parsed.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(OwnerGrantPolicy, "model_validate_json", parse)
    fetch = core.store.rows

    def observed(statement, params=()):
        if "FROM owner_authority_revisions" in statement:
            queries.append(params)
        return fetch(statement, params)

    monkeypatch.setattr(core.store, "rows", observed)
    for _ in range(2):
        with core.store.transaction():
            model_carry.authority_chain(core.store, saved, revisions)
    expected = (saved["manifest"]["authority"]["grant_id"], 3, 7)
    assert queries == [expected, expected]
    assert len(parsed) == 2  # No parsed policy is retained across calls.
    assert digest([saved, revisions]) == before


@pytest.mark.parametrize("change", ["missing", "record", "policy", "redacted_body", "erased"])
def test_authority_chain_rechecks_current_rows_and_redaction(core, tmp_path, change):
    saved, revisions = carried_authority_chain(core, tmp_path)
    with core.store.transaction():
        model_carry.authority_chain(core.store, saved, revisions)
    if change == "missing":
        core.store.execute("DELETE FROM owner_authority_revisions WHERE revision=4")
    elif change == "record":
        core.store.execute('UPDATE owner_authority_revisions SET record_sha256=%s WHERE revision=4',
                           ("0" * 64,))
    elif change == "policy":
        value = json.loads(revisions[1]["policy_json"])
        value["unknown_carry_sha256"] = "0" * 64
        core.store.execute('UPDATE owner_authority_revisions SET policy_json=%s WHERE revision=4',
                           (json.dumps(value),))
    else:
        core.store.execute("UPDATE owner_authority_revisions SET policy_redacted=1 "
                           "WHERE revision=4")
        if change == "erased":
            core.store.execute("UPDATE owner_authority_revisions SET policy_json='{}' "
                               "WHERE revision=4")
    with core.store.transaction():
        if change == "erased":
            model_carry.authority_chain(core.store, saved, revisions)
        else:
            with pytest.raises(ValueError):
                model_carry.authority_chain(core.store, saved, revisions)


def local_receipt(core, case):
    return model_carry.read(
        core.store,
        core.settings,
        case.successor["authority"],
        case.receipt["carry_sha256"],
        digest(case.policy.model_dump(mode="json")),
    )


def test_current_unknown_uses_true_local_close_and_same_approval(core, tmp_path):
    case = migrated(core, tmp_path)
    request_id = current_unknown(core)
    stopped = stop_current(core, case)
    before = core.store.rows("SELECT * FROM model_usage")
    proof = local_receipt(core, case)
    assert proof["kind"] == wire.LOCAL_KIND and proof["version"] == 2
    assert proof["origin_authority"] == case.active["authority"]
    assert proof["authority"] == case.successor["authority"]
    assert proof["request_ids"] == [request_id]
    assert proof["core_stop_sha256"] == stopped["record_sha256"]
    assert proof["carry_sha256"] == case.receipt["carry_sha256"]
    capture = json.loads(
        core.store.one(
            'SELECT value FROM runtime_meta WHERE key=%s',
            ("owner_input:" + proof["inputs"][0]["event_id"],),
        )["value"]
    )["capture"]
    state = owner_input_state.read(core.store, core.settings, OwnerCapture.model_validate(capture))
    assert wire.carried(state, capture, proof) == state
    assert state["unsettled_model"] and not state["terminal"]
    activate(core, case.policy, revision=4, operation="resume-current-closed-unknown")
    assert local_receipt(core, case) == proof
    assert core.store.rows("SELECT * FROM model_usage") == before
    with core.store.transaction():
        view = owner_authority.custody(core.store, core.settings, settlement=True)
    assert len(view["model_carries"]) == 1
    close = view["model_controls"][proof["local_closures"][0]["record_sha256"]]
    assert close["kind"] == model_carry.CLOSE and close["request_id"] == request_id
    assert model_carry.scope_windows(view, core.settings.paid_model.window_id) == [
        "offline-window-1",
        "successor-window-1",
    ]
    # Another native STOP/activation reuses the same approval and retained liability.
    owner_authority.stop(
        core.store,
        core.settings,
        core.clock,
        AuthorityStop(
            operation_id="third-normal-stop",
            authority=case.successor["authority"] | {"revision": 5},
        ),
    )
    activate(core, case.policy, revision=6, operation="third-normal-activation")
    assert core.store.rows("SELECT * FROM model_usage") == before


def test_current_proof_stays_stable_with_approved_unadmitted_input(core, tmp_path):
    case = migrated(core, tmp_path)
    current_unknown(core)
    saved = json.loads(
        core.store.one(

                'SELECT value FROM runtime_meta WHERE key LIKE '
                "'owner_input:%%' AND (CASE WHEN value IS JSON THEN "
                "value::jsonb #> '{capture,authority,revision}' "
                "END)='3'::jsonb"

        )["value"]
    )["capture"]
    value = {key: item for key, item in saved.items() if key != "signature"}
    value["source_event_id"] = value["source_event_id"].rsplit(":", 1)[0] + ":999"
    value["event_id"] = "evt_" + hashlib.sha256(value["source_event_id"].encode()).hexdigest()
    value["signature"] = case.key.sign(capture_signing_payload(value)).hex()
    capture = OwnerCapture.model_validate(value)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    stop_current(core, case)
    assert owner_input_state.read(core.store, core.settings, capture)["terminal"]
    proof = local_receipt(core, case)
    assert capture.event_id not in {item["event_id"] for item in proof["inputs"]}
    activate(core, case.policy, revision=4, operation="resume-with-unadmitted-input")
    assert local_receipt(core, case) == proof


@pytest.mark.parametrize("change", ["unclosed", "approval", "generation", "policy", "restore"])
def test_continuation_rejects_unclosed_or_changed_scope(core, tmp_path, monkeypatch, change):
    case = migrated(core, tmp_path)
    if change == "unclosed":
        monkeypatch.setattr(model_carry, "close_owned", lambda *args: None)
    current_unknown(core)
    stop_current(core, case)
    if change == "approval":
        core.settings.paid_model = core.settings.paid_model.model_copy(update={"window_calls": 100})
    elif change == "generation":
        with core.store.transaction():
            owner_authority._put(core.store, owner_authority.GENERATION, "new-generation")
    elif change == "policy":
        case.policy = case.policy.model_copy(update={"authorization_sha256": "9" * 64})
    elif change == "restore":
        with core.store.transaction():
            core.store.execute(
                "INSERT INTO runtime_meta VALUES ('model_budget_restore_required','1')"
            )
    before = core.store.rows("SELECT * FROM model_usage")
    with pytest.raises(CoreError):
        activate(core, case.policy, revision=4, operation="reject-changed-continuation")
    with pytest.raises(CoreError):
        local_receipt(core, case)
    assert owner_authority._latest(core.store)["state"] == "STOPPED"
    assert core.store.rows("SELECT * FROM model_usage") == before


@pytest.mark.parametrize("change", ["kind", "origin", "closure", "closure_digest", "ids"])
def test_local_wire_proof_rejects_changed_binding(core, tmp_path, change):
    case = migrated(core, tmp_path)
    current_unknown(core)
    stop_current(core, case)
    proof = copy.deepcopy(local_receipt(core, case))
    if change == "kind":
        proof["kind"] = wire.KIND
    elif change == "origin":
        proof["origin_authority"]["grant_id"] = "grant_" + "0" * 32
    elif change == "closure":
        proof["local_closures"][0]["record_sha256"] = "invalid"
    elif change == "closure_digest":
        proof["closed_executors_sha256"] = "0" * 64
    else:
        proof["local_closures"][0]["request_id"] = "model_" + "0" * 32
    proof["receipt_sha256"] = digest({k: v for k, v in proof.items() if k != "receipt_sha256"})
    with pytest.raises(ValueError):
        wire.receipt(proof)
