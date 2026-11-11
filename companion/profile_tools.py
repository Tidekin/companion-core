"""Narrow model tools for the companion's own display name and avatar.

The acting identity is the leased owner chat job, bound on the server; the model
never names a target or an actor. Only a `confirmed` result is a committed fact.
"""

from typing import Annotated

from pydantic import Field

from companion import companion_avatar, companion_profile


def available(core, view):
    return core.settings.profile.enabled and "profile" in view.policy.purposes


def toolset(core, job, snapshots):
    from pydantic_ai import RunContext
    from pydantic_ai.toolsets import FunctionToolset

    from companion.agent_runtime import check_conversation

    async def get_own_profile() -> dict:
        """Read your own display name, avatar, revision and what you may change.

        avatar_choices are images the owner uploaded for you; you may only pick one of them.

        Call this before update_display_profile to get the current revision.
        """
        def read():
            check_conversation(core, job, snapshots, purpose="profile")
            current = companion_profile.view(core.store, core.settings)
            return {"display_name": current["display_name"], "revision": current["revision"],
                    "avatar_asset_id": current["avatar_asset_id"],
                    "may_update_name": current["auto_name"],
                    "may_update_avatar": current["auto_avatar"],
                    "avatar_choices": [
                        {"asset_id": item["asset_id"], "uploaded_at": item["created_at"]}
                        for item in companion_avatar.available(core.store)],
                    "name_rules": "1-32 个可见字符；不能是空白、控制字符，也不能冒充主人或系统"}

        return core.store.read_or_write(read)

    async def update_display_profile(
        ctx: RunContext,
        expected_revision: Annotated[int, Field(ge=0)],
        display_name: Annotated[str | None, Field(min_length=1, max_length=64)] = None,
        avatar_asset_id: Annotated[str | None, Field(max_length=64)] = None,
        note: Annotated[str | None, Field(max_length=200)] = None,
    ) -> dict:
        """Change your own display name and/or avatar. It does not change who you are,
        your memories, your relationship with the owner or what you are allowed to do.

        Decide yourself whether a new name fits; the owner does not need to approve each
        change. Both requested fields change together or not at all. Only
        status="confirmed" means it changed. disabled_by_user, rate_limited,
        validation_failed, asset_not_ready and revision_conflict mean it did not: do not
        claim it did, and do not retry in a loop.

        Args:
            expected_revision: The revision from get_own_profile.
            display_name: The new name you want to be shown with, if changing it.
            avatar_asset_id: One asset_id from avatar_choices, if changing the avatar.
            note: Optional short, owner-readable reason; not your hidden reasoning.
        """
        with core.store.transaction():
            check_conversation(core, job, snapshots, purpose="profile")
            return companion_profile.update_by_companion(
                core.store, core.settings, core.clock(), display_name=display_name,
                avatar_asset_id=avatar_asset_id, expected_revision=expected_revision,
                note=note, job_id=job["job_id"],
                tool_call_id=ctx.tool_call_id)

    return FunctionToolset([get_own_profile, update_display_profile], sequential=True,
                           max_retries=1)
