"""Original durable journals and signed synthetic proofs; no provider/QQ I/O.

The frozen delivery fixture supplies previously checked native authority. Separate
entry-point tests cover capture/dispatch and the Bridge process-lock proof producer.
"""

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import companion_delivery_protocol as wire
from companion import cumulative, live_text, paid_budget
from companion import delivery_reconciliation as reconciliation
from companion.contracts import CoreError, DeliveryResult
from companion.privacy import ErasureLedger
from companion.store import Store, after, encode, uid
from companion.transport import delivery_result
from tests.support.cumulative_budget import policy, view
from tests.support.delivery_reconciliation import decide, native, prepare, put, signed


@pytest.mark.parametrize("action", sorted(wire.ACCOUNT_ACTIONS))
@pytest.mark.parametrize("outcome", ["SENT", "CANCELLED"])
@pytest.mark.parametrize("expired", [False, True])
def test_account_resolution_keeps_native_unknown_and_original_reservations(
    core, tmp_path, action, outcome, expired,
):
    env = native(core, tmp_path, action=action, uncertain=not expired, expired=expired)
    before = deepcopy(view(core))
    reservation = core.store.one("SELECT * FROM social_autonomy_reservations")
    plan = core.store.one("SELECT * FROM social_autonomy_plans")
    assert not reconciliation.is_resolved_account(core.store, env.message_id)
    prepare(env)
    result = decide(env, outcome)
    receipt = wire.validate_receipt(result["receipt"])
    assert receipt["input_closure"] is None and receipt["delivery"]["event_id"] is None
    assert "event_id" not in receipt["quiescence"]
    assert receipt["delivery"]["account_plan_id"] == plan["plan_id"]
    assert receipt["delivery"]["account_receipt_sha256"] == reservation["receipt_sha256"]
    assert receipt["prior_quota_state"] == ("RESERVED" if expired else "DELIVERY_UNCERTAIN")
    assert core.store.one("SELECT * FROM social_autonomy_reservations") == reservation
    assert core.store.one("SELECT * FROM social_autonomy_plans") == plan
    after_state = view(core)
    for key, liability in before["operations"].items():
        assert {k: v for k, v in after_state["operations"][key].items() if k != "state"} == {
            k: v for k, v in liability.items() if k != "state"}
    assert after_state["models"] == before["models"] and after_state["paused"]
    assert reconciliation.is_resolved_account(core.store, env.message_id)
    assert decide(env, outcome) == result
    with pytest.raises(CoreError, match="STALE_DELIVERY_LEASE"):
        delivery_result(core.store, core.settings, env.message_id, DeliveryResult(
            lease_token=env.token, result="SENT", account_action_receipt={
                "message_id": env.message_id, "action_type": action, "lease_token": env.token,
                "payload_sha256": env.proof["payload_sha256"], "result": "SUCCEEDED",
                "native_receipt_sha256": "b" * 64, "native_retcode": 0,
                "resource_id": "synthetic-late-tid" if action == "qzone.publish" else None}),
                core.clock())


@pytest.mark.parametrize("change", ["authorization", "receipt", "reservation", "plan", "lane"])
def test_account_original_binding_drift_rejects_without_append(core, tmp_path, change):
    env = native(core, tmp_path, action="qq.profile.nickname")
    if change == "authorization":
        env.proof = signed(env, authorization_sha256="f" * 64)
    elif change == "receipt":
        core.store.execute('UPDATE social_autonomy_reservations SET receipt_sha256=%s', ("f" * 64,))
    elif change == "reservation":
        core.store.execute('UPDATE social_autonomy_reservations SET action_type=%s',
                           ("qq.profile.avatar",))
    elif change == "plan":
        core.store.execute('UPDATE social_autonomy_plans SET payload_sha256=%s', ("f" * 64,))
    else:
        core.store.execute("UPDATE outbox SET delivery_lane='owner_private'")
    before = rows(env)
    with pytest.raises(CoreError):
        prepare(env)
    assert rows(env) == before


def test_account_resolution_after_source_erasure_keeps_original_hashes(core, tmp_path):
    env = native(core, tmp_path, action="qzone.publish")
    event_id = core.store.one("SELECT event_id FROM events")["event_id"]
    with core.store.transaction():
        core.store.execute('UPDATE outbox SET origin_event_id=%s', (event_id,))
        core.store.insert("social_autonomy_sources", plan_id=env.proof["account_plan_id"],
                          event_id=event_id, role="content", source_sha256="d" * 64)
        ErasureLedger(core.settings.erasure_ledger).expire_events(
            core.store, [event_id], core.clock())
    reservation = core.store.one("SELECT * FROM social_autonomy_reservations")
    prepare(env)
    result = decide(env)
    assert result["receipt"]["delivery"]["event_id"] == event_id
    assert result["receipt"]["input_closure"] is None
    assert core.store.one("SELECT content_json FROM outbox")["content_json"] == "{}"
    assert core.store.one("SELECT * FROM social_autonomy_reservations") == reservation
    assert "synthetic public source" not in encode(result)


