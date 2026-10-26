"""Offline QA boundary tests use the real public task and recorded actual engine evidence."""

import asyncio
import copy
from datetime import timedelta
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from adaptive_game_generation.models import IssueRecord, ResourceLimits, RoleResult
from adaptive_game_generation.providers import GatewayConfig
from adaptive_game_generation.roles.qa_reviewer import QAConfig, QAProposal, QAReport, QAReviewer
from adaptive_game_generation.roles.qa_reviewer.evidence import EvidenceReader
from adaptive_game_generation.roles.qa_reviewer.prompt import build_prompt
from adaptive_game_generation.roles.qa_reviewer.role import evaluate
from adaptive_game_generation.runtime import Executor, InvalidSubmission, RuntimeConfig, Services, TextReply, Workspace
from adaptive_game_generation.runtime.commit import digest_path

ROOT = Path(__file__).resolve().parents[1]
DEMO = runpy.run_path(str(ROOT / "examples/qa_demo.py"))


def conservative_proposal(inputs):
    """Protocol fixture only; never substituted for the paid model in the live demo."""
    return {"summary": "Offline protocol fixture requesting additional acceptance evidence.",
        "instruction_audit": "The original instruction remains authoritative; no original requirement is waived.",
        "criteria": [{"requirement_id": r["requirement_id"], "criterion_index": i, "verdict": "insufficient",
            "basis": "runtime", "rationale": "This offline fixture does not make semantic acceptance claims.", "evidence_ids": [], "source_ids": []}
            for r in inputs["requirements"] for i, _ in enumerate(r["acceptance_criteria"])],
        "issues": [{"issue": {"issue_id": "QA-acceptance-coverage", "issue_type": "evidence_gap", "severity": "major",
            "description": "Collect targeted acceptance evidence for the remaining requirements.", "suggested_role": "player",
            "requirement_ids": [r["requirement_id"] for r in inputs["requirements"]], "evidence_ids": [],
            "reproduction_steps": ["Start a fresh ordinary title session, click the observed Play and Level 1 button rectangles, and record the reached gameplay state."],
            "status": "open", "resolution": None}, "source_ids": [], "instruction_excerpt": ""}],
        "ready": False, "readiness_rationale": "Acceptance coverage is incomplete.", "limitations": ["This is an offline protocol test."]}


class OfflineText:
    def __init__(self, transform=None):
        self.prompts, self.transform = [], transform

    def input_token_upper_bound(self, prompt):
        return len(prompt.encode()) + 1024

    async def generate(self, prompt, **kwargs):
        parsed = json.loads(prompt)
        self.prompts.append(parsed)
        proposal = conservative_proposal(parsed["input"])
        if self.transform:
            proposal = self.transform(proposal, len(self.prompts))
        return TextReply(json.dumps(proposal), 1000, 1000)


async def setup_executor(path, provider=None, controller="fixed_loop"):
    recorded = DEMO["HELPERS"]["load_recorded_inputs"]()
    source, source_path, *_ = recorded[0]
    workspace, task = Workspace.prepare(path, source_path.parent, task_id=source["task_id"], benchmark_snapshot=source["snapshot"])
    limits = ResourceLimits(max_model_calls=6, max_input_tokens=10000000, max_output_tokens=200000, max_image_calls=0,
        max_tool_calls=6000, max_tool_seconds=1200.0, max_wall_seconds=1800.0)
    executor = Executor.create(workspace, task, run_id="qa-test", controller=controller, budget=limits,
        config=RuntimeConfig(role_limits=limits, invocation_timeout_seconds=900, service_configuration_id="offline-qa"),
        services=Services(text=provider or OfflineText()))
    await DEMO["seed_recorded_inputs"](executor, recorded)
    return executor, workspace


def request(executor, identity="qa-first"):
    return executor.request("qa_reviewer", "Review the public task using actual evidence", ("Publish coverage, issues and readiness",), request_id=identity)


