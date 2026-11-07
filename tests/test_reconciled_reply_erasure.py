"""Historical delivery evidence survives an unrelated native memory erasure."""

from copy import deepcopy

import pytest

from companion import delivery_reconciliation as reconciliation
from companion import paid_budget
from companion.contracts import CoreError
from companion.store import after
from tests.support.confirmed_memory import remember
from tests.support.delivery_reconciliation import decide, native, prepare
from tests.support.owner_delivery_reconciliation import pending


def unrelated_memory(core):
    memory_id = remember(core, 800, "我偏爱星空与简洁的表达。")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE event_id='evt_test_800'")
    core.settings.memory_enabled = False
    return memory_id


def resolved(core, tmp_path, kind, outcome):
    if kind == "owner":
        proof, _ = pending(core, tmp_path)
        reconciliation.prepare(core.store, core.settings, proof, core.clock())
        result = reconciliation.resolve(core.store, core.settings, proof["message_id"], outcome,
            core.clock(), operation_id="resolved-before-unrelated-erasure",
            operation_sha256="e" * 64)
        return proof["message_id"], result["receipt"]
    fixture = native(core, tmp_path, action="qq.private.reply")
    prepare(fixture)
    return fixture.message_id, decide(fixture, outcome)["receipt"]


@pytest.mark.parametrize("kind", ["owner", "window"])
@pytest.mark.parametrize("outcome", ["SENT", "CANCELLED"])
def test_unrelated_memory_erasure_keeps_reconciled_journal_replayable(
    core, tmp_path, kind, outcome,
):
    memory_id = unrelated_memory(core)
    message_id, receipt = resolved(core, tmp_path, kind, outcome)
    original = core.store.one('SELECT * FROM outbox WHERE message_id=%s', (message_id,))
    usage = deepcopy(core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence'))
    with core.store.transaction():
        core.ledger.append(core.store, memory_id, core.clock())
    assert core.store.one('SELECT redacted FROM events WHERE event_id=%s',
                          (original["origin_event_id"],))["redacted"] == 0
    erased = core.store.one('SELECT * FROM outbox WHERE message_id=%s', (message_id,))
    assert erased == {**original, "content_json": "{}", "content_sha256": ""}
    ledger = core.ledger.path.read_bytes()
    paid_budget._validated_bytes.cache_clear()
    assert reconciliation.lookup(core.store, core.settings, message_id) == receipt
    assert paid_budget.journal(core.store, core.settings.paid_model,
                               allow_settlement=True)["send_resolutions"][message_id]
    assert core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence') == usage
    assert core.store.one('SELECT * FROM outbox WHERE message_id=%s', (message_id,)) == erased
    assert core.ledger.path.read_bytes() == ledger


@pytest.mark.parametrize("kind", ["owner", "window"])
@pytest.mark.parametrize("reason", [
    "missing", "older", "incomplete", "wrong_type", "bad_timestamp", "nonempty",
])
def test_cleared_reply_requires_a_completed_covering_memory_erasure(
    core, tmp_path, kind, reason,
):
    memory_id = unrelated_memory(core)
    if reason == "older":
        with core.store.transaction():
            core.ledger.append(core.store, memory_id, core.clock())
        core.clock.advance(1)
    message_id, _ = resolved(core, tmp_path, kind, "CANCELLED")
    with core.store.transaction():
        if reason not in {"missing", "older"}:
            core.ledger.append(core.store, memory_id, core.clock())
        core.store.execute((
            "UPDATE outbox SET content_json='{}',content_sha256='' WHERE "
            'message_id=%s'
        ), (message_id,))
        if reason == "older":
            # The legacy window fixture resets its clock; make this deliberately
            # stale marker precede the actual frozen delivery in either fixture.
            created = core.store.one('SELECT created_at FROM outbox WHERE message_id=%s',
                                     (message_id,))["created_at"]
            core.store.execute('UPDATE privacy_erasure_ledger SET erased_at=%s',
                               (after(created, -1),))
        elif reason == "incomplete":
            core.store.execute("UPDATE privacy_erasure_ledger SET state='PENDING'")
        elif reason == "wrong_type":
            core.store.execute("UPDATE privacy_erasure_ledger SET target_type='diary'")
        elif reason == "bad_timestamp":
            core.store.execute("UPDATE privacy_erasure_ledger SET erased_at='invalid'")
        elif reason == "nonempty":
            core.store.execute('UPDATE outbox SET content_json=%s WHERE message_id=%s',
                               ('{"text":"unproved replacement"}', message_id))
    paid_budget._validated_bytes.cache_clear()
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        reconciliation.lookup(core.store, core.settings, message_id)