@pytest.mark.parametrize("action,pool", [("qq.profile.nickname", "profile"),
                                         ("qzone.publish", "qzone")])
def test_owner_closed_account_capacity_keeps_original_daily_limit_and_cooldown(
    core, tmp_path, action, pool,
):
    from companion.social_autonomy import SocialAutonomy
    from companion.social_autonomy_protocol import SocialAutonomyConfig

    env = native(core, tmp_path, action=action)
    prepare(env)
    decide(env, "CANCELLED")
    cfg = SocialAutonomyConfig(enabled=True, scope="qq_social_v1",
        authorization_sha256="a" * 64, qzone_enabled=True, nickname_enabled=True)
    core.settings.social_autonomy = cfg
    module = SocialAutonomy(core.store, core.settings, clock=core.clock)
    reservation = core.store.one("SELECT * FROM social_autonomy_reservations")
    original_quota = deepcopy(view(core))
    before = list(core.store.db.iterdump())
    assert json.loads(reservation["receipt_json"])["result"] == "UNKNOWN"
    assert reservation["released_at"] is None
    with pytest.raises(CoreError, match="SOCIAL_DAILY_ACTION_LIMIT"):
        module._capacity(action, "UTC", core.clock())
    core.settings.social_autonomy = cfg.model_copy(update={pool + "_daily_limit": 2})
    with pytest.raises(CoreError, match="SOCIAL_ACTION_COOLDOWN"):
        module._capacity(action, "UTC", core.clock())
    core.clock.advance(max(86400, getattr(cfg, pool + "_cooldown_seconds")) + 1)
    core.settings.social_autonomy = cfg
    assert module._capacity(action, "UTC", core.clock()) is None
    assert list(core.store.db.iterdump()) == before
    assert core.store.one("SELECT * FROM social_autonomy_reservations") == reservation
    assert view(core) == original_quota
    # Capacity eligibility cannot restart the original stopped execution window.
    assert live_text.stopped(core.store, env.plan.sha256)
    assert original_quota["paused"]


def test_account_capacity_rejects_forged_owner_projection_without_original_journal(core, tmp_path):
    from companion.social_autonomy import SocialAutonomy
    from companion.social_autonomy_protocol import SocialAutonomyConfig

    env = native(core, tmp_path, action="qq.profile.nickname")
    core.settings.social_autonomy = SocialAutonomyConfig(enabled=True, scope="qq_social_v1",
        authorization_sha256="a" * 64, nickname_enabled=True)
    core.store.execute("UPDATE outbox SET state='CANCELLED',lease_token=NULL")
    put(core.store, reconciliation.PROJECTION + env.message_id, "f" * 64)
    before, journal = list(core.store.db.iterdump()), rows(env)
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        SocialAutonomy(core.store, core.settings, clock=core.clock)._capacity(
            "qq.profile.nickname", "UTC", core.clock())
    assert rows(env) == journal and list(core.store.db.iterdump()) == before


@pytest.mark.parametrize("outcome", ["SENT", "CANCELLED"])
@pytest.mark.parametrize("erased", [False, True])
def test_voice_resolution_keeps_frozen_result_and_original_costs(core, tmp_path, outcome, erased):
    env = native(core, tmp_path, action="qq.private.reply", voice=True)
    if erased:
        with core.store.transaction():
            ErasureLedger(core.settings.erasure_ledger).expire_events(
                core.store, [env.proof["event_id"]], core.clock())
    before = deepcopy(view(core))
    original_job = core.store.one("SELECT * FROM voice_jobs")
    prepare(env)
    result = decide(env, outcome)
    assert result["receipt"]["quiescence"]["voice_check"] == env.proof["voice_check"]
    assert result["receipt"]["input_closure"]["terminal"] is True
    assert core.store.one("SELECT * FROM voice_jobs") == original_job
    assert view(core)["models"] == before["models"]
    assert decide(env, outcome) == result


@pytest.mark.parametrize("change", ["proof", "binding", "result", "generation", "missing"])
def test_voice_resolution_rejects_changed_original_result(core, tmp_path, change):
    env = native(core, tmp_path, action="qq.private.reply", voice=True)
    if change == "proof":
        env.proof = signed(env, voice_check={**env.proof["voice_check"], "audio_sha256": "f" * 64})
    elif change == "binding":
        value = reconciliation._meta(core.store, "voice_reply:" + env.message_id)
        value["binding"]["reply_sha256"] = "f" * 64
        put(core.store, "voice_reply:" + env.message_id, value)
    elif change == "result":
        core.store.execute('UPDATE voice_jobs SET result_sha256=%s', ("f" * 64,))
    elif change == "generation":
        core.store.execute("UPDATE voice_jobs SET generation=2")
    else:
        env.proof.pop("voice_check")
        env.proof = signed(env)
    before = rows(env)
    with pytest.raises(CoreError):
        prepare(env)
    assert rows(env) == before


