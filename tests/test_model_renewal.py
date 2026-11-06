"""Renewal retains original carry custody and every historical charge.

It may happen before or after the old expiry, and the successor may be open-ended.
"""

import copy
import json
from datetime import timedelta
from pathlib import Path

import pytest

from companion import (
    budget,
    model_carry,
    owner_authority,
    owner_input,
    owner_input_state,
    paid_budget,
)
from companion.contracts import CoreError
from companion.life_topics import _uncertain
from companion.model_carry_controller import verify
from companion.owner_authority_protocol import AuthorityStop, OwnerCapture
from companion_delivery_protocol import digest
from companion_delivery_protocol import model_carry as wire
from tests.model_carry_support import MESSAGES, executors, migrated, observer
from tests.owner_model_fixtures import authorized_job
from tests.support.owner_authority import activate


def renewal(core, case, *, number=1, offset=timedelta(seconds=1), open_ended=False,
            concurrency=None):
    active = case.successor["authority"]
    previous = paid_budget.snapshot(core.settings)
    expiry = core.settings.paid_model.expires_at
    core.clock.now = expiry + offset
    owner_authority.stop(
        core.store,
        core.settings,
        core.clock,
        AuthorityStop(operation_id=f"stop-renewal-{number}", authority=active),
    )
    expires = None if open_ended else expiry + timedelta(days=1)
    core.settings.paid_model = core.settings.paid_model.model_copy(
        update={
            "window_id": f"renewed-window-{number}",
            "approval_sha256": str(number) * 64,
            "expires_at": expires,
            "concurrency": concurrency or core.settings.paid_model.concurrency,
            "rates_valid_until": None if open_ended else max(
                core.settings.paid_model.rates_valid_until, expires),
        }
    )
    items = executors(core.clock())
    witness = verify(items, observe=observer(items))
    with core.store.transaction():
        view = owner_authority.custody(core.store, core.settings, settlement=True)
        value = model_carry.manifest(
            core.store, core.settings, active, core.settings.paid_model.window_id, items, view
        )
    policy = case.policy.model_copy(
        update={
            "authorization_sha256": str(number + 2) * 64,
            "unknown_carry_sha256": digest(value),
            "expires_at": expires,
            "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings)),
        }
    )
    request = {
        "version": 2,
        "operation_id": f"renewal-operation-{number}",
        "authorization_sha256": policy.authorization_sha256,
        "expected_head_sha256": view["head_sha256"],
        "authority": active,
        "successor_window_id": core.settings.paid_model.window_id,
        "manifest_sha256": digest(value),
        "successor_policy": policy.model_dump(mode="json"),
        "executors": items,
        "previous_paid_contract": previous,
    }
    return request, policy, witness


def test_reviewed_renewal_can_raise_capacity_to_ten_without_changing_old_liability(core, tmp_path):
    case = migrated(core, tmp_path, daily_calls=100, window_calls=100,
                    expires_at=core.clock.now + timedelta(hours=1),
                    rates_valid_until=core.clock.now + timedelta(days=1))
    before = core.store.rows("SELECT * FROM model_usage")
    request, policy, witness = renewal(core, case, concurrency=10)
    receipt = model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    active, _ = activate(core, policy, revision=4, operation="renew-capacity-ten")
    core.authorized_model_test_capture = active, case.key
    core.authorized_model_test_jobs = {}
    job = authorized_job(core, number=2)
    for index in range(10):
        budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock(),
                       clock=core.clock, logical_request_id=f"capacity-renewal-{index}")
    with pytest.raises(CoreError, match="MODEL_PROVIDER_CONCURRENCY_EXHAUSTED"):
        budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock(),
                       clock=core.clock, logical_request_id="capacity-renewal-overflow")
    assert core.store.one("SELECT * FROM model_usage WHERE request_id=?",
                          (case.request_id,)) == before[0]
    with core.store.transaction():
        saved = paid_budget.journal(core.store, core.settings.paid_model)["model_carries"]
    assert saved[receipt["carry_sha256"]]["concurrency"] == 10


