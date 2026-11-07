"""B1b shared settings and original Core hook fences; local synthetic checks only."""

import asyncio
import json
from typing import ClassVar

import httpx
import pytest
from pydantic import ValidationError

from companion import meme_service, owner_authority, paid_budget, reply_revision
from companion.config import Settings
from companion.contracts import CoreError
from companion.meme_policy import TrustedSource
from companion.meme_retrieval import MemeRetrieval
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.store import encode
from tests.conftest import run
from tests.support.failure_notice import assert_failure_notice
from tests.support.paid_model import approved_live

RULE = {
    "repo": "synthetic/pack",
    "revision": "a" * 40,
    "license": "CC0",
    "license_path": "LICENSE",
    "license_sha256": "b" * 64,
    "asset_prefix": "memes",
    "usage_basis": "Synthetic public domain",
}


class LegacySettings(Settings):
    # Exactly the previous serialization fields; retain original field validators.
    meme_catalog_owner: ClassVar[str] = "bridge"
    meme_trusted_sources: ClassVar[tuple] = ()
    meme_cooldown_seconds: ClassVar[int] = 1800


def test_default_fields_preserve_previous_settings_paid_and_owner_hashes(core, tmp_path):
    approved_live(core, tmp_path)
    core.settings.expression_enabled = True
    settings = core.settings
    legacy = LegacySettings.model_validate(settings.model_dump())
    explicit = Settings.model_validate(
        {
            **settings.model_dump(),
            "meme_catalog_owner": "bridge",
            "meme_trusted_sources": [],
            "meme_cooldown_seconds": 1800,
        }
    )
    for mode in ("python", "json"):
        assert (
            settings.model_dump(mode=mode)
            == explicit.model_dump(mode=mode)
            == legacy.model_dump(mode=mode)
        )
    assert settings.model_dump_json() == explicit.model_dump_json() == legacy.model_dump_json()
    assert paid_budget.snapshot(legacy) == paid_budget.snapshot(settings)
    assert meme_service.policy_hash(legacy) == meme_service.policy_hash(settings)
    assert owner_authority.capability_hashes(legacy) == owner_authority.capability_hashes(settings)
    policy = OwnerGrantPolicy(
        owner_qq_id=settings.owner_qq_id,
        purposes=["chat", "meme"],
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(legacy)),
        capability_policies={"meme": meme_service.policy_hash(legacy)},
        capture={
            "public_key": "e" * 64,
            "identity_sha256": paid_budget.sha([settings.owner_qq_id, settings.bot_qq_id]),
            "protocol_epoch": "synthetic-settings",
        },
    )
    assert encode(policy.model_dump(mode="json")) == encode(
        policy.model_copy(
            update={
                "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(settings)),
                "capability_policies": {"meme": meme_service.policy_hash(settings)},
            }
        ).model_dump(mode="json")
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"meme_catalog_owner": "core"},
        {"meme_cooldown_seconds": 0},
        {"meme_trusted_sources": [RULE]},
        {"meme_catalog_owner": "core", "meme_excluded_sha256": ["e" * 64]},
        {"meme_catalog_owner": "core", "meme_cooldown_seconds": 30, "meme_trusted_sources": [RULE]},
    ],
)
def test_nondefaults_are_serialized_and_bound_to_original_meme_policy(core, changes):
    core.settings.expression_enabled = True
    original = core.settings
    candidate = Settings.model_validate({**original.model_dump(), **changes})
    dumped = candidate.model_dump(mode="json")
    for key, value in changes.items():
        assert dumped[key] == value
    if candidate.meme_catalog_owner == "bridge":
        assert "meme_catalog_owner" not in dumped
    assert meme_service.policy_hash(candidate) != meme_service.policy_hash(original)
    assert (
        owner_authority.capability_hashes(candidate)["meme"]
        != owner_authority.capability_hashes(original)["meme"]
    )
    assert Settings.model_validate(candidate.model_dump()).model_dump() == candidate.model_dump()


@pytest.mark.parametrize("value", [True, 1.5, "1800", -1, 86401])
def test_cooldown_is_strict_and_bounded(core, value):
    with pytest.raises(ValidationError):
        Settings.model_validate({**core.settings.model_dump(), "meme_cooldown_seconds": value})


