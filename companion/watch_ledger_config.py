"""Watch ledger switch and bounds; off by default, so the paid contract is unchanged."""

from pydantic import Field

from companion.contracts import StrictModel


class WatchLedgerConfig(StrictModel):
    enabled: bool = False
    max_output_tokens: int = Field(default=200, ge=64, le=512, strict=True)
    lookback_hours: int = Field(default=72, ge=1, le=720, strict=True)
    max_attempts: int = Field(default=2, ge=1, le=5, strict=True)