def test_expired_renewal_preserves_original_unknown_and_cumulative_calls(core, tmp_path):
    case = migrated(core, tmp_path, window_calls=1)
    before = core.store.rows("SELECT * FROM model_usage")
    request, policy, witness = renewal(core, case)
    receipt = model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    assert receipt["kind"] == "owner_model_approval_renewal_v1"
    with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_REQUIRED"):
        model_carry.read(core.store, core.settings, request["authority"],
                         receipt["carry_sha256"], receipt["successor_policy_sha256"])
    assert receipt["retained_micro_usd"] == before[0]["billed_micro_usd"]
    assert model_carry.prepare(core.store, core.settings, request, witness, core.clock()) == receipt
    active, _ = activate(core, policy, revision=4, operation="activate-renewal")
    core.authorized_model_test_capture = active, case.key
    core.authorized_model_test_jobs = {}
    assert core.store.rows("SELECT * FROM model_usage") == before
    assert not _uncertain(core.store, core.settings)
    with pytest.raises(CoreError):
        owner_input.approve(core.store, core.settings, core.clock, case.capture)
    job = authorized_job(core, number=2)
    with pytest.raises(CoreError, match="MODEL_WINDOW_BUDGET_EXHAUSTED"):
        budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())


def test_second_renewal_keeps_all_windows_and_immutable_prior_approval(core, tmp_path):
    case = migrated(core, tmp_path)
    before = core.store.rows("SELECT * FROM model_usage")
    for number, revision in ((1, 4), (2, 6)):
        request, policy, witness = renewal(core, case, number=number)
        model_carry.prepare(core.store, core.settings, request, witness, core.clock())
        active, _ = activate(
            core, policy, revision=revision, operation=f"activate-renewal-{number}"
        )
        case.successor, case.policy = active, policy
    with core.store.transaction():
        view = paid_budget.journal(core.store, core.settings.paid_model)
    assert model_carry.scope_windows(view, "renewed-window-2") == [
        "offline-window-1",
        "renewed-window-1",
        "renewed-window-2",
        "successor-window-1",
    ]
    assert len(view["model_carries"]) == 3
    assert core.store.rows("SELECT * FROM model_usage") == before


def test_renewal_replay_batches_windows_at_each_original_cutoff(core, tmp_path):
    from tests.test_model_carry_lifecycle import current_unknown

    case = migrated(core, tmp_path, expires_at=core.clock.now + timedelta(hours=2),
                    rates_valid_until=core.clock.now + timedelta(days=4))
    for number, revision in ((1, 4), (2, 6)):
        current_unknown(core, number=number + 1)
        request, policy, witness = renewal(core, case, number=number)
        model_carry.prepare(core.store, core.settings, request, witness, core.clock())
        active, _ = activate(core, policy, revision=revision,
                             operation=f"activate-batched-renewal-{number}")
        case.successor, case.policy = active, policy
        core.authorized_model_test_capture = active, case.key
        core.authorized_model_test_jobs = {}
    before = core.store.rows("SELECT * FROM model_usage ORDER BY request_id")
    assert len(before) == 3
    queries = []
    paid_budget.forget_views(core.store)
    core.store.db.set_trace_callback(queries.append)
    try:
        with core.store.transaction():
            view = paid_budget.journal(core.store, core.settings.paid_model)
    finally:
        core.store.db.set_trace_callback(None)
    # Each historical cutoff retains its original baseline despite newer admissions.
    assert len(view["model_carries"]) == 3
    assert core.store.rows("SELECT * FROM model_usage ORDER BY request_id") == before
    baseline_reads = [query for query in queries if query.startswith((
        "SELECT window_id FROM model_admissions",
        "SELECT request_id,window_id FROM model_admissions",
    ))]
    assert len(baseline_reads) == 2  # One current table read per historical renewal.