@pytest.mark.parametrize("field,value", [
    ("generation", True), ("generation", 0), ("audio_bytes", "100"),
    ("audio_bytes", 4_000_001), ("job_id", "other_job"), ("extra", "unexpected"),
])
def test_voice_wire_rejects_non_exact_check(field, value):
    check = {"binding_sha256": "a" * 64, "job_id": "voice_" + "1" * 32, "generation": 1,
             "result_sha256": "b" * 64, "audio_sha256": "c" * 64, "audio_bytes": 100,
             "retirement_token": "random-proof-" + "x" * 32}
    with pytest.raises(ValueError, match="VOICE_CHECK"):
        wire.validate_voice_check({**check, field: value})


def test_voice_check_validation_returns_a_detached_dictionary():
    value = {"binding_sha256": "a" * 64, "job_id": "voice_" + "1" * 32, "generation": 1,
             "result_sha256": "b" * 64, "audio_sha256": "c" * 64, "audio_bytes": 100,
             "retirement_token": "random-proof-" + "x" * 32}
    checked = wire.validate_voice_check(value)
    assert checked == value and checked is not value
    checked["generation"] = 2
    assert value["generation"] == 1
    for missing in ({}, {key: item for key, item in value.items() if key != "audio_bytes"}):
        with pytest.raises(ValueError, match="VOICE_CHECK"):
            wire.validate_voice_check(missing)


def test_shared_wire_imports_without_core_or_bridge(tmp_path):
    import subprocess
    import sys

    package = tmp_path / "companion_delivery_protocol"
    package.mkdir()
    (package / "__init__.py").write_bytes(Path(wire.__file__).read_bytes())
    source = (
        "import importlib.util,sys;sys.path.insert(0,sys.argv[1]);"
        "import companion_delivery_protocol as wire;"
        "assert importlib.util.find_spec('companion') is None;"
        "assert importlib.util.find_spec('companion_onebot_bridge') is None;"
        "assert wire.digest({'v':1}) == sys.argv[2];"
        "assert len(wire.ACCOUNT_ACTIONS)==4;print('standalone-wire:ok')"
    )
    result = subprocess.run([sys.executable, "-I", "-S", "-c", source, str(tmp_path),
                             wire.digest({"v": 1})], capture_output=True, text=True,
                            check=True, timeout=15, cwd=tmp_path)
    assert result.stdout.strip() == "standalone-wire:ok"


def rows(env):
    return [json.loads(line) for line in Path(
        env.core.settings.paid_model.journal_path).read_text("utf-8").splitlines()]


def sibling(env, *, state="SENDING", attempted=True):
    core = env.core
    row = core.store.one('SELECT * FROM outbox WHERE message_id=%s', (env.message_id,))
    message_id, token = uid("msg"), uid("lease")
    row.update(message_id=message_id, lease_token=token if attempted else None,
               delivery_key="synthetic-sibling:" + message_id, state=state,
               attempt_count=int(attempted), provider_message_id=None, native_ack_json=None)
    check = {"window_sha256": env.plan.sha256, "message_id": message_id,
             "event_id": env.proof["event_id"], "lease_token": token,
             env.proof["payload_kind"]: env.proof["payload_sha256"]}
    with core.store.transaction():
        core.store.insert("outbox", **row)
        if attempted:
            authority = reconciliation._authority(core.store, env.message_id)
            put(core.store, "private_life_delivery:" + message_id, {
                **authority, "message_id": message_id, "delivery_key": row["delivery_key"],
            })
            cumulative.admit_operation(core.store, core.settings, core.clock(), "send",
                message_id, wire.digest(check), logical=env.proof["event_id"])
    result = SimpleNamespace(**{**vars(env), "message_id": message_id, "token": token})
    result.proof = signed(env, message_id=message_id, fence=2,
        lease_sha256=wire.digest(token), send_check_sha256=wire.digest(check),
        item_sha256=wire.digest([message_id, token]),
        original_result_sha256=wire.digest({"lease_token": token, "result": "DELIVERY_UNCERTAIN"}))
    return result


