"""HTTP request and response schemas."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateRunRequest(APIModel):
    objective: str = Field(min_length=1, max_length=100_000)
    repository_path: Path | None = None
    auto_advance: bool = False


class LifecycleRequestBody(APIModel):
    reason: str = Field(min_length=1, max_length=4_000)


class ResumeRequestBody(APIModel):
    maximum_tasks: int | None = Field(default=None, ge=0)
