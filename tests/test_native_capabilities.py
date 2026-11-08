"""Native status shares admission rules and never creates execution custody."""

from contextlib import closing
from pathlib import Path

import pytest

from companion import owner_authority, paid_budget
from companion.file_evidence import PrivateFilesConfig
from companion.image_input import ImageInputPolicy
from companion.owner_authority_protocol import AuthorityStop, OwnerGrantPolicy
from companion.store import Store
from tests.postgres_fixture import logical_state
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare
from tests.support.runtime_capabilities import rows
from tests.support.voice_jobs import voice_config


def native(core, tmp_path):
    policy = prepare(core, tmp_path)
    core.settings.content_enabled = True
    core.settings.observation_enabled = True
    core.settings.memory_enabled = True
    core.settings.diary_enabled = True
    core.settings.voice = voice_config()
    core.settings.private_files = PrivateFilesConfig(
        enabled=True, parser_image="sha256:" + "f" * 64)
    core.settings.paid_model = core.settings.paid_model.model_copy(update={
        "image_input": ImageInputPolicy(approval_sha256="b" * 64,
            estimated_tokens_per_image=1000, max_bytes_per_image=100000,
            max_pixels_per_image=10000, max_images_per_request=1)})
    hashes = owner_authority.capability_hashes(core.settings)
    policy = OwnerGrantPolicy.model_validate({**policy.model_dump(),
        "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings)),
        "purposes": ["image", "file", "voice", "content"],
        "capability_policies": {name: hashes[name] for name in ("image", "file", "voice")}})
    return activate(core, policy)[0]


def test_native_media_readiness_needs_no_execution_window_and_is_read_only(core, tmp_path):
    native(core, tmp_path)
    assert core.settings.live_text_window is None
    journal = Path(core.settings.paid_model.journal_path)
    before = (logical_state(core.store), journal.read_bytes())
    current = rows(core)
    for name in ("qq.images", "qq.files", "qq.voice.asr", "qq.voice.tts"):
        assert current[name]["state"] == "READY", current[name]
    assert (logical_state(core.store), journal.read_bytes()) == before
    for name in ("memory.manage", "memory.recall", "diary"):
        assert current[name]["state"] == "UNAVAILABLE"
        assert "OWNER_AUTHORITY_PURPOSE_REQUIRED" in current[name]["reason_codes"]


def test_inventory_keeps_one_read_only_snapshot_across_an_independent_commit(
    core, tmp_path, monkeypatch,
):
    native(core, tmp_path)
    current = owner_authority.current
    scopes = []
    marker = "capability-projection-concurrent-commit"
    with closing(Store(core.database, core.settings.state_directory)) as writer:
        def inspect(store, *args, **kwargs):
            assert store.one("SHOW transaction_read_only") == {"transaction_read_only": "on"}
            scopes.append(store.frozen_scope)
            assert store.one("SELECT value FROM runtime_meta WHERE key=%s", (marker,)) is None
            if len(scopes) == 1:
                writer.insert("runtime_meta", key=marker, value="committed")
            assert store.one("SELECT value FROM runtime_meta WHERE key=%s", (marker,)) is None
            return current(store, *args, **kwargs)

        monkeypatch.setattr(owner_authority, "current", inspect)
        assert rows(core)["qq.images"]["state"] == "READY"
    assert len(scopes) > 1 and len(set(scopes)) == 1 and scopes[0] is not None
    assert core.store.one("SELECT value FROM runtime_meta WHERE key=%s", (marker,)) == {
        "value": "committed",
    }


@pytest.mark.parametrize("change,reason", [
    ("stop", "OWNER_AUTHORITY_DISABLED"),
    ("off", "OWNER_AUTHORITY_DISABLED"),
    ("policy", "OWNER_AUTHORITY_CAPABILITY_CHANGED"),
])
def test_native_status_fences_media_when_original_admission_changes(core, tmp_path, change, reason):
    approved = native(core, tmp_path)
    if change == "stop":
        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id="native-status-stop", authority=approved["authority"]))
    elif change == "off":
        core.settings.owner_runtime.mode = "OFF"
    else:
        core.settings.private_files = core.settings.private_files.model_copy(
            update={"max_daily_files": core.settings.private_files.max_daily_files + 1})
    journal = Path(core.settings.paid_model.journal_path)
    before = (logical_state(core.store), journal.read_bytes())
    current = rows(core)
    assert (logical_state(core.store), journal.read_bytes()) == before
    for name in ("qq.images", "qq.files", "qq.voice.asr", "qq.voice.tts"):
        assert current[name]["state"] == "UNAVAILABLE"
        assert reason in current[name]["reason_codes"]
