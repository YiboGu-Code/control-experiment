"""Designer model output is a specification patch, never a writable runtime result."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from ...models import Blocker
from ...models.base import Identifier, Model, PositiveCount, Text, require_unique

PROMPT_VERSION = "designer.v3"
SectionId = Literal[
    "gameplay", "controls", "state_flow", "content_plan", "visual_feedback",
    "asset_requirements", "implementation_plan", "validation_plan",
]
SECTION_TITLES = {
    "gameplay": "Core gameplay",
    "controls": "Controls",
    "state_flow": "State transitions, outcomes, and retry",
    "content_plan": "Levels, content, and numeric configuration",
    "visual_feedback": "Visual feedback and UI states",
    "asset_requirements": "Asset requirements and integration",
    "implementation_plan": "Implementation contracts and dependencies",
    "validation_plan": "Observable acceptance and playthrough plan",
}


class DesignerConfig(Model):
    prompt_version: Literal["designer.v2", "designer.v3"] = PROMPT_VERSION
    max_output_tokens: PositiveCount = 8192
    max_prompt_bytes: PositiveCount = 512 * 1024
    max_response_bytes: PositiveCount = 256 * 1024
    max_grounding_repairs: Annotated[int, Field(strict=True, ge=0, le=2)] = 0


class GroundingCorrection(Model):
    requirement_id: Identifier
    source_excerpt: Text


class GroundingRepair(Model):
    corrections: Annotated[tuple[GroundingCorrection, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def unique(self):
        require_unique((c.requirement_id for c in self.corrections), "grounding correction IDs")
        return self


class RequirementDraft(Model):
    requirement_id: Identifier
    description: Text
    source_excerpt: Text
    acceptance_criteria: Annotated[tuple[Text, ...], Field(min_length=1)]
    dependency_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def dependencies(self) -> RequirementDraft:
        require_unique(self.dependency_ids, "requirement dependencies")
        if self.requirement_id in self.dependency_ids:
            raise ValueError("requirement cannot depend on itself")
        return self


class DesignSection(Model):
    section_id: SectionId
    content: Text
    requirement_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def references(self) -> DesignSection:
        require_unique(self.requirement_ids, "section requirement references")
        return self


class DesignerProposal(Model):
    status: Literal["completed", "no_op", "blocked"]
    summary: Text
    requirements: tuple[RequirementDraft, ...] = ()
    sections: tuple[DesignSection, ...] = ()
    # null preserves previous assumptions; [] explicitly clears them.
    assumptions: tuple[Text, ...] | None = None
    blockers: tuple[Blocker, ...] = ()

    @model_validator(mode="after")
    def consistent_proposal(self) -> DesignerProposal:
        require_unique((r.requirement_id for r in self.requirements), "requirement IDs")
        require_unique((s.section_id for s in self.sections), "design sections")
        if (self.status == "blocked") != bool(self.blockers):
            raise ValueError("only blocked proposals require blockers")
        if self.status != "completed" and (
            self.requirements or self.sections or self.assumptions is not None
        ):
            raise ValueError("no_op and blocked proposals cannot contain changes")
        return self


class DesignDocument(Model):
    """Full versioned specification; progress remains authoritative in SharedState."""

    schema_version: Literal["designer.1"] = "designer.1"
    task_id: Identifier
    version: PositiveCount
    previous_version: PositiveCount | None
    designer_config: DesignerConfig
    change_summary: Text
    changed_requirement_ids: tuple[Identifier, ...]
    changed_section_ids: tuple[SectionId, ...]
    feedback_issue_ids: tuple[Identifier, ...]
    requirements: Annotated[tuple[RequirementDraft, ...], Field(min_length=1)]
    sections: tuple[DesignSection, ...]
    assumptions: tuple[Text, ...]

    @model_validator(mode="after")
    def complete_design(self) -> DesignDocument:
        if self.version != (self.previous_version or 0) + 1:
            raise ValueError("design versions must be consecutive")
        requirements = {r.requirement_id: r for r in self.requirements}
        require_unique((r.requirement_id for r in self.requirements), "requirement IDs")
        require_unique((s.section_id for s in self.sections), "design sections")
        if {s.section_id for s in self.sections} != SECTION_TITLES.keys():
            raise ValueError("all eight design sections are required")
        covered = set()
        for section in self.sections:
            if set(section.requirement_ids) - requirements.keys():
                raise ValueError("section refers to unknown requirements")
            covered.update(section.requirement_ids)
        if covered != requirements.keys():
            raise ValueError("every requirement must be covered by a design section")
        remaining = {key: set(r.dependency_ids) for key, r in requirements.items()}
        if any(deps - requirements.keys() for deps in remaining.values()):
            raise ValueError("unknown requirement dependency")
        while remaining:
            ready = {key for key, deps in remaining.items() if not deps}
            if not ready:
                raise ValueError("requirement dependency cycle")
            remaining = {key: deps - ready for key, deps in remaining.items() if key not in ready}
        for values, known in (
            (self.changed_requirement_ids, requirements),
            (self.changed_section_ids, SECTION_TITLES),
        ):
            require_unique(values, "changed IDs")
            if set(values) - known.keys():
                raise ValueError("unknown changed ID")
        require_unique(self.feedback_issue_ids, "feedback issue IDs")
        return self