@pytest.mark.parametrize("outcome", ["SENT", "CANCELLED"])
@pytest.mark.parametrize("expired", [False, True])
def test_native_resolution_preserves_all_reservations_money_and_stop(
    core, tmp_path, outcome, expired,
):
    env = native(core, tmp_path, uncertain=not expired, expired=expired)
    before = deepcopy(view(core))
    usage = core.store.rows("SELECT * FROM model_usage")
    admissions = core.store.rows("SELECT * FROM model_admissions")
    original = core.store.one('SELECT * FROM outbox WHERE message_id=%s', (env.message_id,))
    prefix = rows(env)
    assert prepare(env)["state"] == "READY"
    assert reconciliation.readiness(core.store, core.settings, env.message_id)["prepared"]
    result = decide(env, outcome)
    receipt = wire.validate_receipt(result["receipt"])
    after_state = view(core)
    assert receipt["state"] == "OWNER_RESOLVED" and receipt["outcome"] == outcome
    assert receipt["prior_quota_state"] == ("RESERVED" if expired else "DELIVERY_UNCERTAIN")
    assert receipt["quiescence"] == env.proof
    assert receipt["input_closure"]["terminal"] is True
    assert receipt["delivery"]["attempt_count"] == 1
    assert rows(env)[:-1] == prefix and rows(env)[-1]["decision_source"] == "owner_manual"
    assert len(after_state["operations"]) == len(before["operations"]) == 2
    for name, liability in before["operations"].items():
        assert {k: v for k, v in after_state["operations"][name].items() if k != "state"} == {
            k: v for k, v in liability.items() if k != "state"}
    assert after_state["models"] == before["models"]
    assert after_state["policies"] == before["policies"]
    assert after_state["paused"] and after_state["reason"] == before["reason"]
    assert core.store.rows("SELECT * FROM model_usage") == usage
    assert core.store.rows("SELECT * FROM model_admissions") == admissions
    changed = core.store.one('SELECT * FROM outbox WHERE message_id=%s', (env.message_id,))
    assert changed["state"] == outcome and changed["lease_token"] is None
    for field in ("attempt_count", "native_ack_json", "provider_message_id", "created_at",
                  "lease_until", "content_sha256"):
        assert changed[field] == original[field]
    assert live_text.stopped(core.store, env.plan.sha256)
    assert decide(env, outcome) == result
    assert reconciliation.lookup(core.store, core.settings, env.message_id,
                                proof_sha256=wire.digest(env.proof)) == receipt
    assert len(rows(env)) == len(prefix) + 1
    assert core.model.calls == []


@pytest.mark.parametrize("field,value", [
    ("signature", "f" * 128), ("send_check_sha256", "f" * 64),
    ("lease_sha256", "f" * 64), ("payload_sha256", "f" * 64),
    ("content_sha256", "f" * 64), ("event_id", "evt_unrelated"),
    ("plan_sha256", "f" * 64), ("window_id", "win_" + "f" * 32),
    ("budget_journal_id", "budget_" + "f" * 32),
])
def test_changed_signed_or_unsigned_original_binding_is_rejected(core, tmp_path, field, value):
    env = native(core, tmp_path)
    proof = {**env.proof, field: value}
    if field != "signature":
        if field == "content_sha256":
            proof["payload_sha256"] = value
        if field == "payload_sha256":
            proof["content_sha256"] = value
        proof["signature"] = env.key.sign(wire.signing_payload(proof)).hex()
    original = rows(env)
    with pytest.raises(CoreError):
        reconciliation.prepare(core.store, core.settings, proof, core.clock())
    assert rows(env) == original
    assert core.store.one('SELECT state FROM outbox WHERE message_id=%s',
                          (env.message_id,))["state"] == "DELIVERY_UNCERTAIN"


def test_prepare_requires_real_core_stop_and_expired_sending_lease(core, tmp_path):
    env = native(core, tmp_path, uncertain=False, stopped=False)
    with pytest.raises(CoreError, match="STOP_REQUIRED"):
        prepare(env)
    live_text.stop_window(core.store, live_text.WindowReference(window_sha256=env.plan.sha256),
                          core.clock)
    with pytest.raises(CoreError, match="STOP_REQUIRED"):
        prepare(env)
    core.clock.advance(31)
    assert prepare(env)["state"] == "READY"


def test_no_prepared_proof_or_conflicting_owner_decision_can_resolve(core, tmp_path):
    env = native(core, tmp_path)
    with pytest.raises(CoreError, match="QUIESCENCE_REQUIRED"):
        decide(env)
    prepare(env)
    first = decide(env)
    for outcome, op in (("SENT", "owner-fixture-op"), ("CANCELLED", "different-op")):
        with pytest.raises(CoreError, match="CONFLICT"):
            decide(env, outcome, operation_id=op)
    assert reconciliation.lookup(core.store, core.settings, env.message_id,
        proof_sha256=wire.digest(env.proof)) == first["receipt"]


