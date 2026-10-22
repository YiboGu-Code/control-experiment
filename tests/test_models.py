from __future__ import annotations

import json
import runpy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from adaptive_game_generation.models import (
    ArtifactRef, Blocker, ErrorInfo, GameRevision, IssueRecord, LedgerEvent,
    ProposedUpdates, ReadinessReview, ResourceLimits, ResourceUsage, RoleCallFinished,
    RoleCallStarted, RoleRequest, RoleResult, RunEnded, RunLedger, SchedulerDecision,
    SharedState, StateCommitted, TaskSpec,
)
from adaptive_game_generation.schema import export_schemas


@pytest.fixture
def example():
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples/contracts_demo.py"))
    return module["build_example"]()


def event(sequence, payload, when=None):
    return LedgerEvent(
        event_id=f"event-{sequence}", sequence=sequence,
        recorded_at=when or datetime(2026, 10, 19, tzinfo=timezone.utc), payload=payload,
    )


def reviewed_state(example):
    state, request, result = example
    ledger = state.ledger.append(event(2, RoleCallStarted(request=request)))
    ledger = ledger.append(event(3, RoleCallFinished(
        result=result, usage=ResourceUsage(model_calls=1, input_tokens=100, output_tokens=20),
    )))
    ledger = ledger.append(event(4, StateCommitted(
        previous_revision=1, state_revision=2, request_id=request.request_id, summary="Apply acceptance result",
    )))
    return state.revised(
        state_revision=2, ledger=ledger, requirements=result.proposed_updates.requirements,
    )


def test_full_snapshot_round_trip_preserves_unicode_typed_records_and_history(example, tmp_path):
    state = reviewed_state(example)
    path = tmp_path / "state.json"
    path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    restored = SharedState.model_validate_json(path.read_text(encoding="utf-8"))
    assert restored == state
    assert restored.requirements[0].verification.game_revision_id == "G1"
    assert isinstance(restored.ledger.events[2].payload, RoleCallFinished)
    assert restored.ledger.usage.total_tokens == 120
    assert "—" in path.read_text(encoding="utf-8")  # Original English instruction uses Unicode punctuation.


def test_contracts_round_trip_and_result_does_not_apply_itself(example):
    state, request, result = example
    assert RoleRequest.model_validate_json(request.model_dump_json()) == request
    assert RoleResult.model_validate_json(result.model_dump_json()) == result
    assert state.requirements[0].status == "implemented_unverified"
    assert result.proposed_updates.requirements[0].status == "verified_pass"


def test_initial_state_allows_designer_bootstrap_without_work_items():
    state = SharedState(
        task=TaskSpec(task_id="task", instruction="Build a game"),
        ledger=RunLedger(run_id="run", task_id="task", controller="fixed_loop", budget=ResourceLimits()),
    )
    request = RoleRequest(
        request_id="first", run_id="run", task_id="task", role="designer",
        objective="Decompose instruction", completion_criteria=("Traceable requirements",),
        state_revision=0, limits=ResourceLimits(),
    )
    state.validate_request(request)


def test_frozen_state_and_validated_revisions_preserve_original(example):
    state, _, _ = example
    with pytest.raises(ValidationError):
        state.task.instruction = "rewrite"
    with pytest.raises(ValidationError):
        state.revised(state_revision=2)
    assert state.state_revision == 1
    assert isinstance(state.artifacts, tuple)


@pytest.mark.parametrize("path", [
    "/workspace/game", "C:/game", "../game", "game/../secret", "game//a", "game/./a",
    "game\\a", "https://example.org/a", "game/", "game/evil\x00", "game/name.",
])
def test_artifact_paths_are_logical_and_portable(path):
    with pytest.raises(ValidationError):
        ArtifactRef(artifact_id="artifact", kind="log", path=path, created_by="system")


def test_valid_portable_artifact_path_and_digest():
    ref = ArtifactRef(
        artifact_id="trace", version=2, kind="input_trace", path="runs/demo/操作.json",
        created_by="player", sha256="a" * 64,
    )
    assert ref.key == ("trace", 2)


@pytest.mark.parametrize("changes", [
    {"schema_version": "2.0"}, {"hidden_rubric": {}}, {"state_revision": -1},
    {"state_revision": True}, {"state_revision": "1"},
])
def test_unknown_versions_fields_and_bad_revision_values_are_rejected(example, changes):
    state, _, _ = example
    with pytest.raises(ValidationError):
        state.revised(**changes)


def test_update_proposals_cannot_replace_task_or_budget():
    with pytest.raises(ValidationError):
        ProposedUpdates(task={"instruction": "weaker task"})
    with pytest.raises(ValidationError):
        ProposedUpdates(budget={"max_model_calls": 999})


