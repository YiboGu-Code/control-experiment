"""Common validation, identifiers and schema versioning. No I/O on import."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Iterable, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Count = Annotated[int, Field(strict=True, ge=0)]
PositiveCount = Annotated[int, Field(strict=True, gt=0)]
Seconds = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
T = TypeVar("T", bound="Model")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def require_unique(values: Iterable[object], label: str) -> None:
    items = list(values)
    if len(items) != len(set(items)):
        raise ValueError(f"duplicate {label}")


class Model(BaseModel):
    """Immutable validated values; collections use tuples rather than mutable dicts."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_default=True, revalidate_instances="always"
    )

    def revised(self: T, **changes: object) -> T:
        """Create a validated value, unlike model_copy(update=...) which skips checks."""
        return type(self).model_validate({**self.model_dump(), **changes})


class VersionedModel(Model):
    schema_version: Literal["1.0"] = "1.0"


class Role(str, Enum):
    DESIGNER = "designer"
    ART_PRODUCER = "art_producer"
    DEVELOPER = "developer"
    PLAYER = "player"
    QA_REVIEWER = "qa_reviewer"


class ControllerKind(str, Enum):
    FIXED_LOOP = "fixed_loop"
    ADAPTIVE_SCHEDULER = "adaptive_scheduler"


class CheckStatus(str, Enum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
