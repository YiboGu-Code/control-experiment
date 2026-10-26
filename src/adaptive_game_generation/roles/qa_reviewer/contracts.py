"""Criterion-level internal acceptance, distinct from external benchmark scoring."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictBool, model_validator

from ...models import IssueRecord, ReadinessReview
from ...models.base import Count, Identifier, Model, PositiveCount, Text, require_unique, utc_now


class QAConfig(Model):
    prompt_version: Literal["qa_reviewer.v1"] = "qa_reviewer.v1"
    max_output_tokens: PositiveCount = 24000
    max_model_turns: Annotated[int, Field(strict=True, ge=1, le=4)] = 4
    max_grounding_repairs: Annotated[int, Field(strict=True, ge=0, le=3)] = 0
    grounding_repair_tokens: PositiveCount = 8192
    max_visual_images: Annotated[int, Field(strict=True, ge=0, le=8)] = 0
    max_prompt_bytes: PositiveCount = 1024 * 1024
    max_response_bytes: PositiveCount = 512 * 1024
    max_artifacts: PositiveCount = 512
    max_input_bytes: PositiveCount = 64 * 1024 * 1024
    max_project_files: PositiveCount = 256
    max_source_bytes: PositiveCount = 512 * 1024
    max_sessions: PositiveCount = 32


class CriterionReview(Model):
    requirement_id: Identifier
    criterion_index: Count
    verdict: Literal["pass", "fail", "insufficient", "design_conflict"]
    basis: Literal["static", "runtime", "replay", "visual", "specification"]
    rationale: Text
    evidence_ids: tuple[Identifier, ...] = ()
    source_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def references(self):
        require_unique(self.evidence_ids, "criterion evidence IDs")
        require_unique(self.source_ids, "criterion source IDs")
        if self.verdict == "design_conflict" and self.basis != "specification":
            raise ValueError("Design conflicts must use specification basis")
        return self


class ResolvedCriterion(Model):
    requirement_id: Identifier
    criterion_index: Count


class IssueReview(Model):
    issue: IssueRecord
    source_ids: tuple[Identifier, ...] = ()
    instruction_excerpt: str = ""
    resolved_criteria: tuple[ResolvedCriterion, ...] = ()


class QAProposal(Model):
    summary: Text
    instruction_audit: Text
    criteria: tuple[CriterionReview, ...]
    issues: tuple[IssueReview, ...]
    ready: StrictBool
    readiness_rationale: Text
    limitations: tuple[Text, ...]

    @model_validator(mode="after")
    def unique(self):
        require_unique(((c.requirement_id, c.criterion_index) for c in self.criteria), "criterion reviews")
        require_unique((i.issue.issue_id for i in self.issues), "issue IDs")
        return self


class QAReport(Model):
    schema_version: Literal["qa_reviewer.1"] = "qa_reviewer.1"
    config: QAConfig
    reviewed_at: AwareDatetime = Field(default_factory=utc_now)
    task_id: Identifier
    game_revision_id: Identifier
    proposal: QAProposal
    effective_proposal: QAProposal
    grounding_notes: tuple[dict, ...] = ()
    requirement_statuses: dict[str, str]
    readiness: ReadinessReview
    gates: dict[str, bool]
    evidence_integrity: dict
    proposal_rejections: tuple[dict, ...] = ()
