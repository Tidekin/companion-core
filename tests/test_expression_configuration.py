"""Expression switches and owner text share one recoverable native task identity."""

import asyncio
import hashlib
import hmac
import json

import pytest
from pydantic import ValidationError

from companion import budget, operator_config, runtime_config
from companion.contracts import CoreError
from companion.conversation_policy import PARTICIPATION_V1, PARTICIPATION_V2
from companion.core import Core
from companion.model import ModelResponse
from companion.store import encode
from tests.conftest import ingest
from tests.test_runtime_configuration import hot as hot
from tests.test_runtime_configuration import save, task

TEXT = "请接住一个具体细节，用自己的话回应；需要解决问题时认真帮助。"


def rendered(core, job, proof=None):
    return "\n".join(message["content"] for message in core.context(job, snapshots=proof))


def test_unreviewed_new_members_of_an_expression_group_remain_cold(hot):
    from companion.style_live_config import StyleLiveConfig

    class ExtendedStyle(StyleLiveConfig):
        daily_calls: int = 1

    before = hot.settings.model_copy(update={"style_live": ExtendedStyle()})
    after = before.model_copy(update={"style_live": ExtendedStyle(daily_calls=2)})
    assert runtime_config.classify(before, after)["restart_fields"] == ["style_live"]
    assert runtime_config._cold(before) != runtime_config._cold(after)


@pytest.mark.parametrize("mode", ["OFF", "SHADOW", "ENABLED"])
def test_text_replaces_only_enabled_chat_guidance_and_preserves_base_rules(hot, mode):
    from companion.core import PRINCIPLES

    save(hot, conversation_policy={"mode": mode, "strategy": "participation_v2"},
         conversation_guidance=TEXT)
    job = task(hot)
    proof = []
    messages = hot.context(job, snapshots=proof)
    text = json.dumps(messages, ensure_ascii=False)
    assert messages[0]["content"].startswith(PRINCIPLES)
    assert (TEXT in text) == (mode == "ENABLED")
    assert PARTICIPATION_V1 not in text and PARTICIPATION_V2 not in text
    hot._check_context_snapshots(job, proof)
    snapshot = runtime_config.get(hot.store, hot.settings, "job:" + job["job_id"])
    with pytest.raises(ValidationError):
        snapshot.expression.conversation_guidance = "changed"


def test_inflight_model_keeps_old_guidance_and_next_chat_uses_saved_text(hot):
    calls = []

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Model:
            async def complete(self, messages, scenario):
                calls.append(messages)
                if len(calls) == 1:
                    entered.set()
                    await release.wait()
                return ModelResponse("synthetic expression reply", 20, 2)

        hot.model = Model()
        first = ingest(hot)
        running = asyncio.create_task(hot.run_once())
        await asyncio.wait_for(entered.wait(), 10)
        try:
            save(hot, conversation_policy={"mode": "ENABLED"}, conversation_guidance=TEXT,
                 style_live={"enabled": True})
            assert runtime_config.boundary(hot.store, hot.settings, hot.clock).version == 1
            assert TEXT not in json.dumps(calls[0], ensure_ascii=False)
        finally:
            release.set()
            await running
        ingest(hot, number=2)
        assert await hot.run_once()
        assert TEXT in json.dumps(calls[1], ensure_ascii=False)
        assert all(row["state"] == "SUCCEEDED" for row in hot.store.rows("SELECT state FROM jobs"))
        assert runtime_config.get(hot.store, hot.settings, "job:" + first["job_id"]).version == 0
        assert hot.settings.conversation_guidance is None and not hot.settings.style_live.enabled

    asyncio.run(scenario())


