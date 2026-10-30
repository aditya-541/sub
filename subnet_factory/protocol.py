"""Typed wire contracts shared by Factory miners and validators."""

from __future__ import annotations

from enum import Enum
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SafeRelativePath = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=240,
        pattern=r"^[A-Za-z0-9._/-]+$",
    ),
]


def validate_relative_path(value: str) -> str:
    parts = value.split("/")
    if value.startswith("/") or ".." in parts or "\\" in value:
        raise ValueError("Path must stay within the generated project.")
    return value


class TaskKind(str, Enum):
    DISCOVER = "discover"
    ARCHITECTURE = "architecture"
    GENERATE = "generate"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BuildTask(StrictModel):
    task_id: Annotated[str, StringConstraints(min_length=8, max_length=80)]
    kind: TaskKind
    prompt: Annotated[str, StringConstraints(min_length=12, max_length=12_000)]
    specification: dict[str, object] = Field(default_factory=dict)
    required_paths: list[SafeRelativePath] = Field(default_factory=list, max_length=100)
    sdk_target: str = "bittensor-v11"
    deadline_seconds: int = Field(default=300, ge=5, le=1800)

    @field_validator("required_paths")
    @classmethod
    def paths_must_be_relative(cls, values: list[str]) -> list[str]:
        return [validate_relative_path(value) for value in values]


class SubnetSuggestion(StrictModel):
    name: Annotated[str, StringConstraints(min_length=3, max_length=80)]
    problem: Annotated[str, StringConstraints(min_length=20, max_length=1000)]
    commodity: Annotated[str, StringConstraints(min_length=3, max_length=300)]
    users: Annotated[str, StringConstraints(min_length=3, max_length=500)]
    verification: Annotated[str, StringConstraints(min_length=10, max_length=1000)]
    risks: list[str] = Field(min_length=1, max_length=12)
    rationale: Annotated[str, StringConstraints(min_length=20, max_length=1000)]


class ProjectFile(StrictModel):
    path: SafeRelativePath
    content: Annotated[str, StringConstraints(max_length=200_000)]
    media_type: str = "text/plain"

    @field_validator("path")
    @classmethod
    def path_must_be_relative(cls, value: str) -> str:
        return validate_relative_path(value)


class BuildContribution(StrictModel):
    contribution_id: str = Field(default_factory=lambda: uuid4().hex)
    task_id: str
    miner_hotkey: str | None = None
    summary: Annotated[str, StringConstraints(min_length=10, max_length=2000)]
    suggestions: list[SubnetSuggestion] = Field(default_factory=list, max_length=20)
    architecture: dict[str, object] = Field(default_factory=dict)
    files: list[ProjectFile] = Field(default_factory=list, max_length=100)
    assumptions: list[str] = Field(default_factory=list, max_length=40)
    generator: str = "unconfigured"


class ValidationReport(StrictModel):
    task_id: str
    contribution_id: str
    accepted: bool
    score: float = Field(ge=0, le=1)
    checks: dict[str, bool]
    findings: list[str]


class AggregateRequest(StrictModel):
    contributions: list[BuildContribution] = Field(min_length=1, max_length=100)
    reports: list[ValidationReport] = Field(min_length=1, max_length=100)


class DispatchResult(StrictModel):
    task_id: str
    attempted_peers: int
    contribution_count: int
    accepted_count: int
    suggestions: list[SubnetSuggestion] = Field(default_factory=list)
    contributions: list[BuildContribution] = Field(default_factory=list)
    reports: list[ValidationReport] = Field(default_factory=list)


class PlannedWeight(StrictModel):
    hotkey: str
    uid: int = Field(ge=0)
    raw_score: float = Field(ge=0, le=1)
    weight: float = Field(ge=0, le=1)


class WeightPlan(StrictModel):
    netuid: int = Field(ge=0)
    weights: list[PlannedWeight]
    excluded_submissions: int = Field(ge=0)
    status: str = "plan-only"
