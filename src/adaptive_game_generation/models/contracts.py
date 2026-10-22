"""Role requests, results and typed update proposals; nothing is applied here."""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import Field, StrictBool, model_validator

from .artifacts import ArtifactRef
from .base import Count, Identifier, Model, Role, Text, VersionedModel, require_unique
from .domain import (
    AssetRecord, DesignSpec, EvidenceRecord, GameRevision, IssueRecord,
    ReadinessReview, Requirement,
)
from .resources import ResourceLimits


class RoleRequest(VersionedModel):
    request_id: Identifier
    run_id: Identifier
    task_id: Identifier
    role: Role
    work_item_ids: tuple[Identifier, ...] = ()
    objective: Text
    completion_criteria: Annotated[tuple[Text, ...], Field(min_length=1)]
    state_revision: Count
    artifact_refs: tuple[ArtifactRef, ...] = ()
    limits: ResourceLimits

    @model_validator(mode="after")
    def unique_references(self) -> RoleRequest:
        require_unique(self.work_item_ids, "work item IDs")
        require_unique((ref.key for ref in self.artifact_refs), "request artifact references")
        return self


class ProposedUpdates(Model):
    """Upserts, not arbitrary JSON patches. Task input and ledger are not writable."""

    requirements: tuple[Requirement, ...] = ()
    design: DesignSpec | None = None
    assets: tuple[AssetRecord, ...] = ()
    game_revision: GameRevision | None = None
    evidence: tuple[EvidenceRecord, ...] = ()
    issues: tuple[IssueRecord, ...] = ()
    readiness: ReadinessReview | None = None

    @model_validator(mode="after")
    def unique_updates(self) -> ProposedUpdates:
        for records, id_field in (
            (self.requirements, "requirement_id"), (self.assets, "asset_id"),
            (self.evidence, "evidence_id"), (self.issues, "issue_id"),
        ):
            require_unique((getattr(item, id_field) for item in records), id_field)
        return self

    @property
    def is_empty(self) -> bool:
        return not any(getattr(self, name) for name in type(self).model_fields)


class ResultStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    NO_OP = "no_op"
    FAILED = "failed"


class Blocker(Model):
    description: Text
    dependency_ids: tuple[Identifier, ...] = ()


class ErrorInfo(Model):
    code: Identifier
    message: Text
    retryable: StrictBool = False


class RoleResult(VersionedModel):
    request_id: Identifier
    role: Role
    state_revision: Count
    status: ResultStatus
    summary: Text
    artifact_refs: tuple[ArtifactRef, ...] = ()
    proposed_updates: ProposedUpdates = Field(default_factory=ProposedUpdates)
    evidence_refs: tuple[Identifier, ...] = ()
    blockers: tuple[Blocker, ...] = ()
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> RoleResult:
        if self.status == ResultStatus.BLOCKED and not self.blockers:
            raise ValueError("blocked result requires blockers")
        if self.status == ResultStatus.FAILED and self.error is None:
            raise ValueError("failed result requires an error")
        if self.error is not None and self.status not in (ResultStatus.FAILED, ResultStatus.PARTIAL):
            raise ValueError("error is only valid for failed or partial results")
        if self.status == ResultStatus.NO_OP and (
            self.artifact_refs or not self.proposed_updates.is_empty
        ):
            raise ValueError("no_op cannot produce artifacts or state updates")
        require_unique((ref.key for ref in self.artifact_refs), "result artifact references")
        require_unique(self.evidence_refs, "result evidence references")
        return self

    def validate_for(self, request: RoleRequest) -> None:
        if (self.request_id, self.role, self.state_revision) != (
            request.request_id, request.role, request.state_revision
        ):
            raise ValueError("result does not match request ID, role or state revision")
