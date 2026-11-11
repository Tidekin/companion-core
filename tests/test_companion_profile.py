"""Companion display profile: stable identity, guarded self-updates and owner restore."""

import pytest

from companion import companion_profile as profile
from companion import history_import, profile_tools
from companion.contracts import CoreError
from companion.profile_config import ProfileConfig
from tests.support.history_import_package import package


def companion_update(core, name, revision, *, call="call-1", job="job-1", note=None):
    with core.store.transaction():
        return profile.update_by_companion(
            core.store, core.settings, core.clock(), display_name=name,
            expected_revision=revision, note=note, job_id=job, tool_call_id=call)


def audit(core):
    return [(row["actor_type"], row["fields"], row["new_name"])
            for row in reversed(profile.changes(core.store))]


@pytest.mark.parametrize(("value", "reason"), [
    ("", "NAME_EMPTY"), ("   ", "NAME_EMPTY"), ("小​满", "NAME_INVISIBLE_OR_CONTROL_CHARACTER"),
    ("a‮b", "NAME_INVISIBLE_OR_CONTROL_CHARACTER"), ("主人", "NAME_IMPERSONATES_OWNER_OR_SYSTEM"),
    ("SYSTEM", "NAME_IMPERSONATES_OWNER_OR_SYSTEM"), ("名" * 33, "NAME_TOO_LONG"),
    ("a  b", "NAME_REPEATED_SPACES"),
])
def test_names_reject_invisible_impersonating_and_overlong_values(value, reason):
    with pytest.raises(ValueError, match=reason):
        profile.validate_name(value)


def test_names_are_normalized_and_counted_by_visible_characters():
    assert profile.validate_name("  小满 ") == "小满"
    assert profile.validate_name("é" * 32) == "é" * 32
    assert profile.validate_name("名" * 32) == "名" * 32


def test_initial_view_uses_character_then_config_then_default(core):
    assert profile.current(core.store, core.settings)["display_name"] == "伙伴"
    core.settings.profile = ProfileConfig(initial_display_name="小肥鱼")
    assert profile.current(core.store, core.settings) == {
        "companion_id": None, "revision": 0, "display_name": "小肥鱼",
        "avatar_asset_id": None, "updated_at": None}
    history_import.set_character(core, package()[0].character)
    assert profile.current(core.store, core.settings)["display_name"] == "汐米"
    assert core.store.rows("SELECT * FROM companion_profile") == []


def test_companion_renames_itself_once_per_call_and_keeps_identity(core):
    core.settings.profile = ProfileConfig(enabled=True, initial_display_name="小肥鱼")
    first = companion_update(core, "小满", 0)
    assert first["status"] == "revision_conflict" and first["profile"]["revision"] == 1
    identity = profile.current(core.store, core.settings)["companion_id"]
    assert identity.startswith("cmp_")
    result = companion_update(core, " 小满 ", 1, call="call-2", note="想换个新名字")
    assert result["status"] == "confirmed" and result["new_revision"] == 2
    assert result["profile"] == {"revision": 2, "display_name": "小满", "avatar_asset_id": None}
    # Retrying the same tool call returns the committed result without a second write.
    assert companion_update(core, "别的名字", 1, call="call-2") == result
    current = profile.current(core.store, core.settings)
    assert current["display_name"] == "小满" and current["companion_id"] == identity
    assert audit(core) == [("system", "initial", "小肥鱼"), ("companion", "name", "小满")]
    assert companion_update(core, "小满", 2, call="call-3")["status"] == "no_change"
    assert len(audit(core)) == 2


