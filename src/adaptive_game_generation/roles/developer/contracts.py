"""Incremental project changes; models cannot author runtime results or evidence."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from ...models import ArtifactRef, Blocker
from ...models.base import Identifier, Model, PositiveCount, Seconds, Text, require_unique
from ...runtime.workspace import logical_path
from ...runtime.godot_mcp import GodotInspectionRequest

TEXT_SUFFIXES = {".gd", ".tscn", ".tres", ".gdshader", ".json", ".md", ".txt", ".cfg", ".csv", ".uid"}


def project_path(value: str) -> str:
    logical_path(value)
    parts = PurePosixPath(value).parts
    if any(p.startswith(".") or p.casefold() in {"addons", "assets", "licenses", "demo_outputs", "tests", "solution", "solutions"} for p in parts):
        raise ValueError("Project patches cannot write assets, traces, plugins, private paths or caches")
    if value != "project.godot" and PurePosixPath(value).suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError("Unsupported project source file")
    return value


class DeveloperConfig(Model):
    prompt_version: Literal["developer.v1", "developer.v2", "developer.v3", "developer.v4"] = "developer.v1"
    max_output_tokens: PositiveCount = 16384
    max_model_turns: Annotated[int, Field(strict=True, ge=1, le=12)] = 3
    max_patch_files: Annotated[int, Field(strict=True, ge=1, le=128)] = 48
    max_file_bytes: PositiveCount = 256 * 1024
    max_source_bytes: PositiveCount = 1024 * 1024
    max_prompt_bytes: PositiveCount = 2 * 1024 * 1024
    max_response_bytes: PositiveCount = 1024 * 1024
    godot_executable: Text = "godot"
    check_timeout_seconds: Seconds = Field(default=90.0, gt=0)
    max_scenario_checks: Annotated[int, Field(strict=True, ge=0, le=5)] = 2
    scenario_frames: Annotated[int, Field(strict=True, ge=1, le=600)] = 60
    max_packaged_traces: Annotated[int, Field(strict=True, ge=1, le=10)] = 2
    max_mcp_inspections: Annotated[int, Field(strict=True, ge=1, le=5)] = 3
    mcp_timeout_seconds: Seconds = Field(default=180.0, gt=0)
    visual_review_tokens: PositiveCount = 8192


class FileWrite(Model):
    path: Text
    content: str

    @field_validator("path")
    @classmethod
    def safe_source(cls, value):
        return project_path(value)


class ScenarioSpec(Model):
    scenario_id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    description: Text


class DeveloperProposal(Model):
    status: Literal["completed", "partial", "no_op", "blocked"]
    summary: Text
    files: tuple[FileWrite, ...] = ()
    delete_paths: tuple[Text, ...] = ()
    affected_requirement_ids: tuple[Identifier, ...] = ()
    implemented_requirement_ids: tuple[Identifier, ...] = ()
    # Complete current scenario list, including retained scenarios after a patch.
    scenarios: tuple[ScenarioSpec, ...] = ()
    controls: tuple[Text, ...] = ()
    remaining_work: tuple[Text, ...] = ()
    blockers: tuple[Blocker, ...] = ()

    @model_validator(mode="after")
    def consistent(self):
        for path in self.delete_paths:
            project_path(path)
        require_unique((f.path.casefold() for f in self.files), "written paths")
        require_unique((p.casefold() for p in self.delete_paths), "deleted paths")
        if {f.path.casefold() for f in self.files} & {p.casefold() for p in self.delete_paths}:
            raise ValueError("Cannot write and delete the same source path")
        for ids in (self.affected_requirement_ids, self.implemented_requirement_ids):
            require_unique(ids, "requirement references")
        if set(self.implemented_requirement_ids) - set(self.affected_requirement_ids):
            raise ValueError("Implemented claims must be included in affected requirements")
        require_unique((s.scenario_id for s in self.scenarios), "scenario IDs")
        if self.status in ("completed", "partial"):
            if not (self.files or self.delete_paths) or not self.affected_requirement_ids or not self.controls or self.blockers:
                raise ValueError("Implementation needs a patch, affected requirements and controls, without blockers")
            if (self.status == "partial") != bool(self.remaining_work):
                raise ValueError("Only partial proposals must list remaining work")
        elif self.files or self.delete_paths or self.affected_requirement_ids or self.implemented_requirement_ids or self.scenarios or self.controls or self.remaining_work:
            raise ValueError("Blocked and no_op proposals cannot contain implementation changes")
        elif (self.status == "blocked") != bool(self.blockers):
            raise ValueError("Only blocked proposals require blockers")
        return self


class BuildStep(Model):
    name: Text
    argv: tuple[Text, ...]
    status: Literal["passed", "failed", "not_run"]
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    reason: str | None = None


class DeveloperInspect(Model):
    status: Literal["inspect"]
    summary: Text
    inspection: GodotInspectionRequest


class DeveloperFinish(Model):
    status: Literal["finish"]
    summary: Text
    remaining_work: tuple[Text, ...] = ()


class DeveloperInspectionRecord(Model):
    model_turn: PositiveCount
    project_sha256: Text
    request: GodotInspectionRequest
    result: dict
    screenshot_ref: ArtifactRef | None = None
    visual_review: dict | None = None
    artifact_refs: tuple[ArtifactRef, ...]


class BuildAttempt(Model):
    attempt: PositiveCount
    model_turn: PositiveCount = 1
    source_digest: Text
    status: Literal["passed", "failed", "not_run"]
    steps: tuple[BuildStep, ...]


class IntegratedAsset(Model):
    asset_id: Identifier
    artifact_ref: ArtifactRef
    target_path: Text
    is_mock: bool
    license_target: Text | None = None


class ProposalRejection(Model):
    model_turn: PositiveCount
    errors: tuple[Text, ...]


class PackagedTrace(Model):
    target_path: Annotated[str, StringConstraints(pattern=r"^demo_outputs/[a-zA-Z0-9_-]+\.json$")]
    source_game_revision_id: Identifier
    player_report_ref: ArtifactRef
    trace_ref: ArtifactRef
    requirement_ids: tuple[Identifier, ...] = ()


class DeveloperReport(Model):
    schema_version: Literal["developer.1"] = "developer.1"
    config: DeveloperConfig
    task_id: Identifier
    game_revision_id: Identifier
    parent_revision_id: Identifier | None
    project_ref: ArtifactRef
    design_ref: ArtifactRef
    art_manifest_ref: ArtifactRef | None
    summaries: tuple[Text, ...]
    changed_files: tuple[Text, ...]
    deleted_files: tuple[Text, ...]
    affected_requirement_ids: tuple[Identifier, ...]
    model_implementation_claims: tuple[Identifier, ...]
    integrated_assets: tuple[IntegratedAsset, ...]
    scenarios: tuple[ScenarioSpec, ...]
    controls: tuple[Text, ...]
    build_attempts: tuple[BuildAttempt, ...]
    proposal_rejections: tuple[ProposalRejection, ...] = ()
    limitations: tuple[Text, ...]
    packaged_traces: tuple[PackagedTrace, ...] = ()
    mcp_inspections: tuple[DeveloperInspectionRecord, ...] = ()
    self_review_completed: bool = False
    self_review_summary: str | None = None
