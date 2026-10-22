"""Illustrate contracts for one movement requirement from the real Time Loop task.

Evidence is synthetic; this is not a playable game or a live model call.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from adaptive_game_generation.models import (
    ArtifactRef, AssetRecord, CheckStatus, ControllerKind, DesignSpec,
    EvidenceRecord, GameRevision, LedgerEvent, ProposedUpdates, Requirement,
    ResourceLimits, RoleRequest, RoleResult, RunLedger, SharedState,
    StateCommitted, TaskSpec, Verification,
)

TASK_DIRECTORY = Path(__file__).resolve().parents[1] / (
    "benchmark_resources/FreedomIntelligence-gamecraft-bench-a433475/tasks/platformer-time-loop"
)


def build_example() -> tuple[SharedState, RoleRequest, RoleResult]:
    now = datetime(2026, 10, 19, tzinfo=timezone.utc)
    design_ref = ArtifactRef(
        artifact_id="design", kind="document", path="design/v1/design.md",
        created_by="designer",
    )
    sprite_ref = ArtifactRef(
        artifact_id="player-sprite", kind="asset", path="assets/v1/player.png",
        created_by="art_producer", media_type="image/png",
    )
    project_ref = ArtifactRef(
        artifact_id="game", kind="game_project", path="game/v1", created_by="developer",
    )
    log_ref = ArtifactRef(
        artifact_id="build-log", kind="log", path="evidence/v1/build.txt",
        created_by="developer",
    )
    trace_ref = ArtifactRef(
        artifact_id="jump-trace", kind="input_trace", path="game/v1/demo_outputs/jump.json",
        created_by="player", media_type="application/json",
    )
    screenshot_ref = ArtifactRef(
        artifact_id="jump-screen", kind="screenshot", path="evidence/v1/jump.png",
        created_by="player", media_type="image/png",
    )
    config_ref = ArtifactRef(
        artifact_id="run-config", kind="config", path="config/run.json", created_by="system",
    )
    requirement = Requirement(
        requirement_id="R1", description="The player can jump",
        source_excerpt="The player runs, jumps, and\ninteracts with switches and objects.",
        acceptance_criteria=("The configured jump action makes the player leave the ground and land on a platform",),
        status="implemented_unverified",
    )
    build = EvidenceRecord(
        evidence_id="E-build", kind="build_check", game_revision_id="G1",
        artifact_refs=(log_ref,), observation="Synthetic example: startup check passed", outcome="passed",
        recorded_at=now,
    )
    replay = EvidenceRecord(
        evidence_id="E-jump", kind="replay", game_revision_id="G1",
        requirement_ids=("R1",), artifact_refs=(trace_ref, screenshot_ref),
        observation="Synthetic example: replayed input makes the player jump and land", outcome="passed", recorded_at=now,
    )
    ledger = RunLedger(
        run_id="demo-run", task_id=TASK_DIRECTORY.name, controller=ControllerKind.FIXED_LOOP,
        budget=ResourceLimits(max_model_calls=20, max_output_tokens=10000),
        config_ref=config_ref, started_at=now,
        events=(LedgerEvent(
            event_id="event-1", sequence=1, recorded_at=now,
            payload=StateCommitted(previous_revision=0, state_revision=1, summary="Import illustrative snapshot"),
        ),),
    )
    state = SharedState(
        task=TaskSpec(
            task_id=TASK_DIRECTORY.name,
            instruction=(TASK_DIRECTORY / "instruction.md").read_text(encoding="utf-8"),
            benchmark_snapshot=TASK_DIRECTORY.parents[1].name,
        ),
        ledger=ledger, state_revision=1,
        artifacts=(design_ref, sprite_ref, project_ref, log_ref, trace_ref, screenshot_ref, config_ref),
        requirements=(requirement,),
        design=DesignSpec(version=1, document_ref=design_ref, change_summary="Illustrative movement specification"),
        assets=(AssetRecord(
            asset_id="player", artifact_ref=sprite_ref, purpose="Player character",
            source="Illustrative placeholder reference", license="Example only; no actual asset file", integration_notes="32x32 pixels",
        ),),
        game_revisions=(GameRevision(
            revision_id="G1", project_ref=project_ref, change_summary="Synthetic jump implementation",
            affected_requirement_ids=("R1",), build_status=CheckStatus.PASSED,
            build_evidence_ids=("E-build",), created_at=now,
        ),),
        current_game_revision_id="G1", evidence=(build, replay),
    )
    request = RoleRequest(
        request_id="qa-1", run_id="demo-run", task_id=TASK_DIRECTORY.name, role="qa_reviewer",
        work_item_ids=("R1",), objective="Review the jump requirement using the supplied playthrough evidence",
        completion_criteria=("Provide an evidence-backed acceptance decision",), state_revision=1,
        artifact_refs=(trace_ref, screenshot_ref), limits=ResourceLimits(max_model_calls=1),
    )
    result = RoleResult(
        request_id="qa-1", role="qa_reviewer", state_revision=1, status="completed",
        summary="Synthetic example: current-version evidence supports the jump requirement", evidence_refs=("E-jump",),
        proposed_updates=ProposedUpdates(requirements=(requirement.revised(
            status="verified_pass", verification=Verification(
                game_revision_id="G1", evidence_ids=("E-jump",),
                rationale="Illustrative replay and screenshot support the acceptance criteria", reviewed_at=now,
            ),
        ),)),
    )
    state.validate_request(request)
    result.validate_for(request)
    return state, request, result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, model in zip(("shared_state", "role_request", "role_result"), build_example()):
        path = args.output / f"{name}.json"
        path.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(path)


if __name__ == "__main__":
    main()