@pytest.mark.parametrize("action", ["qq.private.reply", "qq.private.proactive", "qq.private.poke"])
def test_erased_payload_uses_retained_authority_and_historical_public_key(core, tmp_path, action):
    env = native(core, tmp_path, action=action)
    with core.store.transaction():
        ErasureLedger(core.settings.erasure_ledger).expire_events(
            core.store, [env.proof["event_id"]], core.clock())
    core.settings.live_text_window = core.settings.live_text_window.model_copy(update={
        "capture_public_key": Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex(),
    })
    prepare(env)
    result = decide(env)
    assert result["receipt"]["delivery"]["content_sha256"]
    assert result["receipt"]["quiescence"] == env.proof
    assert "synthetic private reply" not in encode(result)
    assert core.store.one('SELECT content_json,content_sha256 FROM outbox WHERE message_id=%s',
                          (env.message_id,)) == {"content_json": "{}", "content_sha256": ""}


def test_unrelated_model_unknown_stays_unknown_and_blocks_renewal(core, tmp_path):
    env = native(core, tmp_path, model_unknown=True)
    original = deepcopy(view(core)["models"])
    prepare(env)
    decide(env)
    assert view(core)["models"] == original
    assert {value["state"] for value in original.values()} == {"UNKNOWN"}
    policy(core)
    with pytest.raises(CoreError):
        with core.store.transaction():
            cumulative.activate(core.store, core.settings, core.clock())


def test_legacy_erased_text_receipt_keeps_payload_hash_without_inventing_content_hash(
    core, tmp_path,
):
    env = native(core, tmp_path, action="qq.private.reply", legacy=True)
    with core.store.transaction():
        ErasureLedger(core.settings.erasure_ledger).expire_events(
            core.store, [env.proof["event_id"]], core.clock())
    prepare(env)
    result = decide(env)
    assert result["receipt"]["delivery"]["content_sha256"] is None
    assert result["receipt"]["quiescence"]["payload_sha256"] == env.proof["payload_sha256"]
    assert result["receipt"]["source_sha256"] == env.proof["send_check_sha256"]
    assert decide(env) == result


def test_late_provider_ack_cannot_replace_owner_resolution(core, tmp_path):
    env = native(core, tmp_path)
    prepare(env)
    first = decide(env)
    with pytest.raises(CoreError, match="STALE_DELIVERY_LEASE"):
        delivery_result(core.store, core.settings, env.message_id, DeliveryResult(
            lease_token=env.token, result="SENT", provider_message_id="late"), core.clock())
    with pytest.raises(CoreError):
        with core.store.transaction():
            cumulative.send_result(core.store, env.message_id, "SENT", core.clock())
    assert decide(env) == first


@pytest.mark.parametrize("stage", ["after_fsync", "projection"])
@pytest.mark.parametrize("flavor", ["private", "account", "voice"])
def test_fsynced_decision_recovers_exact_owner_receipt_after_database_failure(
    core, tmp_path, monkeypatch, stage, flavor,
):
    env = native(core, tmp_path, action="qq.profile.nickname" if flavor == "account"
                 else "qq.private.reply", voice=flavor == "voice")
    prepare(env)
    real_execute, real_fsync = core.store.execute, paid_budget.os.fsync
    failed = False

    def execute(sql, params=()):
        nonlocal failed
        if (stage == "projection" and not failed and sql.startswith("INSERT OR REPLACE")
            and params and str(params[0]).startswith(reconciliation.PROJECTION)):
            failed = True
            raise sqlite3.OperationalError("synthetic projection interruption")
        return real_execute(sql, params)

    def fsync(descriptor):
        nonlocal failed
        real_fsync(descriptor)
        if stage == "after_fsync" and not failed:
            failed = True
            raise OSError("synthetic death after durable append")

    monkeypatch.setattr(core.store, "execute", execute)
    monkeypatch.setattr(paid_budget.os, "fsync", fsync)
    with pytest.raises((CoreError, sqlite3.OperationalError)):
        decide(env)
    monkeypatch.setattr(core.store, "execute", real_execute)
    monkeypatch.setattr(paid_budget.os, "fsync", real_fsync)
    assert failed
    assert rows(env)[-1]["kind"] == reconciliation.KIND
    assert core.store.one('SELECT state FROM outbox WHERE message_id=%s',
                          (env.message_id,))["state"] == "DELIVERY_UNCERTAIN"
    with pytest.raises(CoreError):
        paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True)
    before = rows(env)
    result = decide(env)
    assert result["receipt"]["record_sha256"] == before[-1]["sha256"]
    assert rows(env) == before
    assert len(core.store.rows(
        "SELECT * FROM audit_log WHERE action='delivery.owner_reconciled'")) == 1


@pytest.mark.parametrize("flavor", ["private", "account", "voice"])
def test_new_database_cannot_accept_old_journal_prefix(core, tmp_path, flavor):
    env = native(core, tmp_path, action="qq.profile.nickname" if flavor == "account"
                 else "qq.private.reply", voice=flavor == "voice")
    prepare(env)
    journal = Path(core.settings.paid_model.journal_path)
    old = journal.read_bytes()
    decide(env)
    journal.write_bytes(old)
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        decide(env)


