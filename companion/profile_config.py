"""Owner configuration for the companion's self-updated display profile."""

from pydantic import Field

from companion.contracts import StrictModel


class ProfileConfig(StrictModel):
    # The model tool is registered only when enabled and the owner grant has "profile".
    enabled: bool = False
    # First display name when no explicit character is active; otherwise "伙伴".
    initial_display_name: str | None = Field(default=None, min_length=1, max_length=32)
    name_daily_limit: int = Field(default=1, ge=0, le=20, strict=True)
    name_cooldown_seconds: int = Field(default=3 * 86400, ge=0, le=366 * 86400, strict=True)
    avatar_daily_limit: int = Field(default=1, ge=0, le=20, strict=True)
