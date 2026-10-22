"""Pure authorization and state merging plus validation of staged artifact bytes."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel

from ..models import ArtifactRef, RoleResult, SharedState
from .errors import InvalidSubmission
from .policy import RolePolicy
from .workspace import InvocationWorkspace, inspect_tree


def all_refs(value):
    if isinstance(value, ArtifactRef):
        yield value
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from all_refs(getattr(value, name))
    elif isinstance(value, tuple):
        for item in value:
            yield from all_refs(item)


def digest_path(path: Path) -> str:
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    digest = hashlib.sha256()
    # Match portable, case-insensitive artifact identity on Windows and Linux.
    # Do not rely on pathlib's platform-dependent ordering of mixed-case names.
    for child in sorted(path.rglob("*"), key=lambda p: tuple(part.casefold() for part in p.relative_to(path).parts)):
        if child.is_file():
            digest.update(child.relative_to(path).as_posix().encode("utf-8") + b"\x00")
            digest.update(bytes.fromhex(digest_path(child)))
    return digest.hexdigest()


def validate_artifacts(state: SharedState, result: RoleResult, work: InvocationWorkspace, max_bytes: int) -> None:
    inspect_tree(work.staging, max_bytes=max_bytes)
    existing = {ref.key: ref for ref in state.artifacts}
    declared = {ref.key: ref for ref in result.artifact_refs}
    allowed = {**existing, **declared}
    normalized_paths = [ref.path.casefold() for ref in allowed.values()]
    if len(normalized_paths) != len(set(normalized_paths)):
        raise InvalidSubmission("Artifact paths collide on case-insensitive filesystems")
    coverage: list[Path] = []
    for ref in result.artifact_refs:
        if ref.key in existing:
            if existing[ref.key] != ref:
                raise InvalidSubmission("An existing artifact version cannot be changed")
            continue
        if not ref.path.startswith(work.prefix + "/"):
            raise InvalidSubmission("New artifacts must belong to this invocation's output namespace")
        versions = [old.version for old in state.artifacts if old.artifact_id == ref.artifact_id]
        if versions and ref.version <= max(versions):
            raise InvalidSubmission("Artifact version must increase")
        path = work.read_path(ref.path)
        if not path.exists():
            raise InvalidSubmission("Declared artifact does not exist")
        if ref.kind in ("game_project", "resource_library") and not path.is_dir():
            raise InvalidSubmission("Project/library artifact must be a directory")
        if ref.kind not in ("game_project", "resource_library") and not path.is_file():
            raise InvalidSubmission("File artifact must be a regular file")
        if ref.sha256 is not None and digest_path(path) != ref.sha256:
            raise InvalidSubmission("Artifact digest does not match its bytes")
        coverage.append(path)
    for ref in all_refs(result.proposed_updates):
        if allowed.get(ref.key) != ref:
            raise InvalidSubmission("Update references an undeclared or inconsistent artifact")
    for path in work.staging.rglob("*"):
        if path.is_file() and not any(path == root or path.is_relative_to(root) for root in coverage):
            raise InvalidSubmission("Invocation contains undeclared output files")
    paths = [path.relative_to(work.staging).as_posix().casefold() for path in work.staging.rglob("*")]
    if len(paths) != len(set(paths)):
        raise InvalidSubmission("Output paths collide on case-insensitive filesystems")


def merge_updates(state: SharedState, result: RoleResult, policy: RolePolicy) -> dict:
    """Return candidate fields. The caller supplies finished/commit ledger events."""
    policy.validate_result(state, result)
    updates = result.proposed_updates

    def upsert(old, new, key):
        records = {getattr(item, key): item for item in old}
        records.update({getattr(item, key): item for item in new})
        return tuple(records.values())

    evidence = {item.evidence_id: item for item in state.evidence}
    for item in updates.evidence:
        if item.evidence_id in evidence and evidence[item.evidence_id] != item:
            raise InvalidSubmission("Existing evidence is immutable")
    game = updates.game_revision
    if game is not None:
        if game.revision_id in {item.revision_id for item in state.game_revisions}:
            raise InvalidSubmission("Game revisions are immutable; submit a new revision")
        if game.parent_revision_id != state.current_game_revision_id:
            raise InvalidSubmission("New game must be based on the current revision")
    if updates.design is not None and state.design is not None:
        if updates.design != state.design and updates.design.version <= state.design.version:
            raise InvalidSubmission("Design version must increase")
    old_requirements = {item.requirement_id: item for item in state.requirements}
    changed_specification = any(
        item.requirement_id not in old_requirements or
        item.model_dump(exclude={"status", "verification"}) !=
        old_requirements[item.requirement_id].model_dump(exclude={"status", "verification"})
        for item in updates.requirements
    )
    requirements = upsert(state.requirements, updates.requirements, "requirement_id")
    if game is not None or changed_specification or (updates.design is not None and updates.design != state.design):
        requirements = tuple(
            item.revised(status="implemented_unverified", verification=None)
            if item.verification is not None else item for item in requirements
        )
    artifacts = {ref.key: ref for ref in state.artifacts}
    artifacts.update({ref.key: ref for ref in result.artifact_refs})
    known_evidence = set(evidence) | {item.evidence_id for item in updates.evidence}
    if set(result.evidence_refs) - known_evidence:
        raise InvalidSubmission("Result references unknown evidence")
    return dict(
        artifacts=tuple(artifacts.values()), requirements=requirements,
        design=updates.design or state.design,
        assets=upsert(state.assets, updates.assets, "asset_id"),
        game_revisions=(*state.game_revisions, game) if game else state.game_revisions,
        current_game_revision_id=game.revision_id if game else state.current_game_revision_id,
        evidence=upsert(state.evidence, updates.evidence, "evidence_id"),
        issues=upsert(state.issues, updates.issues, "issue_id"),
        readiness=updates.readiness if not updates.is_empty else state.readiness,
    )