@pytest.mark.parametrize("change", ["missing", "window"])
def test_renewal_replay_rejects_changed_baseline_admission(core, tmp_path, change):
    case = migrated(core, tmp_path)
    request, policy, witness = renewal(core, case)
    model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    activate(core, policy, revision=4, operation="activate-baseline-custody")
    before = core.store.rows("SELECT * FROM model_usage")
    if change == "missing":
        core.store.execute("DELETE FROM model_admissions WHERE request_id=?", (case.request_id,))
    else:
        core.store.insert("model_budget_windows", window_id="unrelated-window",
                          contract_sha256="0" * 64)
        core.store.execute("UPDATE model_admissions SET window_id=? WHERE request_id=?",
                           ("unrelated-window", case.request_id))
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        with core.store.transaction():
            paid_budget.journal(core.store, core.settings.paid_model)
    assert core.store.rows("SELECT * FROM model_usage") == before


@pytest.mark.parametrize("change", ["price", "cap", "start", "purpose", "old_contract", "head"])
def test_renewal_rejects_scope_or_accounting_changes(core, tmp_path, change):
    case = migrated(core, tmp_path)
    request, policy, witness = renewal(core, case)
    if change in {"price", "cap", "start"}:
        updates = {
            "price": {"input_rate": 1},
            "cap": {"window_calls": 21},
            "start": {"starts_at": core.settings.paid_model.starts_at + timedelta(seconds=1)},
        }
        core.settings.paid_model = core.settings.paid_model.model_copy(update=updates[change])
        request["successor_policy"]["paid_contract_sha256"] = paid_budget.sha(
            paid_budget.snapshot(core.settings)
        )
    elif change == "purpose":
        request["successor_policy"]["purposes"].append("goals")
    elif change == "old_contract":
        request["previous_paid_contract"]["window_calls"] += 1
    else:
        request["expected_head_sha256"] = "0" * 64
    path = Path(core.settings.paid_model.journal_path)
    before = path.read_bytes()
    with pytest.raises(CoreError):
        model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    assert path.read_bytes() == before


def test_renewal_before_old_expiry_keeps_custody_without_a_gap(core, tmp_path):
    case = migrated(core, tmp_path)
    before = core.store.rows("SELECT * FROM model_usage")
    request, policy, witness = renewal(core, case, offset=-timedelta(hours=1))
    receipt = model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    assert receipt["kind"] == "owner_model_approval_renewal_v1"
    assert receipt["retained_micro_usd"] == before[0]["billed_micro_usd"]
    activate(core, policy, revision=4, operation="activate-early-renewal")
    assert core.store.rows("SELECT * FROM model_usage") == before
    with core.store.transaction():
        view = paid_budget.journal(core.store, core.settings.paid_model)
    assert "successor-window-1" in model_carry.scope_windows(view, "renewed-window-1")


@pytest.mark.parametrize("change", ["not_later", "already_past", "rates_shorter"])
def test_renewal_must_extend_into_the_future(core, tmp_path, change):
    case = migrated(core, tmp_path)
    old = core.settings.paid_model
    request, policy, witness = renewal(core, case, offset=-timedelta(hours=1))
    updates = {
        "not_later": {"expires_at": old.expires_at},
        "already_past": {"expires_at": core.clock.now - timedelta(seconds=1),
                         "rates_valid_until": old.rates_valid_until},
        "rates_shorter": {"rates_valid_until": None},
    }[change]
    if change == "already_past":
        core.clock.now = old.expires_at + timedelta(hours=2)
    core.settings.paid_model = core.settings.paid_model.model_copy(update=updates)
    request["successor_policy"]["paid_contract_sha256"] = paid_budget.sha(
        paid_budget.snapshot(core.settings))
    with pytest.raises(CoreError):
        model_carry.prepare(core.store, core.settings, request, witness, core.clock())


