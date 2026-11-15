"""Source-grounded private check-ins; these types grant no permission."""

from pydantic import AwareDatetime, Field, model_validator

from companion.contracts import StrictModel


class CareConfig(StrictModel):
    enabled: bool = True
    daily_limit: int = Field(default=2, ge=0, le=10)
    jitter_seconds: int = Field(default=600, ge=0, le=1800)
    useful_seconds: int = Field(default=7200, ge=300, le=21600)
    recent_chat_seconds: int = Field(default=600, ge=60, le=3600)


class CarePlan(StrictModel):
    about: str = Field(min_length=2, max_length=160)
    quote: str = Field(min_length=2, max_length=500)
    check_at: AwareDatetime
    plan_id: str | None = Field(default=None, pattern=r"^care_[a-f0-9]{32}$")
    expected_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def revision_pair(self):
        if (self.plan_id is None) != (self.expected_version is None):
            raise ValueError("updating a plan requires its current id and version")
        return self


class CareClosure(StrictModel):
    plan_id: str = Field(pattern=r"^care_[a-f0-9]{32}$")
    expected_version: int = Field(ge=1)
    quote: str = Field(min_length=2, max_length=500)
    resolved: bool = True