@pytest.fixture(scope="module")
def inspected(tmp_path_factory):
    async def prepare():
        executor, workspace = await setup_executor(tmp_path_factory.mktemp("qa-inspected") / "workspace")
        captured = {}
        async def inspect(context):
            reader = EvidenceReader(context, QAConfig(), context.state.game_revisions[-1])
            captured.update(inputs=await reader.collect(), reader=reader, state=context.state)
            return RoleResult(role="qa_reviewer", request_id=context.request.request_id, state_revision=context.request.state_revision,
                              status="no_op", summary="Offline integrity inspection completed")
        result = await executor.execute(request(executor), inspect)
        assert result.status == "no_op", result
        return SimpleNamespace(**captured)
    return asyncio.run(prepare())


def test_real_recorded_evidence_integrity(inspected):
    facts = inspected.reader.integrity
    assert (facts["sessions"], facts["frames"], facts["screenshots"]) == (10, 5406, 47)
    assert (facts["replay_matches"], facts["replay_mismatches"]) == (4, 2)
    assert not facts["screenshots_visually_inspected"]
    assert inspected.reader.gates == {"current_build_passed": True, "current_play_evidence": True, "packaged_traces_replayed": False, "final_art": False}
    assert len(inspected.inputs["requirements"]) == 37
    assert len(conservative_proposal(inspected.inputs)["criteria"]) == 116


def test_ready_recommendation_is_vetoed_and_specifications_preserved(inspected):
    raw = conservative_proposal(inspected.inputs)
    raw["ready"] = True
    requirements, issues, readiness, gates, _ = evaluate(QAProposal.model_validate(raw), inspected.state, inspected.reader)
    assert not readiness.ready and readiness.blocking_issue_ids == ("QA-acceptance-coverage",)
    assert not gates["all_requirements_verified"]
    for before, after in zip(inspected.state.requirements, requirements):
        assert before.model_dump(exclude={"status", "verification"}) == after.model_dump(exclude={"status", "verification"})


@pytest.mark.parametrize("fault,expected", [
    ("missing", "every acceptance"), ("stale", "stale evidence"), ("unknown_source", "unknown inspected"),
    ("coverage", "does not cover"), ("build_runtime", "corresponding inspected"), ("visual", "Text-only"),
    ("new_closed", "already closed"), ("no_gap", "actionable open"), ("wrong_excerpt", "not verbatim"),
])
def test_rejects_unfounded_acceptance(inspected, fault, expected):
    raw = conservative_proposal(inspected.inputs)
    criterion = raw["criteria"][0]
    if fault == "missing":
        raw["criteria"].pop()
    elif fault == "stale":
        criterion["evidence_ids"] = ["old-game-evidence"]
    elif fault == "unknown_source":
        criterion["source_ids"] = ["imaginary-screenshot"]
    elif fault == "coverage":
        criterion["evidence_ids"] = ["player-0001-play-01"]
    elif fault in ("build_runtime", "visual"):
        criterion.update(verdict="pass", basis="visual" if fault == "visual" else "runtime",
            evidence_ids=["build-0001"], source_ids=["build:build-0001"])
    elif fault == "new_closed":
        raw["issues"][0]["issue"].update(status="closed", resolution="Invented resolution", evidence_ids=["build-0001"])
    elif fault == "no_gap":
        raw["issues"] = []
    else:
        raw["issues"][0]["instruction_excerpt"] = "Invented instruction"
    with pytest.raises(InvalidSubmission, match=expected):
        evaluate(QAProposal.model_validate(raw), inspected.state, inspected.reader)


def test_measured_replay_cannot_be_claimed_pass(inspected):
    raw = conservative_proposal(inspected.inputs)
    criterion = next(c for c in raw["criteria"] if c["requirement_id"] == "R031")
    criterion.update(verdict="pass", basis="replay", evidence_ids=["player-0001-replay-00"],
        source_ids=["comparison:title_nav_probe-replay-1"])
    with pytest.raises(InvalidSubmission, match="contradicts"):
        evaluate(QAProposal.model_validate(raw), inspected.state, inspected.reader)


def test_prompt_hides_controller_and_ledger(inspected):
    prompt = json.loads(build_prompt(inspected.inputs, QAConfig(), []))
    assert "controller" not in prompt["input"] and "ledger" not in prompt["input"]
    assert prompt["input"]["task"]["instruction"].encode() == (ROOT / "docs/examples/player_review/instruction.md").read_bytes()
    assert "rubric" not in prompt["input"]


