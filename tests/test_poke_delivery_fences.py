"""Native erasure closure and pre-delegate evidence for interactive notices."""

import pytest

from companion import input_protocol, poke
from companion.contracts import CoreError
from companion.store import after, uid
from tests.poke_authority_fixture import environment as native_environment
from tests.poke_authority_fixture import owner_turn, settle
from tests.support.poke import claim, receive


@pytest.fixture
def environment(core, tmp_path):
    return native_environment(core, tmp_path)


@pytest.mark.parametrize("kind", ["poke", "text"])
@pytest.mark.parametrize("evidence", ["valid", "changed_hash", "delivery_observed"])
def test_consumed_history_recall_preserves_genuine_not_sent_proof(environment, kind, evidence):
    env = environment
    source = owner_turn(env, 1, "context the owner will recall")
    receive(env)
    job = claim(env)
    choice = {"kind": kind, **({"text": "short reply"} if kind == "text" else {})}
    settle(env, job, choice)
    message_id = poke.finish(env.store, env.settings, env.approval, job, choice, env.clock())[0]
    original = env.store.one('SELECT * FROM outbox WHERE message_id=%s', (message_id,))
    token = uid("lease")
    with env.store.transaction(control_write=True):
        env.store.execute((
            'UPDATE outbox SET '
            "state='SENDING',lease_token=%s,lease_until=%s,attempt_count=1 "
            'WHERE message_id=%s'
        ),
                          (token, after(env.clock(), 60), message_id))
        env.core.ledger.expire_events(env.store, [source["event_id"]], env.clock())
        if evidence == "delivery_observed":
            env.store.audit("delivery.result", message_id, "delivery", env.clock(),
                            state="DELIVERY_UNCERTAIN")
    erased = env.store.one('SELECT * FROM outbox WHERE message_id=%s', (message_id,))
    assert erased["state"] == "DELIVERY_UNCERTAIN" and erased["content_sha256"] == ""
    assert not env.store.one('SELECT 1 FROM runtime_meta WHERE key=%s',
                             ("erased_event:" + job["event_id"],))
    with pytest.raises(CoreError, match="REPLY_SOURCE_RECALLED"):
        poke.check_delivery(env.store, env.settings, env.approval, message_id, clock=env.clock)
    receipt = input_protocol.OwnerCancelBeforeSend(
        authority=env.approval.authority.as_dict(), message_id=message_id, lease_token=token,
        content_sha256="0" * 64 if evidence == "changed_hash" else original["content_sha256"],
    )
    if evidence == "valid":
        result = input_protocol.cancel_before_send(env.store, env.settings, env.clock, receipt)
        assert result["state"] == "CANCELLED" and result["send_attempt_retained"]
        assert input_protocol.cancel_before_send(
            env.store, env.settings, env.clock, receipt) == result
    else:
        with pytest.raises(CoreError, match="INPUT_CANCEL_BEFORE_SEND_UNPROVEN"):
            input_protocol.cancel_before_send(env.store, env.settings, env.clock, receipt)
    saved = env.store.one('SELECT state,attempt_count FROM outbox WHERE message_id=%s',
                          (message_id,))
    assert saved == {"state": "CANCELLED" if evidence == "valid" else "DELIVERY_UNCERTAIN",
                     "attempt_count": 1}
