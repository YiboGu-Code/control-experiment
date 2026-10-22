"""One permission policy for both control paradigms."""

from __future__ import annotations

from ..models import ArtifactKind, EvidenceKind, Role, RoleResult, SharedState
from .errors import PermissionDenied

_FIELDS = {
    Role.DESIGNER: {"requirements", "design"},
    Role.ART_PRODUCER: {"assets"},
    Role.DEVELOPER: {"requirements", "game_revision", "evidence"},
    Role.PLAYER: {"evidence"},
    Role.QA_REVIEWER: {"requirements", "issues", "readiness"},
}
_ARTIFACTS = {
    Role.DESIGNER: {ArtifactKind.DOCUMENT, ArtifactKind.REPORT},
    Role.ART_PRODUCER: {ArtifactKind.ASSET, ArtifactKind.DOCUMENT, ArtifactKind.REPORT},
    Role.DEVELOPER: {ArtifactKind.GAME_PROJECT, ArtifactKind.LOG, ArtifactKind.REPORT, ArtifactKind.SCREENSHOT},
    Role.PLAYER: {ArtifactKind.INPUT_TRACE, ArtifactKind.SCREENSHOT, ArtifactKind.VIDEO, ArtifactKind.LOG, ArtifactKind.REPORT},
    Role.QA_REVIEWER: {ArtifactKind.REPORT},
}


class RolePolicy:
    def allow_tool(self, role: Role | str, tool: str) -> None:
        common = {"read", "list", "model"}
        allowed = common if role == "scheduler" else common | {"write", "copy"}
        if role in (Role.DEVELOPER, Role.PLAYER):
            allowed = allowed | {"sandbox", "godot_inspect"}
        if role == Role.DEVELOPER:
            allowed = allowed | {"remove", "vision"}
        if role == Role.ART_PRODUCER:
            allowed = allowed | {"image", "library"}
        if tool not in allowed:
            raise PermissionDenied(f"{role} cannot use tool {tool}")

    def validate_result(self, state: SharedState, result: RoleResult) -> None:
        role, updates = result.role, result.proposed_updates
        for name in type(updates).model_fields:
            if getattr(updates, name) and name not in _FIELDS[role]:
                raise PermissionDenied(f"{role.value} cannot update {name}")
        for ref in result.artifact_refs:
            if ref.created_by != role or ref.kind not in _ARTIFACTS[role]:
                raise PermissionDenied("Artifact type or creator is not permitted for this role")
        old_requirements = {item.requirement_id: item for item in state.requirements}
        for item in updates.requirements:
            old = old_requirements.get(item.requirement_id)
            if role == Role.DESIGNER:
                if old is None and (item.status != "not_implemented" or item.verification is not None):
                    raise PermissionDenied("Designer cannot claim implementation or verification")
                if old is not None:
                    if item.source_excerpt != old.source_excerpt:
                        raise PermissionDenied("Original requirement grounding cannot be replaced")
                    if item.status != old.status or item.verification != old.verification:
                        raise PermissionDenied("Designer cannot change implementation or verification status")
            else:
                if old is None:
                    raise PermissionDenied("Only Designer can create requirements")
                if item.model_dump(exclude={"status", "verification"}) != old.model_dump(exclude={"status", "verification"}):
                    raise PermissionDenied("This role cannot rewrite requirement specifications")
                if role == Role.DEVELOPER and (item.verification is not None or item.status not in ("not_implemented", "implemented_unverified")):
                    raise PermissionDenied("Developer cannot approve its own implementation")
        for record in updates.evidence:
            if role == Role.DEVELOPER and record.kind != EvidenceKind.BUILD_CHECK:
                raise PermissionDenied("Developer may submit build-check evidence only")
            if role == Role.PLAYER and record.kind == EvidenceKind.BUILD_CHECK:
                raise PermissionDenied("Build checks belong to Developer")
        evidence = {item.evidence_id: item for item in state.evidence}
        old_issues = {item.issue_id: item for item in state.issues}
        for issue in updates.issues:
            if issue.status == "closed" and old_issues.get(issue.issue_id) != issue:
                if not any(
                    evidence_id in evidence and evidence[evidence_id].game_revision_id == state.current_game_revision_id
                    for evidence_id in issue.evidence_ids
                ):
                    raise PermissionDenied("Closing an issue requires evidence from the current game revision")
