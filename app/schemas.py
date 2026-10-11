
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

class TaskCreate(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    instruction: str = Field(min_length=3, max_length=3000)
    keywords: list[str] = Field(default_factory=list, max_length=30)
    location: str = Field(default="", max_length=160)
    interval_minutes: int = Field(default=0, ge=0, le=10080)

class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=2, max_length=160)
    instruction: str | None = Field(default=None, min_length=3, max_length=3000)
    keywords: list[str] | None = Field(default=None, max_length=30)
    location: str | None = Field(default=None, max_length=160)
    interval_minutes: int | None = Field(default=None, ge=0, le=10080)


class AssistantCommand(BaseModel):
    instruction: str = Field(min_length=3, max_length=3000)
    title: str | None = Field(default=None, min_length=2, max_length=160)
    keywords: list[str] | None = Field(default=None, max_length=30)
    location: str | None = Field(default=None, max_length=160)
    interval_minutes: int = Field(default=0, ge=0, le=10080)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    web_search: bool = False


class WhatsAppMessageCreate(BaseModel):
    recipient: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    message: str = Field(min_length=1, max_length=4096)
    consent_confirmed: bool = False

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be blank")
        return value


class FindingUpdate(BaseModel):
    status: Literal["new", "relevant", "irrelevant", "duplicate", "contacted", "completed"]


class MemoryCreate(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=2000)


class PreferencesUpdate(BaseModel):
    quiet_hours_start: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    quiet_hours_end: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    morning_briefing: bool | None = None
    default_location: str | None = Field(default=None, max_length=160)
    default_keywords: list[str] | None = Field(default=None, max_length=30)


class ImportedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=80)
    source_id: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2000)
    task_id: int
    company: str = Field(default="", max_length=300)
    location: str = Field(default="", max_length=300)
    summary: str = Field(default="", max_length=5000)
    score: int = Field(default=0, ge=0, le=100)


class N8nFindingBatch(BaseModel):
    findings: list[ImportedFinding] = Field(min_length=1, max_length=500)


class PublicFeedCreate(BaseModel):
    name: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    url: str = Field(min_length=10, max_length=2000)