def test_text_is_encrypted_restorable_and_blank_restores_selected_preset(hot):
    save(hot, conversation_policy={"mode": "ENABLED", "strategy": "participation_v1"},
         conversation_guidance=TEXT)
    first = runtime_config.boundary(hot.store, hot.settings, hot.clock)
    assert TEXT not in encode(hot.store.rows("SELECT * FROM operator_configuration"))
    assert TEXT not in encode(hot.store.rows("SELECT * FROM operator_runtime_snapshots"))
    save(hot, conversation_guidance=" \n ")
    blank = runtime_config.boundary(hot.store, hot.settings, hot.clock)
    assert blank.expression.conversation_guidance is None
    assert PARTICIPATION_V1 in rendered(hot, task(hot))
    operator_config.update(hot.store, hot.settings,
        operator_config.ConfigurationChange(expected_version=2, restore_version=1),
        "restore-expression", hot.clock())
    restored = runtime_config.boundary(hot.store, hot.settings, hot.clock)
    assert restored.version == 3 and restored.expression == first.expression


def test_invalid_text_and_corrupt_snapshot_cannot_partially_apply(hot):
    save(hot, conversation_policy={"mode": "ENABLED"}, conversation_guidance=TEXT)
    good = runtime_config.boundary(hot.store, hot.settings, hot.clock)
    with pytest.raises(CoreError, match="INVALID_OPERATOR_CONFIGURATION"):
        save(hot, conversation_guidance="x" * 2001, style_live={"enabled": True})
    assert runtime_config.boundary(hot.store, hot.settings, hot.clock) == good
    job = task(hot)
    key = runtime_config.identity(hot.settings, good)
    hot.store.execute("UPDATE operator_runtime_snapshots SET expression_json=? WHERE snapshot_id=?",
        (encode({"format_version": 2, "conversation_policy": {"mode": "OFF"},
                 "style_live_enabled": False, "encrypted_guidance": None}), key))
    with pytest.raises(CoreError, match="CONFIGURATION_SNAPSHOT_INVALID"):
        hot.context(job)


def test_expression_values_keep_contracts_and_still_obey_real_input_limit(hot, tmp_path):
    from companion import owner_authority, paid_budget
    from tests.owner_model_fixtures import authorized_job
    from tests.support.paid_model import approved_live

    approved_live(hot, tmp_path, max_input_tokens=1000)
    contract, capabilities = paid_budget.snapshot(hot.settings), (
        owner_authority.capability_hashes(hot.settings))
    save(hot, conversation_policy={"mode": "ENABLED"}, conversation_guidance="表" * 2000,
         style_live={"enabled": True})
    candidate = operator_config.load(hot.settings)
    assert paid_budget.snapshot(candidate) == contract
    assert owner_authority.capability_hashes(candidate) == capabilities
    job_id = authorized_job(hot)
    job = hot.store.one("SELECT j.*,e.body_json,e.conversation_id FROM jobs j "
                       "JOIN events e USING(event_id) WHERE job_id=?", (job_id,))
    messages = hot.context(job)
    assert "表" * 2000 in messages[0]["content"]
    with pytest.raises(CoreError, match="MODEL_INPUT_LIMIT"):
        budget.reserve(hot.store, hot.settings, job_id, "chat", messages, hot.clock())
    assert not hot.store.rows("SELECT * FROM model_usage")


def legacy_binding(core, job):
    # Independent reproduction of 0055's signed bytes, including its original cold fields.
    raw = operator_config._raw(core.settings)
    for field in ("video_models", "meme_gallery_source", "meme_excluded_sha256"):
        raw.pop(field)  # These fields were absent from the original signed settings.
    for field in ("chat_time_labels", "operator_hot_reload", "conversation_guidance"):
        raw.pop(field)
    base = (json.loads(core.settings._operator_config_base) if core.settings._operator_config_base
            else operator_config._raw(core.settings))
    raw["deployment_chat_time_labels"] = base["chat_time_labels"]

    def sign(value):
        return hmac.new(core.settings.control_token.get_secret_value().encode(),
            ("companion/runtime-config/v1:" + encode(value)).encode(), hashlib.sha256).hexdigest()

    original = {"version": core.settings._operator_config_version,
                "base_sha256": sign(raw), "chat_time_labels": core.settings.chat_time_labels}
    key = sign(original)
    core.store.insert("operator_runtime_snapshots", snapshot_id=key, version=original["version"],
        base_sha256=original["base_sha256"], chat_time_labels=int(original["chat_time_labels"]),
        created_at=core.clock())
    task_key = "job:" + job["job_id"]
    core.store.execute("UPDATE operator_task_configuration SET snapshot_id=?,binding_sha256=? "
        "WHERE task_key=?", (key, sign([task_key, key]), task_key))
    return key