@pytest.mark.parametrize(
    "changes",
    [
        {"meme_catalog_owner": "core"},
        {"meme_cooldown_seconds": 1},
        {"meme_trusted_sources": [TrustedSource(**RULE)]},
        {"meme_catalog_owner": "core", "meme_excluded_sha256": ["e" * 64]},
    ],
)
def test_old_native_owner_grant_does_not_authorize_changed_catalog_policy(
    core,
    tmp_path,
    monkeypatch,
    changes,
):
    from tests.meme_authority_fixture import environment

    async def scenario():
        async with environment(core, tmp_path, monkeypatch):
            owner_authority.current(core.store, core.settings, core.clock, purpose="meme")
            for key, value in changes.items():
                setattr(core.settings, key, value)
            with pytest.raises(CoreError, match="OWNER_AUTHORITY_CAPABILITY_CHANGED"):
                owner_authority.current(core.store, core.settings, core.clock, purpose="meme")
            assert not core.store.rows("SELECT * FROM model_usage")

    run(scenario())


def test_exact_source_rules_validate_at_configuration_boundary(core):
    data = core.settings.model_dump()
    value = Settings.model_validate({**data, "meme_trusted_sources": [RULE] * 32})
    assert len(value.meme_trusted_sources) == 32
    assert all(isinstance(rule, TrustedSource) for rule in value.meme_trusted_sources)
    for rules in (
        [RULE] * 33,
        [{**RULE, "revision": "main"}],
        [{**RULE, "asset_prefix": "../memes"}],
        [{**RULE, "unexpected": True}],
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({**data, "meme_trusted_sources": rules})
    with pytest.raises(ValidationError):
        Settings.model_validate({**data, "meme_catalog_owner": "automatic"})


def load_environment(core, tmp_path, monkeypatch):
    path = tmp_path / "settings.toml"
    path.write_text(
        'owner_qq_id="10001"\nbot_qq_id="10002"\n'
        'bridge_token="synthetic-config-token-12345678"\n'
        'meme_catalog_owner="bridge"\nmeme_cooldown_seconds=1800\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("COMPANION_CONFIG", str(path))
    monkeypatch.setenv("COMPANION_DATABASE", core.settings.database)
    monkeypatch.setenv("COMPANION_ERASURE_LEDGER", core.settings.erasure_ledger)
    monkeypatch.setenv("COMPANION_MEME_CATALOG_OWNER", "core")
    monkeypatch.setenv("COMPANION_MEME_TRUSTED_SOURCES", json.dumps([RULE]))
    monkeypatch.setenv("COMPANION_MEME_COOLDOWN_SECONDS", "0")


def test_load_parses_json_sources_and_strict_json_integer_environment(core, tmp_path, monkeypatch):
    load_environment(core, tmp_path, monkeypatch)
    settings = Settings.load()
    assert settings.meme_catalog_owner == "core" and settings.meme_cooldown_seconds == 0
    assert settings.meme_trusted_sources == [TrustedSource(**RULE)]
    assert settings.model_dump(mode="json")["meme_trusted_sources"] == [RULE]


@pytest.mark.parametrize(
    "field,value",
    [
        ("TRUSTED_SOURCES", "not-json"),
        ("TRUSTED_SOURCES", "{}"),
        ("COOLDOWN_SECONDS", '"1800"'),
        ("COOLDOWN_SECONDS", "true"),
        ("COOLDOWN_SECONDS", "1.5"),
        ("COOLDOWN_SECONDS", "86401"),
    ],
)
def test_invalid_environment_is_not_silently_coerced(core, tmp_path, monkeypatch, field, value):
    load_environment(core, tmp_path, monkeypatch)
    monkeypatch.setenv("COMPANION_MEME_" + field, value)
    with pytest.raises(ValidationError):
        Settings.load()


def queued_native_chat(core, tmp_path):
    from tests.support.owner_runtime import chat

    _, job = chat(core, tmp_path)
    core.store.execute(
        "UPDATE jobs SET state='QUEUED',lease_token=NULL WHERE job_id=%s", (job["job_id"],)
    )
    return job


@pytest.mark.parametrize("saved", [False, True])
def test_prepare_failure_fences_fresh_and_persisted_native_chat(core, tmp_path, monkeypatch, saved):
    from pydantic_ai import Conversation

    from companion.agent_messages import from_wire_messages
    from companion.agent_persistence import ConversationStore
    from tests.support.owner_runtime import chat

    _, job = chat(core, tmp_path)
    if saved:
        persistence = ConversationStore(core, job, [])
        persistence._save(
            Conversation(
                conversation_id=job["job_id"],
                messages=from_wire_messages([{"role": "assistant", "content": "saved answer"}]),
            ),
            "READY",
        )
    core.store.execute(
        "UPDATE jobs SET state='QUEUED',lease_token=NULL WHERE job_id=%s", (job["job_id"],)
    )
    called = []

    async def fail(job):
        called.append(job["job_id"])
        raise CoreError("MEME_ASSET_SOURCE_CHANGED", 403)

    monkeypatch.setattr(core.memes, "prepare_offer", fail)
    assert run(core.run_once("prepare-hook-check", job_id=job["job_id"]))
    assert called == [job["job_id"]]
    row = core.store.one('SELECT state,last_error_code FROM jobs WHERE job_id=%s', (job["job_id"],))
    assert row == {"state": "FAILED", "last_error_code": "MEME_ASSET_SOURCE_CHANGED"}
    assert not core.store.rows("SELECT * FROM model_usage")
    assert_failure_notice(core)
    if saved:
        assert persistence.row()["phase"] == "READY"


def test_offer_revocation_after_http_hook_fences_final_provider_io(core, tmp_path, monkeypatch):
    job = queued_native_chat(core, tmp_path)
    invalid, providers = False, []

    def check(job):
        if invalid:
            raise CoreError("MEME_ASSET_SOURCE_CHANGED", 403)

    async def revoke(request):
        nonlocal invalid
        invalid = True

    monkeypatch.setattr(core.memes, "check_offer", check)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: providers.append(request) or httpx.Response(200, json={})
            ),
            event_hooks={"request": [revoke]},
        ) as client:
            core.model = ModelClient(core.settings, client)
            assert await core.run_once("offer-revocation-hook", job_id=job["job_id"])

    asyncio.run(scenario())
    assert not providers
    assert_failure_notice(core)
    assert core.store.one('SELECT last_error_code FROM jobs WHERE job_id=%s', (job["job_id"],)) == {
        "last_error_code": "MEME_ASSET_SOURCE_CHANGED"
    }
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
        "state": "SETTLED",
        "billed_micro_usd": 0,
    }