@pytest.mark.parametrize("delta", [2048, -1])
def test_renewal_may_only_raise_the_output_ceiling(core, tmp_path, delta):
    case = migrated(core, tmp_path)
    request, policy, witness = renewal(core, case)
    ceiling = core.settings.paid_model.max_output_tokens + delta
    core.settings.paid_model = core.settings.paid_model.model_copy(
        update={"max_output_tokens": ceiling})
    contract = paid_budget.sha(paid_budget.snapshot(core.settings))
    request["successor_policy"]["paid_contract_sha256"] = contract
    policy = policy.model_copy(update={"paid_contract_sha256": contract})
    path = Path(core.settings.paid_model.journal_path)
    before = path.read_bytes()
    if delta < 0:
        with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_APPROVAL_CHANGED"):
            model_carry.prepare(core.store, core.settings, request, witness, core.clock())
        assert path.read_bytes() == before
        return
    model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    activate(core, policy, revision=4, operation="activate-raised-output")
    assert paid_budget.contract(core.settings, core.clock()).max_output_tokens == ceiling


def test_open_ended_renewal_keeps_caps_and_has_no_expiry(core, tmp_path):
    case = migrated(core, tmp_path, window_calls=1)
    request, policy, witness = renewal(core, case, offset=-timedelta(hours=1), open_ended=True)
    assert policy.expires_at is None
    assert "expires_at" not in paid_budget.snapshot(core.settings)
    model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    active, _ = activate(core, policy, revision=4, operation="activate-open-renewal")
    core.authorized_model_test_capture = active, case.key
    core.authorized_model_test_jobs = {}
    core.clock.now += timedelta(days=3650)
    assert paid_budget.contract(core.settings, core.clock()) is core.settings.paid_model
    job = authorized_job(core, number=2)
    # The window call cap carried across the chain still applies with no expiry.
    with pytest.raises(CoreError, match="MODEL_WINDOW_BUDGET_EXHAUSTED"):
        budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())


@pytest.mark.parametrize("current_closed", [False, True])
def test_renewal_fsync_replay_requires_the_exact_reviewed_request(
    core, tmp_path, monkeypatch, current_closed,
):
    case = migrated(core, tmp_path)
    if current_closed:
        from tests.test_model_carry_lifecycle import current_unknown

        current_unknown(core)
    request, policy, witness = renewal(core, case)
    execute = core.store.execute

    def crash(query, values=()):
        if query == "INSERT INTO runtime_meta VALUES (?,?)" and values[0].startswith(
            "model_control:"
        ):
            raise RuntimeError("crash after renewal fsync")
        return execute(query, values)

    with monkeypatch.context() as patch:
        patch.setattr(core.store, "execute", crash)
        with pytest.raises(RuntimeError, match="after renewal fsync"):
            model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    assert _uncertain(core.store, core.settings)
    changed = copy.deepcopy(request)
    changed["authorization_sha256"] = "a" * 64
    with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_CHANGED"):
        model_carry.prepare(core.store, core.settings, changed, witness, core.clock())
    model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    rows = [
        json.loads(line)
        for line in Path(core.settings.paid_model.journal_path).read_text().splitlines()
    ]
    assert sum(r["kind"] == model_carry.RENEW for r in rows) == 1


def test_new_journal_closed_unknown_no_longer_pauses_life(core, tmp_path):
    from tests.test_model_carry_lifecycle import current_unknown

    migrated(core, tmp_path)
    assert not _uncertain(core.store, core.settings)
    request_id = current_unknown(core)
    assert core.store.one("SELECT state FROM model_usage WHERE request_id=?",
                          (request_id,))["state"] == "UNKNOWN"
    # Owner decision 2026-11-01: closed liability counts as settled for Life too.
    assert not _uncertain(core.store, core.settings)


