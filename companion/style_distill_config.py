"""Style distillation switch and bounds; off by default, so the paid contract is unchanged."""

from pydantic import Field

from companion.contracts import StrictModel


class StyleDistillConfig(StrictModel):
    enabled: bool = False
    daily_pairs: int = Field(default=50, ge=1, le=500, strict=True)
    max_output_tokens: int = Field(default=300, ge=64, le=1024, strict=True)
    max_attempts: int = Field(default=2, ge=1, le=5, strict=True)
    candidate_days: int = Field(default=30, ge=1, le=365, strict=True)