def test_owner_switch_rate_limit_and_validation_never_commit(core):
    core.settings.profile = ProfileConfig(enabled=True, name_cooldown_seconds=3600)
    assert companion_update(core, "小满", 0)["status"] == "revision_conflict"
    bad = companion_update(core, "主人", 1, call="c-bad")
    assert bad["status"] == "validation_failed" and bad["reason"].startswith("NAME_")
    assert companion_update(core, "小满", 1, call="c-ok")["status"] == "confirmed"
    limited = companion_update(core, "小雨", 2, call="c-again")
    assert limited["status"] == "rate_limited" and limited["retry_after_seconds"] > 0
    core.clock.advance(86400 + 1)
    owner = profile.owner_settings(core.store)
    with core.store.transaction():
        profile.update_settings(core.store, core.settings, core.clock(),
                                expected_settings_revision=owner["settings_revision"],
                                auto_name=False)
    disabled = companion_update(core, "小雨", 2, call="c-off")
    assert disabled == {"status": "disabled_by_user", "field": "display_name",
                        "profile": {"revision": 2, "display_name": "小满", "avatar_asset_id": None}}
    assert [entry[2] for entry in audit(core)] == ["伙伴", "小满"]


def test_owner_restore_is_a_new_revision_and_fences_the_companion(core):
    core.settings.profile = ProfileConfig(enabled=True, name_cooldown_seconds=0)
    companion_update(core, "x", 0)
    assert companion_update(core, "小满", 1, call="c1")["status"] == "confirmed"
    initial = profile.changes(core.store)[-1]
    with core.store.transaction():
        restored = profile.restore(core.store, core.settings, core.clock(),
                                   change_id=initial["change_id"], expected_revision=2,
                                   pause_name=True, operation_id="restore-op-1")
    assert restored["display_name"] == "伙伴" and restored["revision"] == 3
    assert restored["auto_name"] is False
    with core.store.transaction():
        again = profile.restore(core.store, core.settings, core.clock(),
                                change_id=initial["change_id"], expected_revision=2,
                                pause_name=True, operation_id="restore-op-1")
    assert again["change_id"] == restored["change_id"]
    with pytest.raises(CoreError, match="PROFILE_REVISION_CONFLICT"), core.store.transaction():
        profile.restore(core.store, core.settings, core.clock(), change_id=initial["change_id"],
                        expected_revision=2, pause_name=False, operation_id="restore-op-2")
    # A request prepared before the restore cannot overwrite it.
    assert companion_update(core, "小雨", 2, call="c2")["status"] in {
        "disabled_by_user", "revision_conflict"}
    assert audit(core) == [("system", "initial", "伙伴"), ("companion", "name", "小满"),
                           ("owner", "name", "伙伴")]


def test_alias_is_separate_from_the_companion_name(core):
    core.settings.profile = ProfileConfig(enabled=True)
    with core.store.transaction():
        result = profile.set_alias(core.store, core.settings, core.clock(),
                                   expected_settings_revision=0, alias="鱼鱼")
    assert result["viewer_alias"] == "鱼鱼" and result["display_name"] == "伙伴"
    with pytest.raises(CoreError, match="PROFILE_SETTINGS_REVISION_CONFLICT"), \
            core.store.transaction():
        profile.set_alias(core.store, core.settings, core.clock(),
                          expected_settings_revision=0, alias="别名")
    with core.store.transaction():
        cleared = profile.set_alias(core.store, core.settings, core.clock(),
                                    expected_settings_revision=result["settings_revision"],
                                    alias=None)
    assert cleared["viewer_alias"] is None and cleared["display_name"] == "伙伴"


def test_tools_need_the_switch_and_the_profile_purpose(core):
    class View:
        class policy:
            purposes = ["chat"]

    assert not profile_tools.available(core, View)
    core.settings.profile = ProfileConfig(enabled=True)
    assert not profile_tools.available(core, View)
    View.policy.purposes = ["chat", "profile"]
    assert profile_tools.available(core, View)


def test_prompt_fact_names_display_and_character_separately(core):
    core.settings.profile = ProfileConfig(enabled=True)
    history_import.set_character(core, package()[0].character)
    assert "「汐米」" in profile.prompt_fact(core.store, core.settings)
    companion_update(core, "x", 0)
    companion_update(core, "小满", 1, call="c1")
    fact = profile.prompt_fact(core.store, core.settings)
    assert "「小满」" in fact and "角色名是「汐米」" in fact