def test_executor_publishes_only_qa_reports_and_reuses_issue_ids(tmp_path):
    async def run():
        provider = OfflineText()
        executor, workspace = await setup_executor(tmp_path / "workspace", provider)
        before = executor.state
        role = QAReviewer(QAConfig(max_model_turns=1))
        first = await executor.execute(request(executor), role)
        assert first.status == "completed", first
        second = await executor.execute(request(executor, "qa-second"), role)
        assert second.status == "completed", second
        assert len(executor.state.issues) == 1 and not executor.state.readiness.ready
        assert all(r.kind == "report" and r.created_by == "qa_reviewer" for r in first.artifact_refs)
        assert executor.state.evidence == before.evidence and executor.state.game_revisions == before.game_revisions
        assert executor.state.assets == before.assets and executor.state.design == before.design
        for ref in second.artifact_refs:
            assert digest_path(workspace.read_public(ref.path)) == ref.sha256
        assert executor.state.ledger.usage.image_calls == 0 and len(provider.prompts) == 2
        third = await executor.execute(request(executor, "qa-config-change"), QAReviewer(QAConfig(max_model_turns=2)))
        assert third.status == "failed" and len(provider.prompts) == 2
    asyncio.run(run())


def test_invalid_protocol_repairs_are_bounded_and_atomic(tmp_path):
    async def run():
        provider = OfflineText(lambda raw, n: {**raw, "criteria": []})
        executor, _ = await setup_executor(tmp_path / "workspace", provider)
        before = executor.state
        result = await executor.execute(request(executor), QAReviewer(QAConfig(max_model_turns=2)))
        assert result.status == "failed" and len(provider.prompts) == 2
        assert not result.artifact_refs and not executor.state.issues and executor.state.readiness is None
        assert executor.state.state_revision == before.state_revision
        assert provider.prompts[1]["validation_errors"]
        assert json.loads(provider.prompts[1]["previous_response"])["criteria"] == []
    asyncio.run(run())


def test_corrupt_game_rejected_before_model_call(tmp_path):
    async def run():
        provider = OfflineText()
        executor, workspace = await setup_executor(tmp_path / "workspace", provider)
        project = workspace.read_public(executor.state.game_revisions[-1].project_ref.path)
        (project / "project.godot").write_bytes(b"corrupted test input")
        result = await executor.execute(request(executor), QAReviewer())
        assert result.status == "failed" and not provider.prompts and not executor.state.issues
    asyncio.run(run())


def test_qa_gateway_prefix_and_secret_exclusion(tmp_path):
    env = tmp_path / ".env"
    env.write_text("QA_MODEL=example-model\nMODEL_GATEWAY_API_KEY=fixture-secret\nMODEL_GATEWAY_BASE_URL=https://example.invalid/v1\nQA_MAX_OUTPUT_TOKENS=24000\n", encoding="utf-8")
    config = GatewayConfig.from_env(env, prefix="QA", environ={})
    assert config.max_output_tokens == 24000 and config.model == "example-model"
    assert "fixture-secret" not in json.dumps(config.public_settings())


@pytest.mark.parametrize("malformed_patch", [False, True])
def test_scoped_grounding_repair_keeps_review_publishable(tmp_path, malformed_patch):
    class ScopedText(OfflineText):
        async def generate(self, prompt, **kwargs):
            data = json.loads(prompt)
            self.prompts.append(data)
            if len(self.prompts) == 1:
                proposal = conservative_proposal(data["input"])
                proposal["criteria"][0].update(verdict="pass", evidence_ids=["unknown-evidence"], source_ids=["unknown-source"])
                return TextReply(json.dumps(proposal), 1000, 2000)
            assert data["protocol"] == "qa-grounding-patch.v1"
            assert len(data["criteria"]) == 1 and "input" not in data and not kwargs.get("images")
            assert kwargs["max_output_tokens"] == 8192
            row = {**data["criteria"][0], "verdict": "insufficient", "evidence_ids": [], "source_ids": []}
            return TextReply("not JSON" if malformed_patch else json.dumps({"criteria": [row]}), 300, 200)
    async def run():
        text = ScopedText()
        executor, workspace = await setup_executor(tmp_path / "workspace", text)
        result = await executor.execute(request(executor), QAReviewer(QAConfig(max_grounding_repairs=1)))
        assert result.status == "completed", result
        assert len(text.prompts) == 2 and not executor.state.readiness.ready
        ref = next(r for r in result.artifact_refs if r.artifact_id == "qa-report")
        report = QAReport.model_validate_json(workspace.read_public(ref.path).read_bytes())
        assert len(report.effective_proposal.criteria) == len(report.proposal.criteria)
        assert report.effective_proposal.criteria[0].verdict == "insufficient"
        assert bool(report.grounding_notes) == malformed_patch
    asyncio.run(run())