@pytest.mark.parametrize("version", [0, 1])
def test_legacy_task_and_model_identity_survive_upgrade_save_and_restart(hot, version):
    if version:
        save(hot, conversation_policy={"mode": "ENABLED", "strategy": "participation_v1"})
    settings = operator_config.load(hot.settings)
    with Core(settings, hot.model, hot.clock) as old:
        job = task(old)
        key = legacy_binding(old, job)
        proof = []
        before = rendered(old, job, proof)
        request = budget.reserve(old.store, settings, job["job_id"], "chat",
                                 [{"role": "user", "content": "hello"}], old.clock())
        save(old, conversation_policy={"mode": "ENABLED", "strategy": "participation_v2"},
             conversation_guidance=TEXT, style_live={"enabled": True},
             turn_taking={"silence_enabled": True}, input_debounce={"quiet_seconds": 5})
        runtime_config.boundary(old.store, settings, old.clock)
        old.store.execute("UPDATE jobs SET state='RETRY_WAIT',lease_token=NULL WHERE job_id=?",
                          (job["job_id"],))
    reloaded = operator_config.load(hot.settings)
    with Core(reloaded, hot.model, hot.clock) as resumed:
        retry = resumed.claim_job("upgraded", job_id=job["job_id"])
        assert retry is not None
        assert rendered(resumed, retry) == before
        resumed._check_context_snapshots(retry, proof)
        runtime_config.check_request(resumed.store, reloaded, request)
        original = runtime_config.get(resumed.store, reloaded, "job:" + job["job_id"])
        assert original.format_version == 1 and runtime_config.identity(reloaded, original) == key
        assert not original.silence_enabled and original.debounce.quiet_seconds == 2
        reloaded.monthly_limit_micro_usd += 1
        with pytest.raises(CoreError, match="CONFIGURATION_STARTUP_IDENTITY_CHANGED"):
            resumed.context(retry)


def test_populated_snapshot_migration_preserves_legacy_evidence_and_fences_downgrade(hot, tmp_path):
    from contextlib import closing
    from types import SimpleNamespace

    from alembic import command
    from alembic.config import Config

    from companion.store import Store, migrate

    path = tmp_path / "legacy-snapshots.db"
    settings = hot.settings.model_copy(update={"database": str(path)})
    migrate(str(path), "0056_watch_ledger_goal_reads")
    with closing(Store(str(path))) as store:
        old = SimpleNamespace(store=store, settings=settings, clock=hot.clock)
        key = legacy_binding(old, {"job_id": "migration"})
        store.insert("operator_task_configuration", task_key="job:migration", snapshot_id=key,
            binding_sha256=runtime_config._digest(settings, ["job:migration", key]),
            created_at=hot.clock())
        before = store.rows("SELECT * FROM operator_task_configuration")
    migrate(str(path), "0057_expression_configuration")
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + path.resolve().as_posix())
    with closing(Store(str(path))) as store:
        legacy = runtime_config.get(store, settings, "job:migration")
        assert runtime_config.identity(settings, legacy) == key and legacy.format_version == 1
        assert store.rows("SELECT * FROM operator_task_configuration") == before
    command.downgrade(cfg, "0056_watch_ledger_goal_reads")
    migrate(str(path), "0057_expression_configuration")
    with closing(Store(str(path))) as store:
        runtime_config.boundary(store, settings, hot.clock)
    with pytest.raises(RuntimeError, match="CONFIGURATION_DOWNGRADE_REQUIRES_EXPLICIT_DATA_REVIEW"):
        command.downgrade(cfg, "0056_watch_ledger_goal_reads")