def test_requirement_grounding_and_dependencies(example):
    state, _, _ = example
    requirement = state.requirements[0]
    with pytest.raises(ValidationError, match="not grounded"):
        state.revised(requirements=(requirement.revised(source_excerpt="not in the instruction"),))
    with pytest.raises(ValidationError, match="unknown requirement dependencies"):
        state.revised(requirements=(requirement.revised(dependency_ids=("unknown",)),))
    second = requirement.revised(requirement_id="R2", dependency_ids=("R1",))
    with pytest.raises(ValidationError, match="dependency cycle"):
        state.revised(requirements=(requirement.revised(dependency_ids=("R2",)), second))


def test_ambiguous_and_duplicate_ids_rejected(example):
    state, _, _ = example
    with pytest.raises(ValidationError, match="duplicate requirement_id"):
        state.revised(requirements=state.requirements * 2)
    issue = IssueRecord(
        issue_id="R1", issue_type="gameplay", severity="major", description="Problem",
        suggested_role="developer",
    )
    with pytest.raises(ValidationError, match="must not overlap"):
        state.revised(issues=(issue,))


def test_unregistered_or_inconsistent_artifact_references_fail(example):
    state, _, _ = example
    with pytest.raises(ValidationError, match="artifact reference"):
        state.revised(artifacts=state.artifacts[1:])
    changed_ref = state.design.document_ref.revised(path="different/design.md")
    with pytest.raises(ValidationError, match="artifact reference"):
        state.revised(design=state.design.revised(document_ref=changed_ref))
    with pytest.raises(ValidationError, match="artifact paths"):
        state.revised(artifacts=(*state.artifacts, state.artifacts[0].revised(version=2)))


def test_verification_requires_covering_current_version_evidence(example):
    state = reviewed_state(example)
    verification = state.requirements[0].verification
    with pytest.raises(ValidationError, match="verified status requires"):
        state.requirements[0].revised(verification=None)
    with pytest.raises(ValidationError, match="does not cover"):
        state.revised(requirements=(state.requirements[0].revised(
            verification=verification.revised(evidence_ids=("E-build",)),
        ),))
    with pytest.raises(ValidationError, match="unknown evidence IDs"):
        state.revised(requirements=(state.requirements[0].revised(
            verification=verification.revised(evidence_ids=("missing",)),
        ),))


def test_new_game_invalidates_old_verdict_without_deleting_evidence(example):
    state = reviewed_state(example)
    new_ref = state.game_revisions[0].project_ref.revised(version=2, path="game/v2")
    new_game = GameRevision(
        revision_id="G2", parent_revision_id="G1", project_ref=new_ref, change_summary="Change jumping",
    )
    changes = dict(
        artifacts=(*state.artifacts, new_ref), game_revisions=(*state.game_revisions, new_game),
        current_game_revision_id="G2",
    )
    with pytest.raises(ValidationError, match="stale verification"):
        state.revised(**changes)
    updated = state.revised(**changes, requirements=(state.requirements[0].revised(
        status="implemented_unverified", verification=None,
    ),))
    assert len(updated.evidence) == len(state.evidence)
    assert updated.requirements[0].verification is None


def test_game_history_and_build_evidence_integrity(example):
    state, _, _ = example
    game = state.game_revisions[0]
    with pytest.raises(ValidationError, match="parent must precede"):
        state.revised(game_revisions=(game.revised(parent_revision_id="missing"),))
    with pytest.raises(ValidationError, match="build evidence kind/outcome"):
        state.revised(game_revisions=(game.revised(build_evidence_ids=("E-jump",)),))
    with pytest.raises(ValidationError, match="current game revision does not exist"):
        state.revised(current_game_revision_id="missing")


def test_replay_requires_trace_and_evidence_requires_known_game(example):
    state, _, _ = example
    replay = state.evidence[1]
    with pytest.raises(ValidationError, match="requires an input trace"):
        replay.revised(artifact_refs=(replay.artifact_refs[1],))
    with pytest.raises(ValidationError, match="unknown game revision"):
        state.revised(evidence=(state.evidence[0], replay.revised(game_revision_id="missing")))


def test_readiness_and_issue_resolution_require_evidence(example):
    state, _, _ = example
    with pytest.raises(ValidationError):
        ReadinessReview(game_revision_id="G1", ready=True, rationale="Looks fine")
    with pytest.raises(ValidationError, match="closing an issue"):
        IssueRecord(
            issue_id="I1", issue_type="gameplay", severity="major", description="Broken",
            suggested_role="developer", status="closed",
        )
    with pytest.raises(ValidationError, match="stale readiness"):
        state.revised(readiness=ReadinessReview(
            game_revision_id="old", ready=False, rationale="Needs review",
        ))


def test_requests_cannot_use_stale_state_unknown_work_or_other_run(example):
    state, request, _ = example
    for changes in ({"state_revision": 0}, {"run_id": "other"}, {"task_id": "other"}, {"work_item_ids": ("missing",)}):
        with pytest.raises(ValueError):
            state.validate_request(request.revised(**changes))


