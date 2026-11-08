"""Owner jargon learning settings; see companion/jargon.py."""

from pydantic import Field

from companion.contracts import StrictModel


class JargonConfig(StrictModel):
    enabled: bool = True
    batch_messages: int = Field(default=12, ge=4, le=40, strict=True)
    max_output_tokens: int = Field(default=300, ge=64, le=1024, strict=True)
    max_attempts: int = Field(default=2, ge=1, le=5, strict=True)
    max_context_terms: int = Field(default=5, ge=1, le=10, strict=True)