def test_expired_reserved_send_without_prior_pause_is_closed_but_remains_paused(core, tmp_path):
    env = native(core, tmp_path, uncertain=False, expired=True, stopped=False)
    with core.store.transaction():
        # Valid historical STOP prefix before the separately recorded quota pause.
        paid_budget.journal(core.store, core.settings.paid_model, {
            "kind": "window_stop", "window_id": env.proof["window_id"],
            "plan_sha256": env.plan.sha256,
        }, allow_settlement=True)
        history = reconciliation._meta(core.store, live_text.HISTORY + env.plan.sha256)
        put(core.store, live_text.HISTORY + env.plan.sha256, {**history, "state": "STOPPED"})
        core.store.insert("runtime_meta", key=live_text.STOP + env.plan.sha256, value="OWNER_STOP")
    assert not view(core)["paused"]
    prepare(env)
    decide(env)
    assert view(core)["paused"]
    assert view(core)["reason"] == "OWNER_DELIVERY_RECONCILIATION"
    operation = view(core)["operations"]["send:" + wire.digest(env.message_id)]
    assert operation["state"] == "OWNER_CLOSED"
    with pytest.raises(CoreError, match="CUMULATIVE_PAUSED"):
        cumulative.check(core.store, core.settings, core.clock())


def test_fresh_approved_renewal_preserves_counts_and_does_not_open_old_window(core, tmp_path):
    env = native(core, tmp_path)
    prepare(env)
    decide(env)
    before = deepcopy(view(core))
    with pytest.raises(CoreError, match="CUMULATIVE_PAUSED"):
        cumulative.check(core.store, core.settings, core.clock())
    policy(core, approval_sha256="8" * 64)
    with core.store.transaction():
        cumulative.activate(core.store, core.settings, core.clock())
    current = view(core)
    assert current["operations"] == before["operations"]
    assert current["models"] == before["models"]
    assert len(current["policies"]) == len(before["policies"]) + 1
    assert live_text.stopped(core.store, env.plan.sha256)
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        paid_budget.journal(core.store, core.settings.paid_model)
    assert reconciliation.lookup(core.store, core.settings, env.message_id,
                                proof_sha256=wire.digest(env.proof)) is not None


def test_readiness_only_reads_and_supports_caller_transaction_and_clock(core, tmp_path):
    env = native(core, tmp_path, uncertain=False, expired=False)
    core.clock.advance(31)
    prepare(env)
    original_journal = rows(env)
    original_meta = core.store.rows("SELECT * FROM runtime_meta ORDER BY key")
    with core.store.transaction():
        assert reconciliation.readiness(core.store, core.settings, env.message_id,
                                        now=core.clock())["prepared"]
    assert rows(env) == original_journal
    assert core.store.rows("SELECT * FROM runtime_meta ORDER BY key") == original_meta
    assert not reconciliation.readiness(core.store, core.settings, env.message_id,
                                         now=env.proof["quiesced_at"])["prepared"]


def test_changed_historical_approval_cannot_replace_the_frozen_capture_key(core, tmp_path):
    env = native(core, tmp_path)
    history = reconciliation._meta(core.store, live_text.HISTORY + env.plan.sha256)
    other = Ed25519PrivateKey.generate()
    history["approval"]["capture_public_key"] = other.public_key().public_bytes_raw().hex()
    put(core.store, live_text.HISTORY + env.plan.sha256, history)
    proof = dict(env.proof)
    proof["signature"] = other.sign(wire.signing_payload(proof)).hex()
    with pytest.raises(CoreError, match="QUIESCENCE_INVALID"):
        reconciliation.prepare(core.store, core.settings, proof, core.clock())


def test_old_database_recovers_only_exact_original_owner_record(core, tmp_path):
    env = native(core, tmp_path)
    prepare(env)
    backup = tmp_path / "before-owner.db"
    destination = sqlite3.connect(backup)
    core.store.db.backup(destination)
    destination.close()
    expected = decide(env)
    restored = Store(str(backup))
    try:
        with pytest.raises(CoreError):
            reconciliation.lookup(restored, core.settings, env.message_id,
                proof_sha256=wire.digest(env.proof))
        with pytest.raises(CoreError):
            reconciliation.lookup(restored, core.settings, env.message_id,
                operation_id="different-op",
                operation_sha256=wire.digest([env.message_id, "CANCELLED"]),
                outcome="CANCELLED", repair=True)
        recovered = reconciliation.lookup(restored, core.settings, env.message_id,
            operation_id="owner-fixture-op",
            operation_sha256=wire.digest([env.message_id, "CANCELLED"]),
            outcome="CANCELLED", repair=True, now=core.clock())
        assert recovered == expected["receipt"]
        assert reconciliation.lookup(restored, core.settings, env.message_id,
            proof_sha256=wire.digest(env.proof)) == recovered
    finally:
        restored.db.close()


