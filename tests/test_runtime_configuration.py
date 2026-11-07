"""Runtime versions remain task-local across saves, leases and process restarts."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from companion import operator_config, owner_authority, paid_budget, runtime_config
from companion.config import Settings
from companion.contracts import CoreError
from companion.core import Core
from companion.model import ModelResponse
from companion.owner_authority_protocol import AuthorityStop
from companion.store import Store
from tests.conftest import ingest
from tests.support.owner_console import PASSWORD, client, login  # noqa: F401
from tests.support.owner_input import captured
from tests.support.runtime_configuration import hot as hot
from tests.support.runtime_configuration import save, task


def labels(messages):
    return any("当前时间参考" in str(item["content"]) for item in messages)


def test_default_is_enabled_but_the_switch_is_deployment_custody():
    settings = Settings(owner_qq_id="10001", bot_qq_id="10002", bridge_token="t" * 40)
    assert settings.operator_hot_reload
    assert "operator_hot_reload" in operator_config.EXCLUDED


def test_running_request_and_next_task_use_distinct_fixed_versions(hot):
    calls = []

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Model:
            async def complete(self, messages, scenario):
                calls.append(messages)
                entered.set()
                await release.wait()
                return ModelResponse("synthetic configuration reply", 20, 2)

        hot.model = Model()
        first = ingest(hot)
        running = asyncio.create_task(hot.run_once())
        await asyncio.wait_for(entered.wait(), 10)
        try:
            saved = save(hot, chat_time_labels=True)
            assert saved["version"] == 1 and saved["pending_reload"]
            assert runtime_config.boundary(hot.store, hot.settings, hot.clock).version == 1
            assert not labels(calls[0])
            old = runtime_config.check(hot.store, hot.settings, "job:" + first["job_id"])
            assert old.version == 0 and not old.chat_time_labels
            with pytest.raises(ValidationError):
                old.chat_time_labels = True
        finally:
            release.set()
            await running
        second = ingest(hot, number=2)
        assert await hot.run_once()
        assert labels(calls[1])
        new = runtime_config.check(hot.store, hot.settings, "job:" + second["job_id"])
        assert new.version == 1
        for row in hot.store.rows("SELECT request_id,job_id FROM model_usage"):
            assert runtime_config.check(hot.store, hot.settings, "model:" + row["request_id"]) == (
                runtime_config.check(hot.store, hot.settings, "job:" + row["job_id"]))
        assert all(row["state"] == "SUCCEEDED" for row in hot.store.rows("SELECT state FROM jobs"))
        assert not hot.settings.chat_time_labels  # Services retain the original settings object.

    asyncio.run(scenario())


def test_mood_provenance_remains_live_across_configuration_switch(hot):
    from tests.support.mood import erase, feedback

    source = feedback(hot)
    job = task(hot, number=10)
    proof = []
    assert not labels(hot.context(job, snapshots=proof))
    assert {item["kind"] for item in proof} >= {"runtime_configuration", "mood"}
    save(hot, chat_time_labels=True)
    assert runtime_config.boundary(hot.store, hot.settings, hot.clock).version == 1
    hot._check_context_snapshots(job, proof)
    assert not labels(hot.context(job))
    erase(hot, [source])
    with pytest.raises(CoreError, match="REPLY_SOURCE_RECALLED"):
        hot._check_context_snapshots(job, proof)
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        hot.mood.revalidate(next(item["proof"] for item in proof if item["kind"] == "mood"))
    assert runtime_config.check(hot.store, hot.settings, "job:" + job["job_id"]).version == 0


def test_retry_after_restart_retains_original_configuration(hot):
    job = task(hot)
    save(hot, chat_time_labels=True)
    hot.store.execute("UPDATE jobs SET state='RETRY_WAIT',lease_token=NULL WHERE job_id=%s",
                      (job["job_id"],))
    reloaded = operator_config.load(hot.settings)
    assert reloaded._operator_config_version == 1 and reloaded.chat_time_labels
    assert operator_config.load(reloaded)._operator_config_base == reloaded._operator_config_base
    with Core(reloaded, hot.model, hot.clock) as restarted:
        resumed = restarted.claim_job("restarted-worker", job_id=job["job_id"])
        assert resumed is not None
        proof = []
        assert not labels(restarted.context(resumed, snapshots=proof))
        assert proof[0]["kind"] == "runtime_configuration"
        restarted._check_context_snapshots(resumed, proof)
        proof[0]["snapshot_id"] = "f" * 64
        with pytest.raises(CoreError, match="CONFIGURATION_CONTEXT_CHANGED"):
            restarted._check_context_snapshots(resumed, proof)


def test_workers_switch_independently_and_idle_observation_does_not_write(hot):
    runtime_config.boundary(hot.store, hot.settings, hot.clock)
    save(hot, chat_time_labels=True)

    # Store is deliberately owned/closed by each independent connection.
    def run(kind):
        store = Store(hot.settings.database)
        try:
            return runtime_config.boundary(store, hot.settings, hot.clock, worker=kind)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshots = list(pool.map(run, ["life-worker", "content-worker"]))
    assert snapshots[0] == snapshots[1] and snapshots[0].version == 1
    view = operator_config.view(hot.store, hot.settings)
    assert view["active_version"] == 0 and view["pending_reload"]
    assert {w["worker"]: w["active_version"] for w in view["workers"]} == {
        "worker": 0, "life-worker": 1, "content-worker": 1}
    runtime_config.boundary(hot.store, hot.settings, hot.clock)
    changes = hot.store.db.total_changes
    hot.clock.advance(60)
    runtime_config.boundary(hot.store, hot.settings, hot.clock)
    assert hot.store.db.total_changes == changes
    assert not operator_config.view(hot.store, hot.settings)["pending_reload"]


@pytest.mark.parametrize("changes", [
    {"max_output_tokens": 1024}, {"chat_thinking": "low"},
    {"memory_extract_max_output_tokens": 3072},
    {"segmented_reply": {"mode": "semantic"}}, {"model_name": "other-model"},
    {"monthly_limit_micro_usd": 123456}, {"expression_selection_probability": 0.7},
    {"quote_policy": "often"}, {"timeout_seconds": 60},
    {"mood_enabled": True}, {"input_debounce": {"enabled": True, "quiet_seconds": 3}},
    {"turn_taking": {"silence_enabled": True, "follow_up_daily_checks": 4}},
])
def test_mixed_version_never_partially_applies(hot, changes):
    baseline = runtime_config.boundary(hot.store, hot.settings, hot.clock)
    view = save(hot, chat_time_labels=True, **changes)
    assert view["pending_restart"]
    assert runtime_config.boundary(hot.store, hot.settings, hot.clock) == baseline
    assert not runtime_config.check(hot.store, hot.settings,
        "job:" + task(hot)["job_id"]).chat_time_labels


def test_corruption_keeps_last_good_version_and_can_restore(hot):
    save(hot, chat_time_labels=True)
    good = runtime_config.boundary(hot.store, hot.settings, hot.clock)
    save(hot, chat_time_labels=False)
    hot.store.execute("UPDATE operator_configuration SET encrypted_overrides='damaged' "
                      "WHERE version=2")
    assert runtime_config.boundary(hot.store, hot.settings, hot.clock) == good
    assert operator_config.view(hot.store, hot.settings)["error"] == (
        "OPERATOR_CONFIGURATION_UNREADABLE")
    recovered = operator_config.load(hot.settings)
    assert recovered._operator_config_version == 1 and recovered.chat_time_labels
    body = operator_config.ConfigurationChange(expected_version=2, restore_version=1)
    restored = operator_config.update(hot.store, hot.settings, body, "restore-good", hot.clock())
    assert restored["version"] == 3
    assert runtime_config.boundary(hot.store, hot.settings, hot.clock).version == 3
    assert operator_config.update(hot.store, hot.settings, body, "restore-good", hot.clock()) == (
        operator_config.view(hot.store, hot.settings))


def test_invalid_and_stale_saves_do_not_change_runtime_or_history(hot):
    baseline = runtime_config.boundary(hot.store, hot.settings, hot.clock)
    with pytest.raises(CoreError, match="INVALID_OPERATOR_CONFIGURATION"):
        save(hot, max_output_tokens=1000000, model_key="never-expose-this-key")
    assert operator_config.latest(hot.store) is None
    save(hot, chat_time_labels=True)
    with pytest.raises(CoreError, match="CONFIGURATION_VERSION_CHANGED"):
        operator_config.update(hot.store, hot.settings,
            operator_config.ConfigurationChange(expected_version=0,
                                                changes={"chat_time_labels": False}),
            "conflicting-save", hot.clock())
    assert runtime_config.get(hot.store, hot.settings, "job:missing") is None
    assert baseline.version == 0


def test_conflicting_version_payload_cannot_replace_an_effective_snapshot(hot):
    save(hot, chat_time_labels=True)
    good = runtime_config.boundary(hot.store, hot.settings, hot.clock)
    encrypted = operator_config._cipher(hot.settings).encrypt(
        b'{"chat_time_labels":false}').decode()
    hot.store.execute('UPDATE operator_configuration SET encrypted_overrides=%s', (encrypted,))
    assert runtime_config.boundary(hot.store, hot.settings, hot.clock) == good
    assert operator_config.view(hot.store, hot.settings)["error"] == (
        "CONFIGURATION_VERSION_CONFLICT")


def test_restart_can_recover_only_a_verified_effective_snapshot(hot):
    runtime_config.boundary(hot.store, hot.settings, hot.clock)
    save(hot, chat_time_labels=True)
    hot.store.execute("UPDATE operator_configuration SET encrypted_overrides='damaged'")
    recovered = operator_config.load(hot.settings)
    assert recovered._operator_config_version == 0 and not recovered.chat_time_labels
    hot.settings.monthly_limit_micro_usd += 1
    with pytest.raises(CoreError, match="OPERATOR_CONFIGURATION_UNREADABLE"):
        operator_config.load(hot.settings)


def test_switch_off_restores_startup_reads_and_retains_bound_tasks(hot):
    job = task(hot)
    save(hot, chat_time_labels=True)
    hot.settings.operator_hot_reload = False
    assert runtime_config.boundary(hot.store, hot.settings, hot.clock).version == 0
    assert not labels(hot.context(job))
    assert operator_config.view(hot.store, hot.settings)["pending_restart"]
    assert operator_config.load(hot.settings).chat_time_labels


def test_snapshot_tampering_and_cold_changes_are_fenced(hot):
    job = task(hot)
    hot.settings.monthly_limit_micro_usd += 1
    with pytest.raises(CoreError, match="CONFIGURATION_STARTUP_IDENTITY_CHANGED"):
        hot.context(job)
    hot.settings.monthly_limit_micro_usd -= 1
    hot.store.execute("UPDATE operator_runtime_snapshots SET chat_time_labels=1")
    with pytest.raises(CoreError, match="CONFIGURATION_SNAPSHOT_INVALID"):
        hot.context(job)


def test_pre_gallery_default_snapshot_survives_restart(hot, monkeypatch):
    original = operator_config._raw

    def before_gallery(settings, *, reveal=True):
        value = original(settings, reveal=reveal)
        if isinstance(settings, Settings):
            value.pop("meme_gallery_source", None)
            value.pop("meme_excluded_sha256", None)
        return value

    with monkeypatch.context() as old:
        old.setattr(operator_config, "_raw", before_gallery)
        job = task(hot)
        baseline = runtime_config.check(hot.store, hot.settings, "job:" + job["job_id"])
    assert runtime_config.check(hot.store, hot.settings, "job:" + job["job_id"]) == baseline
    hot.settings.meme_catalog_owner = "core"
    hot.settings.meme_excluded_sha256 = ["a" * 64]
    with pytest.raises(CoreError, match="CONFIGURATION_STARTUP_IDENTITY_CHANGED"):
        hot.context(job)


def test_owner_revocation_cannot_activate_a_new_runtime_version(hot, tmp_path):
    capture, _ = captured(hot, tmp_path)
    old_contract = paid_budget.snapshot(hot.settings)
    old_hashes = owner_authority.capability_hashes(hot.settings)
    old = runtime_config.boundary(hot.store, hot.settings, hot.clock)
    save(hot, chat_time_labels=True)
    candidate = operator_config.load(hot.settings)
    assert paid_budget.snapshot(candidate) == old_contract
    assert owner_authority.capability_hashes(candidate) == old_hashes
    owner_authority.stop(hot.store, hot.settings, hot.clock,
        AuthorityStop(operation_id="stop-config-reload", authority=capture.authority))
    assert runtime_config.boundary(hot.store, hot.settings, hot.clock) == old
    state = operator_config.view(hot.store, hot.settings)["workers"][0]
    assert state["reason"] == "OWNER_AUTHORITY_DISABLED"


def test_restart_does_not_hide_a_changed_paid_contract(hot, tmp_path):
    captured(hot, tmp_path)
    save(hot, monthly_limit_micro_usd=hot.settings.monthly_limit_micro_usd + 1)
    restarted = operator_config.load(hot.settings)
    view = operator_config.view(hot.store, restarted)
    assert not view["pending_restart"] and view["requires_reapproval"]
    assert view["approval_reason"] == "OWNER_AUTHORITY_PAID_POLICY_CHANGED"
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_PAID_POLICY_CHANGED"):
        owner_authority.current(hot.store, restarted, hot.clock)


def test_console_saved_active_and_history_selection_are_separate(hot, client):  # noqa: F811
    headers = login(client)
    body = {"expected_version": 0, "changes": {"chat_time_labels": True}}
    saved = client.put("/v1/console/configuration", json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["pending_reload"] and not saved.json()["pending_restart"]
    runtime_config.boundary(hot.store, hot.settings, hot.clock)
    view = client.get("/v1/console/configuration").json()
    assert view["active_version"] == 1 and view["pending_version"] is None
    selected = client.put("/v1/console/configuration", json={
        "expected_version": 1, "restore_version": 0,
    }, headers={**headers, "Idempotency-Key": "restore-startup-config"})
    assert selected.status_code == 200, selected.text
    assert selected.json()["version"] == 2 and selected.json()["active_version"] == 1
    assert not runtime_config.boundary(hot.store, hot.settings, hot.clock).chat_time_labels