def test_successful_static_verification_has_current_provenance(inspected):
    raw = conservative_proposal(inspected.inputs)
    # Synthetic semantic decisions here test bookkeeping, not actual game acceptance.
    for c in raw["criteria"]:
        if c["requirement_id"] == "R001":
            c.update(verdict="pass", basis="static", evidence_ids=["build-0001"], source_ids=["project-inventory"])
    requirements, *_ = evaluate(QAProposal.model_validate(raw), inspected.state, inspected.reader)
    r = next(r for r in requirements if r.requirement_id == "R001")
    assert r.status == "verified_pass" and r.verification.game_revision_id == inspected.reader.game.revision_id
    assert r.verification.evidence_ids == ("build-0001",)


@pytest.mark.parametrize("fault", [None, "same", "failed", "before_review", "build"])
def test_issue_closure_requires_new_successful_current_runtime_evidence(inspected, fault):
    raw = conservative_proposal(inspected.inputs)
    original = inspected.reader.evidence["player-0001-play-04"]
    old = IssueRecord(issue_id="QA-runtime", issue_type="runtime", severity="major", description="Offline runtime defect fixture",
        suggested_role="developer", requirement_ids=("R001",), evidence_ids=(original.evidence_id,), reproduction_steps=("Replay the recorded trace",))
    fresh = original.revised(evidence_id="player-new-evidence", outcome="failed" if fault == "failed" else "passed",
        recorded_at=original.recorded_at + timedelta(days=1))
    if fault == "build":
        fresh = fresh.revised(kind="build_check")
    selected = original.evidence_id if fault == "same" else fresh.evidence_id
    state = inspected.state.revised(evidence=(*inspected.state.evidence, fresh), issues=(old,))
    reader = copy.copy(inspected.reader)
    reader.evidence = {**reader.evidence, fresh.evidence_id: fresh}
    reader.sources = copy.deepcopy(reader.sources)
    source = "snapshot:near_rewind_probe-capture:180"
    reader.sources[source]["evidence_ids"].append(fresh.evidence_id)
    reader.previous_review_time = fresh.recorded_at + timedelta(seconds=1) if fault == "before_review" else original.recorded_at
    for c in raw["criteria"]:
        if c["requirement_id"] == "R001":
            c.update(verdict="pass", basis="runtime", evidence_ids=[selected], source_ids=[source])
    raw["issues"].append({"issue": old.revised(status="closed", resolution="Verified with new successful evidence",
        evidence_ids=(selected,)).model_dump(mode="json"), "source_ids": [source]})
    if fault:
        with pytest.raises(InvalidSubmission):
            evaluate(QAProposal.model_validate(raw), state, reader)
    else:
        _, issues, *_ = evaluate(QAProposal.model_validate(raw), state, reader)
        assert next(i for i in issues if i.issue_id == old.issue_id).status == "closed"


