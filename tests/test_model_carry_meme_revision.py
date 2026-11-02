"""An explicit existing meme revision retains original carry and other scopes."""

import asyncio
import json
import ssl
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from companion import meme_policy, meme_service, model_carry, owner_authority
from companion.config import ImageInputPolicy
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop, OwnerGrantPolicy
from companion_onebot_bridge.config import BridgeConfig
from tests import owner_model_fixtures
from tests.conftest import run
from tests.meme_authority_fixture import admit
from tests.model_carry_support import migrated
from tests.test_evaluation_process import process_config
from tests.test_meme_core_process import process
from tests.test_meme_pack_files import RULE
from tests.test_model_carry_lifecycle import current_unknown, stop_current
from tests.test_model_renewal import renewal
from tests.test_owner_authority import activate
from tests.test_voice_jobs import voice_config
from tests.test_voice_node_api import tls_core


def carried_meme(core, tmp_path, monkeypatch, *, catalog_owner="bridge"):
    core.settings.voice = voice_config()
    core.settings.expression_enabled = True
    core.settings.expression_selection_probability = 1.0
    core.settings.meme_discovery_enabled = True
    core.settings.meme_semantics_enabled = True
    core.settings.meme_catalog_owner = catalog_owner
    core.settings.meme_trusted_sources = [meme_policy.TrustedSource(**RULE)]

    def policy(**values):
        return OwnerGrantPolicy(**{**values,
            "purposes": ["chat", "expression", "meme", "voice"],
            "capability_policies": {"meme": meme_service.policy_hash(core.settings),
                                    "voice": core.settings.voice.authority_sha256()}})

    with monkeypatch.context() as patch:
        patch.setattr(owner_model_fixtures, "OwnerGrantPolicy", policy)
        return migrated(core, tmp_path, max_output_tokens=4096,
            meme_input=ImageInputPolicy(approval_sha256="e" * 64,
                estimated_tokens_per_image=100, max_bytes_per_image=4000000,
                max_pixels_per_image=16000000, max_images_per_request=5))


def revised(core, case):
    core.settings.meme_discovery_daily_download_bytes = 32 * 1024 * 1024
    core.settings.meme_discovery_daily_storage_bytes = 8 * 1024 * 1024
    return case.policy.model_copy(update={"authorization_sha256": "a" * 64,
        "capability_policies": {**case.policy.capability_policies,
            "meme": meme_service.policy_hash(core.settings)}})


@pytest.mark.parametrize("catalog_owner", ["bridge", "core"])
def test_meme_budget_revision_and_renewal_keep_original_liability(
    core, tmp_path, monkeypatch, catalog_owner,
):
    case = carried_meme(core, tmp_path, monkeypatch, catalog_owner=catalog_owner)
    original = core.store.rows("SELECT * FROM model_usage")
    stop_current(core, case)
    policy = revised(core, case)
    active, _ = activate(core, policy, revision=4, operation="approve-exact-meme-revision")
    assert policy.capability_policies["voice"] == case.policy.capability_policies["voice"]
    assert owner_authority.current(core.store, core.settings, core.clock,
        purpose="meme").authority.as_dict() == active["authority"]
    assert core.store.rows("SELECT * FROM model_usage") == original
    case.policy, case.successor = policy, active
    request, renewed, witness = renewal(core, case)
    receipt = model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    assert receipt["retained_micro_usd"] == original[0]["billed_micro_usd"]
    activate(core, renewed, revision=6, operation="renew-after-exact-meme-revision")
    assert core.store.rows("SELECT * FROM model_usage") == original
    assert not core.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("change", ["authorization", "wrong_meme", "disabled", "purposes",
    "owner", "capture", "retention", "carry", "paid_contract", "paid_config", "voice",
    "expiry", "reply_limit"])
def test_meme_revision_cannot_rebind_or_expand_other_scope(core, tmp_path, monkeypatch, change):
    case = carried_meme(core, tmp_path, monkeypatch)
    stop_current(core, case)
    policy = revised(core, case)
    changes = {
        "authorization": {"authorization_sha256": case.policy.authorization_sha256},
        "wrong_meme": {"capability_policies": {**policy.capability_policies, "meme": "0" * 64}},
        "purposes": {"purposes": [*policy.purposes, "evaluation"]},
        "owner": {"owner_qq_id": "99999"},
        "capture": {"capture": policy.capture.model_copy(update={"protocol_epoch": "other-epoch"})},
        "retention": {"source_retention_days": 30},
        "carry": {"unknown_carry_sha256": "b" * 64},
        "paid_contract": {"paid_contract_sha256": "b" * 64},
        "expiry": {"expires_at": datetime(2099, 1, 1, tzinfo=UTC)},
        "reply_limit": {"max_reply_chars": 123},
    }
    policy = policy.model_copy(update=changes.get(change, {}))
    if change == "disabled":
        core.settings.expression_enabled = False
        core.settings.meme_discovery_enabled = False
        core.settings.meme_semantics_enabled = False
    if change == "paid_config":
        core.settings.paid_model = core.settings.paid_model.model_copy(update={"window_calls": 999})
    if change == "voice":
        core.settings.voice = core.settings.voice.model_copy(update={
            "reply": core.settings.voice.reply.model_copy(update={"max_chars": 199})})
        policy = policy.model_copy(update={"capability_policies": {
            **policy.capability_policies, "voice": core.settings.voice.authority_sha256()}})
    journal = Path(core.settings.paid_model.journal_path)
    before, finance = journal.read_bytes(), core.store.rows("SELECT * FROM model_usage")
    with pytest.raises(CoreError):
        activate(core, policy, revision=4, operation="reject-meme-scope-drift")
    assert journal.read_bytes() == before
    assert core.store.rows("SELECT * FROM model_usage") == finance
    assert owner_authority._latest(core.store)["state"] == "STOPPED"