@pytest.mark.parametrize("command", ["/发送状态", "/暂停主动分享", "/暂停写日记"])
def test_classified_controls_keep_original_reply_without_offer_preparation(
    core, tmp_path, monkeypatch, command
):
    from tests.meme_authority_fixture import admit, environment

    core.settings.meme_catalog_owner = "core"

    def forbidden(*args, **kwargs):
        pytest.fail("a server-classified control entered meme retrieval")

    monkeypatch.setattr(MemeRetrieval, "prepare", forbidden)
    monkeypatch.setattr(MemeRetrieval, "check", forbidden)

    async def scenario():
        async with environment(core, tmp_path, monkeypatch) as (client, config, grant, now):
            _, response = await admit(client, config, grant, now, text=command)
            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]
            job = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (job_id,))
            assert reply_revision.control_job(core.store, job)
            assert await core.run_once("control-hook-check", job_id=job_id)
            assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (job_id,)) == {
                "state": "SUCCEEDED"
            }
            assert (
                json.loads(core.store.one("SELECT body_json FROM events")["body_json"])["text"]
                == command
            )
            assert not core.store.rows("SELECT * FROM model_usage") and not core.model.calls
            assert len(core.store.rows("SELECT * FROM outbox")) == 1
            assert (
                core.store.one(
                    'SELECT 1 FROM runtime_meta WHERE key=%s', ("meme_offer:" + job["event_id"],)
                )
                is None
            )

    run(scenario())