@pytest.mark.parametrize("outcome", ["SENT", "CANCELLED"])
def test_live_without_cumulative_still_requires_proof_and_original_budget_record(
    core, tmp_path, outcome,
):
    env = native(core, tmp_path, quota=False)
    with pytest.raises(CoreError, match="QUIESCENCE_REQUIRED"):
        decide(env, outcome)
    prepare(env)
    result = decide(env, outcome)
    assert result["receipt"]["account_ref"] is result["receipt"]["policy_sha256"] is None
    assert result["receipt"]["prior_liability_sha256"] is None
    assert not core.store.one('SELECT 1 FROM runtime_meta WHERE key=%s', (cumulative.PROJECTION,))
    assert decide(env, outcome) == result


def test_shared_protocol_is_standard_library_only_and_rejects_extra_private_fields():
    import ast

    source = Path(wire.__file__).read_text("utf-8")
    imports = {node.module.split(".")[0] for node in ast.walk(ast.parse(source))
               if isinstance(node, ast.ImportFrom)}
    imports |= {alias.name.split(".")[0] for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Import) for alias in node.names}
    assert imports <= {"hashlib", "json", "re", "datetime", "companion_delivery_protocol"}
    import companion_delivery_protocol.authority as authority_wire

    authority_tree = ast.parse(Path(authority_wire.__file__).read_text("utf-8"))
    authority_imports = {node.module.split(".")[0] for node in ast.walk(authority_tree)
                        if isinstance(node, ast.ImportFrom)}
    authority_imports |= {alias.name.split(".")[0] for node in ast.walk(authority_tree)
                          if isinstance(node, ast.Import) for alias in node.names}
    assert authority_imports <= {"re", "dataclasses", "datetime", "companion_delivery_protocol"}
    with pytest.raises(ValueError):
        wire.validate_quiescence({"private_body": "not a valid proof"})