@pytest.mark.parametrize("prior_renewal", [False, True])
def test_renewal_retains_new_closed_unknown_and_uses_existing_input_handoff(
    core, tmp_path, prior_renewal,
):
    from tests.test_model_carry_lifecycle import current_unknown

    case = migrated(core, tmp_path, expires_at=core.clock.now + timedelta(hours=2),
                    rates_valid_until=core.clock.now + timedelta(days=4))
    if prior_renewal:
        request, policy, witness = renewal(core, case)
        model_carry.prepare(core.store, core.settings, request, witness, core.clock())
        active, _ = activate(core, policy, revision=4, operation="first-renewal")
        case.successor, case.policy = active, policy
        core.authorized_model_test_capture = active, case.key
        core.authorized_model_test_jobs = {}
    current = current_unknown(core)
    before = core.store.rows("SELECT * FROM model_usage ORDER BY request_id")
    assert not _uncertain(core.store, core.settings)  # journal-closed: Life is not paused
    request, policy, witness = renewal(core, case, number=2 if prior_renewal else 1)
    receipt = model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    assert receipt["retained_micro_usd"] == sum(row["billed_micro_usd"] for row in before)
    proof = model_carry.read(core.store, core.settings, request["authority"],
                             receipt["carry_sha256"], receipt["successor_policy_sha256"])
    assert proof["kind"] == wire.KIND and proof["version"] == 1
    assert proof["request_ids"] == [current]
    assert proof["retained_micro_usd"] == next(
        row["billed_micro_usd"] for row in before if row["request_id"] == current
    )
    capture = OwnerCapture.model_validate(json.loads(core.store.one(
        "SELECT value FROM runtime_meta WHERE key=?",
        ("owner_input:" + proof["inputs"][0]["event_id"],),
    )["value"])["capture"])
    state = owner_input_state.read(core.store, core.settings, capture)
    assert not state["terminal"] and state["unsettled_model"]
    assert wire.carried(state, capture.model_dump(mode="json"), proof) == state
    active, _ = activate(core, policy, revision=request["authority"]["revision"] + 1,
                         operation="activate-closed-input-renewal")
    assert not _uncertain(core.store, core.settings)
    assert model_carry.read(core.store, core.settings, request["authority"],
                            receipt["carry_sha256"], receipt["successor_policy_sha256"]) == proof
    assert core.store.rows("SELECT * FROM model_usage ORDER BY request_id") == before
    with pytest.raises(CoreError):
        owner_input.approve(core.store, core.settings, core.clock, capture)
    # A later STOP still uses the original local-close continuation protocol,
    # including when its predecessor record is a renewal rather than a first carry.
    core.authorized_model_test_capture = active, case.key
    core.authorized_model_test_jobs = {}
    later = current_unknown(core)
    assert not _uncertain(core.store, core.settings)  # journal-closed: Life is not paused
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="stop-after-renewal", authority=active["authority"],
    ))
    continuation = model_carry.read(core.store, core.settings, active["authority"],
                                    receipt["carry_sha256"], receipt["successor_policy_sha256"])
    assert continuation["kind"] == wire.LOCAL_KIND and continuation["request_ids"] == [later]
    activate(core, policy, revision=active["authority"]["revision"] + 1,
             operation="same-approval-after-renewal")
    for row in before:
        assert core.store.one("SELECT * FROM model_usage WHERE request_id=?",
                               (row["request_id"],)) == row


def test_renewal_rejects_current_unknown_without_local_close(core, tmp_path, monkeypatch):
    from tests.test_model_carry_lifecycle import current_unknown

    case = migrated(core, tmp_path)
    monkeypatch.setattr(model_carry, "close_owned", lambda *args: None)
    current_unknown(core)
    with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_NOT_QUIESCENT"):
        renewal(core, case)
    rows = [json.loads(line)
            for line in Path(core.settings.paid_model.journal_path).read_text().splitlines()]
    assert not any(row["kind"] == model_carry.RENEW for row in rows)


