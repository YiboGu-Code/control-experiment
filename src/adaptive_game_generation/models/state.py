"""Validated snapshots shared by both controllers. No execution or persistence."""

from __future__ import annotations

from pydantic import model_validator

from .artifacts import ArtifactRef
from .base import Count, Identifier, VersionedModel, require_unique
from .contracts import RoleRequest
from .domain import (
    AssetRecord, DesignSpec, EvidenceKind, EvidenceRecord, GameRevision,
    IssueRecord, ReadinessReview, Requirement, TaskSpec,
)
from .ledger import RunLedger


class SharedState(VersionedModel):
    task: TaskSpec
    ledger: RunLedger
    state_revision: Count = 0
    artifacts: tuple[ArtifactRef, ...] = ()
    requirements: tuple[Requirement, ...] = ()
    design: DesignSpec | None = None
    assets: tuple[AssetRecord, ...] = ()
    game_revisions: tuple[GameRevision, ...] = ()
    current_game_revision_id: Identifier | None = None
    evidence: tuple[EvidenceRecord, ...] = ()
    issues: tuple[IssueRecord, ...] = ()
    readiness: ReadinessReview | None = None

    @model_validator(mode="after")
    def consistent_snapshot(self) -> SharedState:
        if self.task.task_id != self.ledger.task_id:
            raise ValueError("state and ledger must refer to the same task")
        if self.state_revision != self.ledger.state_revision:
            raise ValueError("state_revision must match the committed ledger revision")
        for records, key in (
            (self.artifacts, "key"), (self.requirements, "requirement_id"),
            (self.assets, "asset_id"), (self.game_revisions, "revision_id"),
            (self.evidence, "evidence_id"), (self.issues, "issue_id"),
        ):
            require_unique((getattr(record, key) for record in records), key)
        # A new version must not overwrite the location of historical evidence.
        require_unique((ref.path for ref in self.artifacts), "artifact paths")
        registry = {ref.key: ref for ref in self.artifacts}
        requirements = {item.requirement_id: item for item in self.requirements}
        issues = {item.issue_id: item for item in self.issues}
        games = {item.revision_id: item for item in self.game_revisions}
        evidence = {item.evidence_id: item for item in self.evidence}
        if requirements.keys() & issues.keys():
            raise ValueError("requirement and issue IDs must not overlap")

        def check_ref(ref: ArtifactRef) -> None:
            if registry.get(ref.key) != ref:
                raise ValueError(f"unregistered or inconsistent artifact reference: {ref.key}")

        def check_ids(ids: tuple[str, ...], known: dict, label: str) -> None:
            require_unique(ids, label)
            if missing := set(ids) - known.keys():
                raise ValueError(f"unknown {label}: {sorted(missing)}")

        def check_evidence(ids: tuple[str, ...], revision_id: str) -> None:
            check_ids(ids, evidence, "evidence IDs")
            for evidence_id in ids:
                if evidence[evidence_id].game_revision_id != revision_id:
                    raise ValueError(f"evidence {evidence_id} belongs to another game revision")

        for ref in self.task.resource_refs:
            check_ref(ref)
        if self.ledger.config_ref is not None:
            check_ref(self.ledger.config_ref)
        if self.design is not None:
            check_ref(self.design.document_ref)
        for asset in self.assets:
            check_ref(asset.artifact_ref)

        for requirement in self.requirements:
            if requirement.source_excerpt not in self.task.instruction:
                raise ValueError(f"requirement {requirement.requirement_id} is not grounded in instruction")
            check_ids(requirement.dependency_ids, requirements, "requirement dependencies")
        self._check_dependency_cycles(requirements)

        seen_revisions: set[str] = set()
        for game in self.game_revisions:
            check_ref(game.project_ref)
            if game.parent_revision_id is not None and game.parent_revision_id not in seen_revisions:
                raise ValueError("game parent must precede the child in revision history")
            seen_revisions.add(game.revision_id)
            check_ids(game.affected_requirement_ids, requirements, "affected requirements")
            check_evidence(game.build_evidence_ids, game.revision_id)
            for evidence_id in game.build_evidence_ids:
                check = evidence[evidence_id]
                if check.kind != EvidenceKind.BUILD_CHECK or check.outcome != game.build_status:
                    raise ValueError("build evidence kind/outcome must match the game build status")
        if self.current_game_revision_id is not None:
            if self.current_game_revision_id not in games:
                raise ValueError("current game revision does not exist")
        elif games:
            raise ValueError("game history requires a current game revision")

        for record in self.evidence:
            if record.game_revision_id not in games:
                raise ValueError("evidence refers to an unknown game revision")
            check_ids(record.requirement_ids, requirements, "evidence requirements")
            for ref in record.artifact_refs:
                check_ref(ref)

        for requirement in self.requirements:
            if requirement.verification is None:
                continue
            verification = requirement.verification
            if verification.game_revision_id != self.current_game_revision_id:
                raise ValueError("stale verification: reset requirement to implemented_unverified")
            check_evidence(verification.evidence_ids, verification.game_revision_id)
            for evidence_id in verification.evidence_ids:
                if requirement.requirement_id not in evidence[evidence_id].requirement_ids:
                    raise ValueError("verification evidence does not cover this requirement")

        for issue in self.issues:
            check_ids(issue.requirement_ids, requirements, "issue requirements")
            check_ids(issue.evidence_ids, evidence, "issue evidence IDs")
        if self.readiness is not None:
            if self.readiness.game_revision_id != self.current_game_revision_id:
                raise ValueError("stale readiness review must be cleared or refreshed")
            check_evidence(self.readiness.evidence_ids, self.readiness.game_revision_id)
            check_ids(self.readiness.blocking_issue_ids, issues, "blocking issues")
        if self.ledger.termination is not None:
            termination = self.ledger.termination
            if termination.final_game_revision_id is not None and termination.final_game_revision_id not in games:
                raise ValueError("final game revision does not exist")
            check_ids(termination.unresolved_issue_ids, issues, "unresolved issues")
        return self

    @staticmethod
    def _check_dependency_cycles(requirements: dict[str, Requirement]) -> None:
        remaining = {key: set(item.dependency_ids) for key, item in requirements.items()}
        while remaining:
            ready = {key for key, dependencies in remaining.items() if not dependencies}
            if not ready:
                raise ValueError("requirement dependency cycle")
            remaining = {
                key: dependencies - ready
                for key, dependencies in remaining.items() if key not in ready
            }

    def validate_request(self, request: RoleRequest) -> None:
        """Check snapshot references; role permissions and budgets are checked later."""
        if self.ledger.termination is not None:
            raise ValueError("cannot call a role after the run has ended")
        if (request.run_id, request.task_id, request.state_revision) != (
            self.ledger.run_id, self.task.task_id, self.state_revision
        ):
            raise ValueError("request belongs to a different run, task or state revision")
        known = {item.requirement_id for item in self.requirements} | {
            item.issue_id for item in self.issues
        }
        if set(request.work_item_ids) - known:
            raise ValueError("request refers to unknown work items")
        registry = {ref.key: ref for ref in self.artifacts}
        if any(registry.get(ref.key) != ref for ref in request.artifact_refs):
            raise ValueError("request refers to unregistered or inconsistent artifacts")
