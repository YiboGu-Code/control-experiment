"""Task, requirements, design, assets, game versions, evidence and review records."""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import AwareDatetime, Field, StrictBool, model_validator

from .artifacts import ArtifactKind, ArtifactRef
from .base import (
    CheckStatus, Identifier, Model, PositiveCount, Role, Text, require_unique, utc_now,
)


class TaskSpec(Model):
    task_id: Identifier
    instruction: Text
    benchmark_snapshot: Text | None = None
    environment_constraints: tuple[Text, ...] = ()
    resource_refs: tuple[ArtifactRef, ...] = ()


class RequirementStatus(str, Enum):
    NOT_IMPLEMENTED = "not_implemented"
    IMPLEMENTED_UNVERIFIED = "implemented_unverified"
    VERIFIED_PASS = "verified_pass"
    VERIFIED_FAIL = "verified_fail"


class Verification(Model):
    game_revision_id: Identifier
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    rationale: Text
    reviewed_at: AwareDatetime = Field(default_factory=utc_now)


class Requirement(Model):
    requirement_id: Identifier
    description: Text
    source_excerpt: Text
    acceptance_criteria: Annotated[tuple[Text, ...], Field(min_length=1)]
    dependency_ids: tuple[Identifier, ...] = ()
    status: RequirementStatus = RequirementStatus.NOT_IMPLEMENTED
    verification: Verification | None = None

    @model_validator(mode="after")
    def consistent_verification(self) -> Requirement:
        verified = self.status in (
            RequirementStatus.VERIFIED_PASS, RequirementStatus.VERIFIED_FAIL
        )
        if verified != (self.verification is not None):
            raise ValueError("verified status requires verification; other states must clear it")
        require_unique(self.dependency_ids, "requirement dependencies")
        if self.requirement_id in self.dependency_ids:
            raise ValueError("requirement cannot depend on itself")
        return self


class DesignSpec(Model):
    version: PositiveCount
    document_ref: ArtifactRef
    change_summary: Text
    assumptions: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def document_kind(self) -> DesignSpec:
        if self.document_ref.kind != ArtifactKind.DOCUMENT:
            raise ValueError("design must reference a document")
        return self


class AssetRecord(Model):
    asset_id: Identifier
    artifact_ref: ArtifactRef
    purpose: Text
    source: Text
    license: Text
    integration_notes: Text

    @model_validator(mode="after")
    def asset_kind(self) -> AssetRecord:
        if self.artifact_ref.kind != ArtifactKind.ASSET:
            raise ValueError("asset must reference an asset artifact")
        return self


class GameRevision(Model):
    revision_id: Identifier
    project_ref: ArtifactRef
    change_summary: Text
    parent_revision_id: Identifier | None = None
    affected_requirement_ids: tuple[Identifier, ...] = ()
    build_status: CheckStatus = CheckStatus.NOT_RUN
    build_evidence_ids: tuple[Identifier, ...] = ()
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def consistent_build(self) -> GameRevision:
        if self.project_ref.kind != ArtifactKind.GAME_PROJECT:
            raise ValueError("game revision must reference a game project")
        if (self.build_status != CheckStatus.NOT_RUN) != bool(self.build_evidence_ids):
            raise ValueError("a completed build check requires evidence")
        if self.parent_revision_id == self.revision_id:
            raise ValueError("game revision cannot be its own parent")
        return self


class EvidenceKind(str, Enum):
    BUILD_CHECK = "build_check"
    PLAYTHROUGH = "playthrough"
    REPLAY = "replay"
    OBSERVATION = "observation"


class EvidenceRecord(Model):
    evidence_id: Identifier
    kind: EvidenceKind
    game_revision_id: Identifier
    requirement_ids: tuple[Identifier, ...] = ()
    artifact_refs: Annotated[tuple[ArtifactRef, ...], Field(min_length=1)]
    observation: Text
    outcome: CheckStatus = CheckStatus.NOT_RUN
    scenario: Text | None = None
    recorded_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def replay_has_trace(self) -> EvidenceRecord:
        require_unique((ref.key for ref in self.artifact_refs), "evidence artifact references")
        require_unique(self.requirement_ids, "evidence requirements")
        if self.kind == EvidenceKind.REPLAY and not any(
            ref.kind == ArtifactKind.INPUT_TRACE for ref in self.artifact_refs
        ):
            raise ValueError("replay evidence requires an input trace")
        return self


class IssueType(str, Enum):
    REQUIREMENT_GAP = "requirement_gap"
    DESIGN = "design"
    RUNTIME = "runtime"
    GAMEPLAY = "gameplay"
    VISUAL = "visual"
    TRACE = "trace"
    EVIDENCE_GAP = "evidence_gap"
    TOOL = "tool"


class Severity(str, Enum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"


class IssueStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    AWAITING_VERIFICATION = "awaiting_verification"
    CLOSED = "closed"


class IssueRecord(Model):
    issue_id: Identifier
    issue_type: IssueType
    severity: Severity
    description: Text
    suggested_role: Role
    requirement_ids: tuple[Identifier, ...] = ()
    evidence_ids: tuple[Identifier, ...] = ()
    reproduction_steps: tuple[Text, ...] = ()
    status: IssueStatus = IssueStatus.OPEN
    resolution: Text | None = None

    @model_validator(mode="after")
    def closed_issue_has_evidence(self) -> IssueRecord:
        if self.status == IssueStatus.CLOSED and (not self.resolution or not self.evidence_ids):
            raise ValueError("closing an issue requires a resolution and evidence")
        return self


class ReadinessReview(Model):
    """QA's recommendation, not permission to terminate or an official score."""

    game_revision_id: Identifier
    ready: StrictBool
    rationale: Text
    evidence_ids: tuple[Identifier, ...] = ()
    blocking_issue_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def ready_has_evidence(self) -> ReadinessReview:
        if self.ready and (not self.evidence_ids or self.blocking_issue_ids):
            raise ValueError("ready review requires evidence and no listed blocking issues")
        return self
