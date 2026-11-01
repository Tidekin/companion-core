"""Meme glossary settings; see companion/meme_wiki.py."""

from pydantic import Field

from companion.contracts import StrictModel


class MemeWikiConfig(StrictModel):
    enabled: bool = True
    # 串子程度 0–100; None leaves the banter scale to the model, as in dsh-memewiki.
    banter_level: int | None = Field(default=None, ge=0, le=100, strict=True)