@pytest.mark.parametrize("fault", [None, "insufficient", "unknown_scope", "stale", "gap", "wrong_source"])
def test_scoped_defect_closes_without_certifying_unrelated_requirement(inspected, fault):
    raw = conservative_proposal(inspected.inputs)
    original = inspected.reader.evidence["player-0001-play-04"]
    old = IssueRecord(issue_id="QA-focused-defect", issue_type="evidence_gap" if fault == "gap" else "runtime",
        severity="major", description="Offline concrete defect associated with two requirements",
        suggested_role="developer", requirement_ids=("R001", "R002"), evidence_ids=(original.evidence_id,),
        reproduction_steps=("Replay the affected checkpoint",))
    fresh = original.revised(evidence_id="fresh-scoped-proof", recorded_at=original.recorded_at + timedelta(days=1),
                             requirement_ids=("R001",), outcome="passed")
    state = inspected.state.revised(evidence=(*inspected.state.evidence, fresh), issues=(old,))
    reader = copy.copy(inspected.reader)
    reader.evidence = {**reader.evidence, fresh.evidence_id: fresh}
    reader.sources = copy.deepcopy(reader.sources)
    source = "snapshot:near_rewind_probe-capture:180"
    reader.sources[source]["evidence_ids"].append(fresh.evidence_id)
    reader.previous_review_time = fresh.recorded_at if fault == "stale" else original.recorded_at
    criterion = next(c for c in raw["criteria"] if c["requirement_id"] == "R001")
    criterion.update(verdict="insufficient" if fault == "insufficient" else "pass", basis="runtime",
                     evidence_ids=[fresh.evidence_id], source_ids=[source])
    raw["issues"].append({"issue": old.revised(status="closed", resolution="Fresh checkpoint proves the specific defect fixed; unrelated tests remain pending",
        evidence_ids=(fresh.evidence_id,)).model_dump(mode="json"),
        "source_ids": ["project-inventory" if fault == "wrong_source" else source],
        "resolved_criteria": [{"requirement_id": "R001", "criterion_index": 999 if fault == "unknown_scope" else criterion["criterion_index"]}]})
    proposal = QAProposal.model_validate(raw)
    if fault:
        with pytest.raises(InvalidSubmission):
            evaluate(proposal, state, reader)
    else:
        requirements, issues, readiness, gates, _ = evaluate(proposal, state, reader)
        assert next(i for i in issues if i.issue_id == old.issue_id).status == "closed"
        assert next(r for r in requirements if r.requirement_id == "R002").status != "verified_pass"
        assert not readiness.ready and not gates["all_requirements_verified"]
        from adaptive_game_generation.roles.qa_reviewer.grounding import ground_proposal
        effective, notes = ground_proposal(proposal, state, reader)
        assert not notes and next(i for i in effective.issues if i.issue.issue_id == old.issue_id).issue.status == "closed"
        import os
        if root := os.environ.get("PIPELINE_REVIEW_ROOT"):
            folder = Path(root) / "qa-scoped-closure"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "proposal.json").write_text(proposal.model_dump_json(indent=2), encoding="utf-8")
            (folder / "readiness.json").write_text(readiness.model_dump_json(indent=2), encoding="utf-8")
            (folder / "validation_input.json").write_text(json.dumps({
                "mode": "Offline scoped-closure protocol fixture; no live acceptance claim",
                "state": state.model_dump(mode="json"),
                "current_evidence": {k: e.model_dump(mode="json") for k, e in reader.evidence.items()},
                "inspected_sources": reader.sources,
                "previous_review_time": reader.previous_review_time.isoformat(),
                "gates": reader.gates}, indent=2), encoding="utf-8")


@pytest.mark.parametrize("fault", ["events", "journal", "checkpoint", "isolation", "comparison"])
def test_corrupt_recordings_and_false_replay_verdicts_are_rejected(inspected, fault):
    from adaptive_game_generation.roles.player import PlayerReport
    from adaptive_game_generation.roles.player.role import strict_json
    report_ref = next(r for r in inspected.state.artifacts if r.artifact_id == "player-report")
    player = PlayerReport.model_validate_json(inspected.reader.cache[report_ref.key])
    session = next(s for s in player.sessions if s.mode == "capture")
    if fault == "comparison":
        raw = conservative_proposal(inspected.inputs)
        c = next(c for c in raw["criteria"] if c["requirement_id"] == "R031")
        c.update(verdict="pass", basis="replay", source_ids=["comparison:title_nav_probe-replay-1"], evidence_ids=["player-0001-replay-00"])
        with pytest.raises(InvalidSubmission, match="contradicts"):
            evaluate(QAProposal.model_validate(raw), inspected.state, inspected.reader)
        return
    reader = EvidenceReader(SimpleNamespace(state=inspected.state), QAConfig(), inspected.reader.game)
    reader.cache = dict(inspected.reader.cache)
    log = strict_json(reader.cache[session.log_ref.key])
    refs = {Path(r["path"]).name: (r["artifact_id"], r["version"]) for r in log["runtime_files"]}
    if fault == "events":
        reader.cache[refs["events.jsonl"]] = b""
    elif fault == "journal":
        rows = reader.cache[refs["frames.jsonl"]].splitlines()
        reader.cache[refs["frames.jsonl"]] = b"\n".join(rows[:-1])
    elif fault == "checkpoint":
        reader.cache[refs["frame_0000.json"]] += b" "
    else:
        driver = json.loads(reader.cache[refs["driver.json"]])
        driver["fresh_user_data"] = False
        reader.cache[refs["driver.json"]] = json.dumps(driver).encode()
        log["driver"] = driver
        reader.cache[session.log_ref.key] = json.dumps(log).encode()
    # Cache injection bypasses the outer checksum deliberately to exercise deeper consistency checks.
    with pytest.raises(InvalidSubmission):
        asyncio.run(reader.session(session, player))


