"""Portable logical references; resolving paths is the executor's responsibility."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import StringConstraints, field_validator

from .base import Identifier, Model, PositiveCount, Role, Text


class ArtifactKind(str, Enum):
    DOCUMENT = "document"
    ASSET = "asset"
    GAME_PROJECT = "game_project"
    INPUT_TRACE = "input_trace"
    SCREENSHOT = "screenshot"
    VIDEO = "video"
    LOG = "log"
    REPORT = "report"
    CONFIG = "config"
    RESOURCE_LIBRARY = "resource_library"


class ArtifactRef(Model):
    artifact_id: Identifier
    version: PositiveCount = 1
    kind: ArtifactKind
    path: Text
    created_by: Role | Literal["system"]
    media_type: Text | None = None
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None

    @field_validator("path")
    @classmethod
    def portable_relative_path(cls, value: str) -> str:
        # Logical POSIX paths work the same on Windows and Linux. This does not
        # resolve symlinks or grant filesystem access; that belongs to step 2.
        if (
            value.startswith("/")
            or any(char in value for char in '\\:*?"<>|')
            or any(ord(char) < 32 for char in value)
            or any(part in ("", ".", "..") for part in value.split("/"))
            or any(part.endswith((" ", ".")) for part in value.split("/"))
        ):
            raise ValueError("path must be a normalized relative POSIX path")
        return value

    @property
    def key(self) -> tuple[str, int]:
        return self.artifact_id, self.version