def test_results_match_request_and_encode_blockers_errors_and_no_op(example):
    _, request, result = example
    for changes in ({"state_revision": 0}, {"role": "developer"}, {"request_id": "other"}):
        with pytest.raises(ValueError, match="does not match"):
            result.revised(**changes).validate_for(request)
    with pytest.raises(ValidationError, match="requires blockers"):
        result.revised(status="blocked")
    assert result.revised(status="blocked", blockers=(Blocker(description="Need replay"),)).blockers
    with pytest.raises(ValidationError, match="requires an error"):
        result.revised(status="failed")
    with pytest.raises(ValidationError, match="no_op cannot produce"):
        result.revised(status="no_op")


@pytest.mark.parametrize("usage", [
    {"input_tokens": -1}, {"input_tokens": True}, {"input_tokens": 1.5},
    {"cached_input_tokens": 2, "input_tokens": 1},
    {"tool_seconds": float("nan")}, {"wall_seconds": float("inf")},
])
def test_invalid_accounting_values_fail(usage):
    with pytest.raises(ValidationError):
        ResourceUsage(**usage)


def test_zero_limit_differs_from_unspecified_limit():
    assert ResourceLimits(max_model_calls=0).max_model_calls == 0
    assert ResourceLimits().max_model_calls is None


def test_scheduler_and_failed_worker_costs_count_once_and_preserve_unknown_usage(example):
    state, request, result = example
    ledger = state.ledger.revised(controller="adaptive_scheduler")
    ledger = ledger.append(event(2, SchedulerDecision(
        decision_id="decision-1", state_revision=1, selected_role="qa_reviewer",
        rationale="Review available evidence", usage=ResourceUsage(
            model_calls=1, input_tokens=30, cached_input_tokens=20, output_tokens=10,
        ),
    )))
    ledger = ledger.append(event(3, RoleCallStarted(request=request)))
    ledger = ledger.append(event(4, RoleCallFinished(
        result=result.revised(status="failed", error=ErrorInfo(code="provider_error", message="Failed")),
        usage=ResourceUsage(model_calls=1, input_tokens=50, output_tokens=5, token_usage_complete=False),
    )))
    assert ledger.usage.model_calls == 2
    assert ledger.usage.total_tokens == 95  # Cached input is already included.
    assert ledger.usage.cached_input_tokens == 20
    assert not ledger.usage.token_usage_complete
    assert RunLedger.model_validate_json(ledger.model_dump_json()).usage == ledger.usage
    with pytest.raises(ValidationError, match="exactly once"):
        ledger.append(event(5, ledger.events[-1].payload))


def test_ledger_rejects_reordering_parallel_calls_and_stale_commits(example):
    state, request, result = example
    ledger = state.ledger
    with pytest.raises(ValidationError, match="contiguous"):
        ledger.append(event(3, RoleCallStarted(request=request)))
    with pytest.raises(ValidationError, match="chronological"):
        ledger.append(event(2, RoleCallStarted(request=request), ledger.started_at - timedelta(seconds=1)))
    with pytest.raises(ValidationError, match="stale state revision"):
        ledger.append(event(2, RoleCallStarted(request=request.revised(state_revision=0))))
    active = ledger.append(event(2, RoleCallStarted(request=request)))
    with pytest.raises(ValidationError, match="only one active"):
        active.append(event(3, RoleCallStarted(request=request.revised(request_id="second"))))
    finished = active.append(event(3, RoleCallFinished(result=result, usage=ResourceUsage())))
    committed = finished.append(event(4, StateCommitted(
        previous_revision=1, state_revision=2, request_id=request.request_id, summary="Commit",
    )))
    with pytest.raises(ValidationError, match="uncommitted finished"):
        committed.append(event(5, StateCommitted(
            previous_revision=2, state_revision=3, request_id=request.request_id, summary="Duplicate commit",
        )))


def test_terminal_history_is_closed_and_accounts_for_active_calls(example):
    state, request, result = example
    active = state.ledger.append(event(2, RoleCallStarted(request=request)))
    end = RunEnded(reason="budget_exhausted", summary="Limit reached", final_game_revision_id="G1")
    with pytest.raises(ValidationError, match="finish active calls"):
        active.append(event(3, end))
    finished = active.append(event(3, RoleCallFinished(result=result, usage=ResourceUsage())))
    ended = finished.append(event(4, end))
    ended_state = state.revised(ledger=ended)
    with pytest.raises(ValueError, match="after the run has ended"):
        ended_state.validate_request(request)
    with pytest.raises(ValidationError, match="no events may follow"):
        ended.append(event(5, end))
    assert ended.termination.reason == "budget_exhausted"


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValidationError):
        event(1, RunEnded(reason="failed", summary="Failure"), datetime(2026, 10, 19))


def test_json_schema_export_includes_typed_events_and_forbids_unknown_fields(tmp_path):
    paths = export_schemas(tmp_path)
    assert len(paths) == 6
    schemas = {path.name: json.loads(path.read_text(encoding="utf-8")) for path in paths}
    state = schemas["shared_state.schema.json"]
    assert state["additionalProperties"] is False
    assert state["properties"]["schema_version"]["const"] == "1.0"
    event_schema = schemas["ledger_event.schema.json"]
    assert event_schema["properties"]["payload"]["discriminator"]["propertyName"] == "kind"
