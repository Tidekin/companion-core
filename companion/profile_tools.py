"""Narrow model tools for the companion's own display name.

The acting identity is the leased owner chat job, bound on the server; the model
never names a target or an actor. Only a `confirmed` result is a committed fact.
"""

from typing import Annotated

from pydantic import Field

from companion import companion_profile


def available(core, view):
    return core.settings.profile.enabled and "profile" in view.policy.purposes


def toolset(core, job, snapshots):
    from pydantic_ai import RunContext
    from pydantic_ai.toolsets import FunctionToolset

    from companion.agent_runtime import check_conversation

    async def get_own_profile() -> dict:
        """Read your own current display name, its revision and whether you may change it.

        Call this before update_display_profile to get the current revision.
        """
        def read():
            check_conversation(core, job, snapshots, purpose="profile")
            current = companion_profile.view(core.store, core.settings)
            return {"display_name": current["display_name"], "revision": current["revision"],
                    "may_update_name": current["auto_name"],
                    "name_rules": "1-32 个可见字符；不能是空白、控制字符，也不能冒充主人或系统"}

        return core.store.read_or_write(read)

    async def update_display_profile(
        ctx: RunContext,
        display_name: Annotated[str, Field(min_length=1, max_length=64)],
        expected_revision: Annotated[int, Field(ge=0)],
        note: Annotated[str | None, Field(max_length=200)] = None,
    ) -> dict:
        """Change your own display name. It does not change who you are, your memories,
        your relationship with the owner or what you are allowed to do.

        Decide yourself whether a new name fits; the owner does not need to approve each
        change. Only status="confirmed" means the name changed. disabled_by_user,
        rate_limited, validation_failed and revision_conflict mean it did not: do not
        claim it did, and do not retry in a loop.

        Args:
            display_name: The new name you want to be shown with.
            expected_revision: The revision from get_own_profile.
            note: Optional short, owner-readable reason; not your hidden reasoning.
        """
        with core.store.transaction():
            check_conversation(core, job, snapshots, purpose="profile")
            return companion_profile.update_by_companion(
                core.store, core.settings, core.clock(), display_name=display_name,
                expected_revision=expected_revision, note=note, job_id=job["job_id"],
                tool_call_id=ctx.tool_call_id)

    return FunctionToolset([get_own_profile, update_display_profile], sequential=True,
                           max_retries=1)