def test_semantic_validation_reports_all_bad_criterion_citations(inspected):
    raw = conservative_proposal(inspected.inputs)
    for c in raw["criteria"][:2]:
        c["evidence_ids"] = ["unknown-evidence"]
    with pytest.raises(InvalidSubmission) as result:
        evaluate(QAProposal.model_validate(raw), inspected.state, inspected.reader)
    assert "R001:0" in str(result.value) and "R001:1" in str(result.value)


def test_host_abstains_without_rewriting_the_model_proposal(inspected):
    from adaptive_game_generation.roles.qa_reviewer.grounding import ground_proposal
    raw = conservative_proposal(inspected.inputs)
    raw["ready"] = True
    raw["criteria"][0].update(verdict="pass", evidence_ids=["player-0001-play-01"], source_ids=["snapshot:title_nav_probe-capture:0"])
    proposal = QAProposal.model_validate(raw)
    effective, notes = ground_proposal(proposal, inspected.state, inspected.reader)
    assert proposal.criteria[0].verdict == "pass"
    assert effective.criteria[0].verdict == "insufficient" and not effective.criteria[0].evidence_ids
    assert notes[0]["kind"] == "criterion_abstention" and not effective.ready
    _, _, readiness, *_ = evaluate(effective, inspected.state, inspected.reader)
    assert not readiness.ready


def test_host_creates_missing_followup_issues_and_preserves_rejected_closures(inspected):
    from adaptive_game_generation.roles.qa_reviewer.grounding import ground_proposal
    raw = conservative_proposal(inspected.inputs)
    old = QAProposal.model_validate(raw).issues[0].issue
    state = inspected.state.revised(issues=(old,))
    raw["issues"][0]["issue"].update(status="closed", resolution="Unsupported model closure", evidence_ids=["build-0001"])
    effective, notes = ground_proposal(QAProposal.model_validate(raw), state, inspected.reader)
    _, issues, _, _, _ = evaluate(effective, state, inspected.reader)
    assert next(i for i in issues if i.issue_id == old.issue_id).status == "open"
    assert any(n["kind"] == "issue_update_withheld" for n in notes)
    raw = conservative_proposal(inspected.inputs)
    raw["issues"] = []
    effective, _ = ground_proposal(QAProposal.model_validate(raw), inspected.state, inspected.reader)
    assert effective.issues[0].issue.issue_type == "evidence_gap"
    evaluate(effective, inspected.state, inspected.reader)


def test_failed_capture_receipt_does_not_prove_runtime_acceptance(inspected):
    reader = copy.copy(inspected.reader)
    reader.sources = copy.deepcopy(reader.sources)
    reader.sources["session:near_rewind_probe-capture"]["content"]["status"] = "not_run"
    raw = conservative_proposal(inspected.inputs)
    raw["criteria"][0].update(verdict="pass", evidence_ids=["player-0001-play-04"], source_ids=["session:near_rewind_probe-capture"])
    with pytest.raises(InvalidSubmission, match="corresponding inspected source"):
        evaluate(QAProposal.model_validate(raw), inspected.state, reader)