@pytest.mark.parametrize(("handoffs", "with_current", "prior_renewal", "erased"), [
    (1, False, False, False), (2, False, True, False),
    (1, True, False, False), (2, True, True, False), (1, False, False, True),
])
def test_renewal_keeps_closed_unknown_from_an_earlier_same_approval_revision(
    core, tmp_path, handoffs, with_current, prior_renewal, erased,
):
    from tests.test_model_carry_lifecycle import current_unknown

    case = migrated(core, tmp_path, expires_at=core.clock.now + timedelta(hours=2),
                    rates_valid_until=core.clock.now + timedelta(days=4))
    if prior_renewal:
        request, policy, witness = renewal(core, case)
        model_carry.prepare(core.store, core.settings, request, witness, core.clock())
        active, _ = activate(core, policy, revision=4, operation="renew-before-history")
        case.successor, case.policy = active, policy
        core.authorized_model_test_capture = active, case.key
        core.authorized_model_test_jobs = {}
    historical = current_unknown(core)
    source = core.store.one("SELECT j.event_id FROM jobs j JOIN model_usage m USING(job_id) "
                            "WHERE m.request_id=?", (historical,))["event_id"]
    for number in range(handoffs):
        authority = case.successor["authority"]
        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id=f"history-stop-{number}", authority=authority))
        active, _ = activate(core, case.policy, revision=authority["revision"] + 1,
                             operation=f"history-resume-{number}")
        case.successor = active
    if erased:
        with core.store.transaction():
            core.ledger.expire_events(core.store, [source], core.clock())
    current = None
    if with_current:
        core.authorized_model_test_capture = case.successor, case.key
        core.authorized_model_test_jobs = {}
        current = current_unknown(core, number=3)
    before = core.store.rows("SELECT * FROM model_usage ORDER BY request_id")
    request, policy, witness = renewal(core, case, number=2 if prior_renewal else 1,
                                       concurrency=10)
    assert request["authority"]["revision"] > json.loads(core.store.one(
        "SELECT authority_json FROM model_admissions WHERE request_id=?",
        (historical,),
    )["authority_json"])["revision"]
    receipt = model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    assert receipt["retained_micro_usd"] == sum(v["billed_micro_usd"] for v in before)
    with core.store.transaction():
        view = owner_authority.custody(core.store, core.settings, settlement=True)
    saved = view["model_carries"][receipt["carry_sha256"]]
    assert {key for item in saved["manifest"]["inputs"] for key in item["request_ids"]} == (
        {current} if current else set())
    assert {v["request_id"] for v in saved["manifest"]["unknowns"]} == {
        v["request_id"] for v in before}
    if current:
        proof = model_carry.read(core.store, core.settings, request["authority"],
                                 receipt["carry_sha256"], receipt["successor_policy_sha256"])
        assert proof["request_ids"] == [current]
    else:
        with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_REQUIRED"):
            model_carry.read(core.store, core.settings, request["authority"],
                             receipt["carry_sha256"], receipt["successor_policy_sha256"])
    activate(core, policy, revision=request["authority"]["revision"] + 1,
             operation="renew-with-retained-previous-revision")
    assert core.store.rows("SELECT * FROM model_usage ORDER BY request_id") == before
    assert not core.store.rows("SELECT * FROM outbox")
    if erased:
        assert core.store.one("SELECT redacted FROM events WHERE event_id=?",
                               (source,))["redacted"]


def test_life_does_not_trust_unproved_unknown_or_recovering_journal(core, tmp_path):
    case = migrated(core, tmp_path)
    assert not _uncertain(core.store, core.settings)
    core.store.execute("DELETE FROM runtime_meta WHERE key LIKE 'model_control:%'")
    assert _uncertain(core.store, core.settings)
    assert (
        core.store.one("SELECT state FROM model_usage WHERE request_id=?", (case.request_id,))[
            "state"
        ]
        == "UNKNOWN"
    )