@pytest.mark.parametrize("outcome", ["SENT", "CANCELLED"])
def test_last_unknown_delivery_closes_input_without_changing_previous_receipt(
    core, tmp_path, outcome,
):
    first = native(core, tmp_path, uncertain=False, stopped=False, action="qq.private.reply")
    second = sibling(first)
    for env in (first, second):
        delivery_result(core.store, core.settings, env.message_id, DeliveryResult(
            lease_token=env.token, result="DELIVERY_UNCERTAIN"), core.clock())
    live_text.stop_window(core.store, live_text.WindowReference(window_sha256=first.plan.sha256),
                          core.clock)
    prepare(first)
    before = decide(first, outcome, operation_id="first-owner-op")["receipt"]
    assert not before["input_closure"]["terminal"]
    assert before["input_closure"]["unsettled"]["deliveries"]
    original_prefix = rows(first)
    state_before = deepcopy(view(core))
    prepare(second)
    last = decide(second, outcome, operation_id="second-owner-op")["receipt"]
    assert last["input_closure"]["terminal"]
    by_id = {entry["message_id"]: entry for entry in last["input_closure"]["deliveries"]}
    assert by_id[first.message_id]["owner_resolution_sha256"] == before["record_sha256"]
    assert by_id[second.message_id]["owner_resolution_sha256"] is None
    assert all(entry["attempt_count"] == 1 for entry in by_id.values())
    assert reconciliation.lookup(core.store, core.settings, first.message_id,
        proof_sha256=wire.digest(first.proof)) == before
    assert rows(first)[:-1] == original_prefix
    broken = deepcopy(rows(first)[-1])
    broken.pop("sha256")
    next(entry for entry in broken["input_closure"]["deliveries"]
         if entry["message_id"] == first.message_id)["owner_resolution_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="preceding owner decision"):
        cumulative.fold(state_before, broken)


@pytest.mark.parametrize("blocker", ["jobs", "models", "reads", "deliveries"])
def test_input_closure_never_hides_unsettled_native_work(core, tmp_path, blocker):
    env = native(core, tmp_path, model_unknown=blocker == "models")
    if blocker == "jobs":
        core.store.execute("UPDATE jobs SET state='RUNNING' WHERE event_id=%s",
                           (env.proof["event_id"],))
    elif blocker == "reads":
        core.store.insert("content_items", item_id=uid("content"),
            owner_event_id=env.proof["event_id"], kind="link", slot=0, state="RUNNING",
            captured_privacy_epoch=0, deadline=after(core.clock(), 30),
            created_at=core.clock(), updated_at=core.clock())
    elif blocker == "deliveries":
        sibling(env, state="PENDING", attempted=False)
    prepare(env)
    result = decide(env)
    closure = result["receipt"]["input_closure"]
    assert closure["complete"] and not closure["terminal"]
    assert closure["unsettled"][blocker]
    assert view(core)["paused"] and core.model.calls == []


def test_overlarge_input_inventory_cannot_claim_terminal_or_drop_responsibility(core, tmp_path):
    env = native(core, tmp_path)
    for _ in range(wire.MAX_INPUT_DELIVERIES):
        sibling(env, state="CANCELLED", attempted=False)
    before = core.store.rows(
        "SELECT message_id,state,attempt_count FROM outbox ORDER BY message_id")
    prepare(env)
    result = decide(env)
    closure = result["receipt"]["input_closure"]
    assert not closure["complete"] and not closure["terminal"]
    assert len(closure["deliveries"]) == wire.MAX_INPUT_DELIVERIES
    assert env.message_id in {entry["message_id"] for entry in closure["deliveries"]}
    assert len(wire.canonical(result["receipt"])) <= wire.MAX_RECEIPT_BYTES < 65536
    after_rows = core.store.rows(
        "SELECT message_id,state,attempt_count FROM outbox ORDER BY message_id")
    assert len(before) == len(after_rows) == wire.MAX_INPUT_DELIVERIES + 1
    assert [r for r in after_rows if r["message_id"] != env.message_id] == [
        r for r in before if r["message_id"] != env.message_id]


def test_original_provider_ack_is_hashed_separately_from_owner_resolution(core, tmp_path):
    env = native(core, tmp_path, uncertain=False, stopped=False, action="qq.private.reply")
    acknowledged = sibling(env)
    delivery_result(core.store, core.settings, acknowledged.message_id, DeliveryResult(
        lease_token=acknowledged.token, result="SENT", provider_message_id="native-provider-123"),
        core.clock())
    delivery_result(core.store, core.settings, env.message_id, DeliveryResult(
        lease_token=env.token, result="DELIVERY_UNCERTAIN"), core.clock())
    live_text.stop_window(core.store, live_text.WindowReference(window_sha256=env.plan.sha256),
                          core.clock)
    prepare(env)
    result = decide(env, "SENT")
    entries = {row["message_id"]: row for row in result["receipt"]["input_closure"]["deliveries"]}
    assert result["receipt"]["input_closure"]["terminal"]
    assert entries[acknowledged.message_id]["provider_receipt_sha256"] == wire.digest({
        "provider_message_id": "native-provider-123", "native_ack": None,
    })
    assert entries[env.message_id]["provider_receipt_sha256"] == wire.digest({
        "provider_message_id": None, "native_ack": None,
    })


def test_terminal_input_recovery_does_not_close_an_old_database_with_active_work(core, tmp_path):
    env = native(core, tmp_path)
    prepare(env)
    backup = tmp_path / "old-active-input.db"
    destination = sqlite3.connect(backup)
    core.store.db.backup(destination)
    destination.close()
    expected = decide(env)
    restored = Store(str(backup))
    try:
        restored.execute("UPDATE jobs SET state='RUNNING' WHERE event_id=?",
                         (env.proof["event_id"],))
        with pytest.raises(CoreError, match="INPUT_CLOSURE_RECONCILIATION_REQUIRED"):
            reconciliation.lookup(restored, core.settings, env.message_id,
                operation_id="owner-fixture-op",
                operation_sha256=wire.digest([env.message_id, "CANCELLED"]),
                outcome="CANCELLED", repair=True, now=core.clock())
        assert restored.one("SELECT state FROM outbox WHERE message_id=?",
                             (env.message_id,))["state"] == "DELIVERY_UNCERTAIN"
        assert expected["receipt"]["input_closure"]["terminal"]
    finally:
        restored.db.close()


@pytest.mark.parametrize("change", ["token", "job", "message", "signature", "body", "missing"])
def test_retired_voice_proof_is_bound_to_original_signed_message(core, tmp_path, change):
    env = native(core, tmp_path, action="qq.private.reply", voice=True)
    with core.store.transaction():
        ErasureLedger(core.settings.erasure_ledger).expire_events(
            core.store, [env.proof["event_id"]], core.clock())
    if change == "token":
        env.proof = signed(env, voice_check=env.proof["voice_check"] |
                           {"retirement_token": "unrelated-random-" + "z" * 32})
    elif change == "job":
        env.proof = signed(env, voice_check=env.proof["voice_check"] |
                           {"job_id": "voice_" + "f" * 32})
    elif change == "message":
        env.proof = signed(env, message_id="msg_" + "f" * 32)
    elif change == "signature":
        env.proof["signature"] = "0" * 128
    elif change == "body":
        env.proof["voice_check"]["audio_sha256"] = "f" * 64
    else:
        env.proof.pop("voice_check")
        env.proof = signed(env)
    before = rows(env)
    with pytest.raises(CoreError):
        prepare(env)
    assert rows(env) == before