def test_current_unknown_still_requires_original_same_policy_handoff(core, tmp_path, monkeypatch):
    case = carried_meme(core, tmp_path, monkeypatch)
    request_id = current_unknown(core)
    stop_current(core, case)
    original = core.store.rows("SELECT * FROM model_usage")
    old_limits = (core.settings.meme_discovery_daily_download_bytes,
                  core.settings.meme_discovery_daily_storage_bytes)
    policy = revised(core, case)
    with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_NOT_QUIESCENT"):
        activate(core, policy, revision=4, operation="reject-meme-current-unknown")
    (core.settings.meme_discovery_daily_download_bytes,
     core.settings.meme_discovery_daily_storage_bytes) = old_limits
    same, _ = activate(core, case.policy, revision=4, operation="meme-original-unknown-handoff")
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="meme-after-original-handoff-stop", authority=same["authority"]))
    revised(core, case)
    active, _ = activate(core, policy, revision=6, operation="meme-after-original-handoff")
    assert active["authority"]["revision"] == 7
    assert core.store.rows("SELECT * FROM model_usage") == original
    assert core.store.one("SELECT state FROM model_usage WHERE request_id=?",
                          (request_id,))["state"] == "UNKNOWN"


def test_http_meme_revision_admits_fresh_source_to_independent_worker(core, tmp_path, monkeypatch):
    case = carried_meme(core, tmp_path, monkeypatch, catalog_owner="core")
    stop_current(core, case)
    policy = revised(core, case)
    core.settings.control_token = SecretStr("synthetic-meme-revision-control")
    original = core.store.rows("SELECT * FROM model_usage")

    async def scenario():
        async with tls_core(core, tmp_path) as (origin, certificate), httpx.AsyncClient(
            base_url=origin, verify=ssl.create_default_context(cafile=str(certificate)),
            trust_env=False, headers={"Authorization": "Bearer " +
                                      core.settings.bridge_token.get_secret_value()},
        ) as http:
            query = {"expected_revision": 4, "policy": policy.model_dump(mode="json")}
            denied = await http.post("/internal/v1/owner-authority/preview", json=query)
            assert denied.status_code == 401
            headers = {"Authorization": "Bearer synthetic-meme-revision-control"}
            preview = await http.post("/internal/v1/owner-authority/preview", json=query,
                                      headers=headers)
            assert preview.status_code == 200, preview.text
            activation = {**query, "operation_id": "native-meme-capability-revision",
                          "preview_sha256": preview.json()["preview_sha256"]}
            active = await http.post("/internal/v1/owner-authority/activate", json=activation,
                                     headers=headers)
            assert active.status_code == 200, active.text
            repeated = await http.post("/internal/v1/owner-authority/activate", json=activation,
                                       headers=headers)
            assert repeated.status_code == 200 and repeated.json() == active.json()
            assert core.store.rows("SELECT * FROM model_usage") == original
            config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
                bot_qq_id=core.settings.bot_qq_id,
                bridge_token=core.settings.bridge_token.get_secret_value(), onebot_token="s" * 32,
                protocol_epoch=policy.capture.protocol_epoch)
            grant = SimpleNamespace(authority=active.json()["authority"],
                                    key=case.key, policy=policy)
            event, response = await admit(http, config, grant, core.clock.now, 901,
                                          text="太棒了，成功了")
            assert response.status_code == 202, response.text
            return event

    incoming = run(scenario())
    config, output = tmp_path / "worker.json", tmp_path / "worker-result.json"
    process_config(core, config)
    snapshot = json.loads(config.read_text(encoding="utf-8"))
    snapshot["voice"]["signing_private_key"] = (
        core.settings.voice.signing_private_key.get_secret_value())
    for name, node in core.settings.voice.nodes.items():
        snapshot["voice"]["nodes"][name]["token"] = node.token.get_secret_value()
    config.write_text(json.dumps(snapshot), encoding="utf-8")

    async def worker():
        child = await process("tests.meme_native_acquisition_process", config,
                              core.clock(), "-", output)
        try:
            async with asyncio.timeout(40):
                _, error = await child.communicate()
            assert child.returncode == 0, error.decode(errors="replace")
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

    run(worker())
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["worked"] and report["calls"] == ["chat"] and report["requests"] == 0
    discovery = core.store.one("SELECT * FROM jobs WHERE kind='meme_discovery' AND event_id=?",
                               (incoming["event_id"],))
    assert discovery is not None and discovery["state"] == "QUEUED", report
    owner_authority.check_job(core.store, core.settings, core.clock, discovery, purpose="meme")
    assert core.store.one("SELECT * FROM model_usage WHERE request_id=?",
                          (original[0]["request_id"],)) == original[0]
