"""Offline contract and failure tests; real model integration is test_designer_live."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import runpy
from pathlib import Path

import pytest

from adaptive_game_generation.models import (
    ArtifactRef, DesignSpec, EvidenceRecord, GameRevision, IssueRecord, ProposedUpdates,
    RequirementStatus, ResourceLimits, RoleResult, Verification,
)
from adaptive_game_generation.roles.designer import DesignDocument, Designer, DesignerConfig
from adaptive_game_generation.runtime import Executor, Services, TextReply, Workspace

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "examples" / "designer_case"
SOURCE = json.loads((CASE / "source.json").read_text(encoding="utf-8"))
INSTRUCTION = ROOT / SOURCE["instruction_path"]


def response():
    return json.loads((ROOT / "tests/fixtures/designer_proposal.json").read_text(encoding="utf-8"))


class ScriptedText:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def input_token_upper_bound(self, prompt):
        return len(prompt.encode("utf-8")) + 64

    async def generate(self, prompt, *, max_output_tokens, timeout):
        assert max_output_tokens == 8192
        assert 0 < timeout <= 60
        self.prompts.append(json.loads(prompt))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return TextReply(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), 2000, 3000)


def make_run(tmp_path, *responses, controller="fixed_loop", budget=None):
    source = tmp_path / "source"
    source.mkdir(parents=True)
    (source / "instruction.md").write_bytes(INSTRUCTION.read_bytes())
    for folder, file, value in (
        ("tests", "rubric.json", "HIDDEN-RUBRIC-SENTINEL"),
        ("solution", "reference.txt", "HIDDEN-SOLUTION-SENTINEL"),
    ):
        (source / folder).mkdir()
        (source / folder / file).write_text(value, encoding="utf-8")
    (source / ".env").write_text("API_KEY=SECRET-SENTINEL", encoding="utf-8")
    workspace, task = Workspace.prepare(
        tmp_path / "run", source, task_id=SOURCE["task_id"], benchmark_snapshot=SOURCE["snapshot"],
    )
    task = task.revised(environment_constraints=("Fixed Godot environment; offline assets only",))
    provider = ScriptedText(*responses)
    executor = Executor.create(
        workspace, task, run_id="run", controller=controller,
        services=Services(text=provider),
        budget=budget or ResourceLimits(max_model_calls=10, max_input_tokens=400000, max_output_tokens=100000),
    )
    return executor, provider


def invoke(executor, *, role="designer", adapter=None, **kwargs):
    request = executor.request(role, "Create or revise the public design", ("Traceable and implementable",), **kwargs)
    return asyncio.run(executor.execute(request, adapter or Designer()))


def read_document(executor):
    return DesignDocument.model_validate_json(
        executor.workspace.read_public(executor.state.design.document_ref.path).read_text(encoding="utf-8")
    )


def result(context, **fields):
    return RoleResult(
        request_id=context.request.request_id, role=context.request.role,
        state_revision=context.request.state_revision, status="completed", summary="Synthetic fixture", **fields,
    )


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_offline_designer_contract_through_executor(tmp_path, controller):
    executor, provider = make_run(tmp_path, response(), controller=controller)
    outcome = invoke(executor, request_id="designer-initial")
    assert outcome.status == "completed", outcome.error
    state, document = executor.state, read_document(executor)
    assert (state.state_revision, document.version) == (1, 1)
    assert len(document.sections) == 8
    assert [r.requirement_id for r in state.requirements] == [f"R{i:03}" for i in range(1, 15)]
    assert all(r.source_excerpt in state.task.instruction for r in state.requirements)
    assert all(r.status == RequirementStatus.NOT_IMPLEMENTED and r.verification is None for r in state.requirements)
    assert not state.game_revisions and not state.assets and not state.evidence and state.readiness is None
    assert state.design.assumptions == document.assumptions
    assert (state.ledger.usage.model_calls, state.ledger.usage.tool_calls) == (1, 4)
    assert (state.ledger.usage.input_tokens, state.ledger.usage.output_tokens) == (2000, 3000)
    for ref in outcome.artifact_refs:
        assert ref.path.startswith("artifacts/designer/")
        data = executor.workspace.read_public(ref.path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == ref.sha256
    prompt = provider.prompts[0]
    assert prompt["input"]["task"]["instruction"] == state.task.instruction
    assert state.task.instruction == INSTRUCTION.read_text(encoding="utf-8")
    assert state.task.task_id == "platformer-time-loop"
    assert not re.search(r"[\u3400-\u9fff]", json.dumps(prompt, ensure_ascii=False))
    assert not re.search(r"[\u3400-\u9fff]", outcome.model_dump_json())
    assert prompt["input"]["task"]["environment_constraints"] == list(state.task.environment_constraints)
    assert not prompt["input"]["requirements"] and prompt["input"]["current_design"] is None
    assert "HIDDEN-" not in json.dumps(prompt) and "SECRET-SENTINEL" not in json.dumps(prompt)
    assert "ledger" not in prompt["input"] and "controller" not in prompt["input"]
    assert Executor.open(executor.workspace, services=executor.services).state == state


def test_both_controllers_receive_identical_designer_prompts(tmp_path):
    prompts = []
    for controller in ("fixed_loop", "adaptive_scheduler"):
        executor, provider = make_run(tmp_path / controller, response(), controller=controller)
        assert invoke(executor).status == "completed"
        prompts.append(provider.prompts[0])
    assert prompts[0] == prompts[1]


def test_targeted_feedback_revision_preserves_requirements_and_untouched_sections(tmp_path):
    patch = {
        "status": "completed", "summary": "Clarify that pausing freezes recorded interactions and the shared loop tick, following issue I1.",
        "sections": [{
            "section_id": "state_flow", "requirement_ids": ["R003", "R004", "R005"],
            "content": response()["sections"][2]["content"] + " While paused, no recorded interaction can fire; resume at the same shared loop tick.",
        }],
    }
    executor, provider = make_run(tmp_path, response(), patch)
    assert invoke(executor).status == "completed"
    old = executor.state
    original = read_document(executor)

    async def qa(context):
        issue = IssueRecord(
            issue_id="I1", issue_type="design", severity="minor", suggested_role="designer",
            description="Clarify paused ghost interactions and timeline resumption.", requirement_ids=("R003", "R004"),
        )
        return result(context, proposed_updates=ProposedUpdates(issues=(issue,)))

    assert invoke(executor, role="qa_reviewer", adapter=qa).status == "completed"
    outcome = invoke(executor, work_item_ids=("I1",))
    assert outcome.status == "completed", outcome.error
    updated = read_document(executor)
    assert updated.version == 2 and updated.previous_version == 1
    assert updated.changed_section_ids == ("state_flow",) and updated.changed_requirement_ids == ()
    assert updated.feedback_issue_ids == ("I1",)
    assert updated.assumptions == original.assumptions and executor.state.requirements == old.requirements
    assert all(a == b for a, b in zip(original.sections, updated.sections) if a.section_id != "state_flow")
    assert executor.state.issues[0].status == "open"  # Designer did not close QA's issue.
    assert provider.prompts[1]["input"]["issues"][0]["issue_id"] == "I1"
    assert provider.prompts[1]["input"]["current_design"]["version"] == 1
    assert len(executor.state.artifacts) == len(old.artifacts) + 2
    assert executor.workspace.read_public(old.design.document_ref.path).exists()


def test_requirement_addition_is_an_upsert_with_complete_retained_design(tmp_path):
    patch = {"status": "completed", "summary": "Extract the clock-hand title presentation as a separate requirement.", "requirements": [{
        "requirement_id": "R015", "description": "Overlapping clock hands and ghost silhouettes on the title screen",
        "source_excerpt": "A title screen shows overlapping clock hands and ghost silhouettes.",
        "acceptance_criteria": ["The ordinary title entry visibly contains overlapping clock hands and ghost silhouettes."],
    }], "sections": [{
        **response()["sections"][4],
        "content": response()["sections"][4]["content"] + " Verify the title clock hands overlap visibly.",
        "requirement_ids": [*response()["sections"][4]["requirement_ids"], "R015"],
    }]}
    executor, _ = make_run(tmp_path, response(), patch)
    invoke(executor)
    before = executor.state.requirements
    assert invoke(executor).status == "completed"
    assert executor.state.requirements[:-1] == before
    assert read_document(executor).changed_requirement_ids == ("R015",)


def test_designer_preserves_progress_in_proposal_and_executor_invalidates_old_verification(tmp_path):
    patch = {"status": "completed", "summary": "Clarify the acceptance checks for the ordinary game entry.", "requirements": [{
        **response()["requirements"][0],
        "acceptance_criteria": [*response()["requirements"][0]["acceptance_criteria"], "The title entry remains playable after returning from level completion."],
    }]}
    executor, _ = make_run(tmp_path, response(), patch)
    invoke(executor)

    async def developer(context):
        await context.tools.write_text("game/project.godot", "; simulated project")
        ref = ArtifactRef(artifact_id="project", kind="game_project", path=context.tools.output_path("game"), created_by="developer")
        return result(context, artifact_refs=(ref,), proposed_updates=ProposedUpdates(
            game_revision=GameRevision(revision_id="G1", project_ref=ref, change_summary="Synthetic game"),
        ))

    async def player(context):
        path = await context.tools.write_text("observation.txt", "Synthetic observation, not an actual game run")
        ref = ArtifactRef(artifact_id="observation", kind="report", path=path, created_by="player")
        return result(context, artifact_refs=(ref,), proposed_updates=ProposedUpdates(evidence=(EvidenceRecord(
            evidence_id="E1", kind="observation", game_revision_id="G1", requirement_ids=("R001",),
            artifact_refs=(ref,), observation="Synthetic verification setup", outcome="passed",
        ),)))

    async def qa(context):
        req = context.state.requirements[0].revised(status="verified_pass", verification=Verification(
            game_revision_id="G1", evidence_ids=("E1",), rationale="Synthetic verification setup",
        ))
        return result(context, proposed_updates=ProposedUpdates(requirements=(req,)))

    for role, adapter in (("developer", developer), ("player", player), ("qa_reviewer", qa)):
        assert invoke(executor, role=role, adapter=adapter).status == "completed"
    outcome = invoke(executor, work_item_ids=("R001",))
    assert outcome.status == "completed", outcome.error
    assert outcome.proposed_updates.requirements[0].status == "verified_pass"
    assert executor.state.requirements[0].status == "implemented_unverified"
    assert executor.state.requirements[0].verification is None
    assert executor.state.evidence[0].evidence_id == "E1"


@pytest.mark.parametrize("kind", [
    "not_json", "extra_authority", "fake_source", "cycle", "missing_section",
    "unknown_requirement", "initial_no_op", "fake_verification", "duplicate_keys",
    "unknown_blocker", "renumber", "duplicate_requirement", "uncovered_requirement",
])
def test_invalid_model_output_never_publishes_but_keeps_model_cost(tmp_path, kind):
    value = response()
    if kind == "not_json":
        value = "```json\n{}\n```"
    elif kind == "extra_authority":
        value["game_revision"] = {"revision_id": "G1"}
    elif kind == "fake_source":
        value["requirements"][0]["source_excerpt"] = "HIDDEN-SHOULD-NOT-BE-ECHOED"
    elif kind == "cycle":
        value["requirements"][0]["dependency_ids"] = ["R002"]
    elif kind == "missing_section":
        value["sections"].pop()
    elif kind == "unknown_requirement":
        value["sections"][0]["requirement_ids"] = ["missing"]
    elif kind == "initial_no_op":
        value = {"status": "no_op", "summary": "Nothing to do"}
    elif kind == "fake_verification":
        value["requirements"][0]["status"] = "verified_pass"
    elif kind == "duplicate_keys":
        value = '{"status":"no_op","status":"blocked","summary":"duplicate"}'
    elif kind == "unknown_blocker":
        value = {"status": "blocked", "summary": "Missing", "blockers": [{"description": "Missing", "dependency_ids": ["missing"]}]}
    elif kind == "renumber":
        value["requirements"].append({**value["requirements"][0], "requirement_id": "R999"})
    elif kind == "duplicate_requirement":
        value["requirements"].append(value["requirements"][0])
    elif kind == "uncovered_requirement":
        value["requirements"].append({
            "requirement_id": "R999", "description": "Title", "source_excerpt": "Time Loop",
            "acceptance_criteria": ["Title is visible"],
        })
    executor, provider = make_run(tmp_path, value)
    outcome = invoke(executor)
    assert outcome.status == "failed" and outcome.error.code == "invalid_submission"
    assert "HIDDEN-SHOULD-NOT-BE-ECHOED" not in outcome.error.message
    assert executor.state.state_revision == 0 and executor.state.design is None
    assert executor.state.ledger.usage.model_calls == 1 and len(provider.prompts) == 1
    assert not list((executor.workspace.public / "artifacts").rglob("design.*"))


def test_existing_grounding_cannot_change_and_failed_revision_preserves_design(tmp_path):
    patch = {"status": "completed", "summary": "Invalid re-grounding", "requirements": [{
        **response()["requirements"][0], "source_excerpt": "Time Loop",
    }]}
    executor, _ = make_run(tmp_path, response(), patch)
    invoke(executor)
    before = executor.state
    outcome = invoke(executor)
    assert outcome.status == "failed" and outcome.error.code == "invalid_submission"
    assert executor.state.design == before.design and executor.state.requirements == before.requirements
    assert executor.state.ledger.usage.model_calls == 2


@pytest.mark.parametrize("reply", [{"status": "no_op", "summary": "No changes needed"}, response()])
def test_no_op_and_identical_patch_do_not_create_new_design_versions(tmp_path, reply):
    executor, _ = make_run(tmp_path, response(), reply)
    invoke(executor)
    before = executor.state
    outcome = invoke(executor)
    assert outcome.status == "no_op"
    assert executor.state.design == before.design and executor.state.state_revision == 1
    assert executor.state.ledger.usage.model_calls == 2


def test_blocked_result_records_dependencies_without_writing_design(tmp_path):
    executor, _ = make_run(tmp_path, {
        "status": "blocked", "summary": "Public runtime constraints conflict.",
        "blockers": [{"description": "Resolve the conflicting public runtime configuration."}],
    })
    outcome = invoke(executor)
    assert outcome.status == "blocked" and outcome.blockers
    assert executor.state.design is None and executor.state.state_revision == 0
    assert executor.state.ledger.usage.model_calls == 1


@pytest.mark.parametrize("budget, expected_calls", [
    (ResourceLimits(max_model_calls=0), 0),
    (ResourceLimits(max_tool_calls=3), 1),  # JSON staged but Markdown cannot be written.
])
def test_budget_rejection_is_atomic_and_uses_common_meter(tmp_path, budget, expected_calls):
    executor, provider = make_run(tmp_path, response(), budget=budget)
    outcome = invoke(executor)
    assert outcome.status == "failed" and outcome.error.code == "budget_exceeded"
    assert executor.state.design is None and executor.state.state_revision == 0
    assert len(provider.prompts) == executor.state.ledger.usage.model_calls == expected_calls
    assert not list((executor.workspace.public / "artifacts").rglob("design.*"))


def test_provider_failure_is_not_retried_or_echoed_and_retains_reservation(tmp_path):
    executor, provider = make_run(tmp_path, OSError("SECRET-API-KEY"))
    outcome = invoke(executor)
    assert outcome.status == "failed" and "SECRET-API-KEY" not in outcome.model_dump_json()
    assert len(provider.prompts) == 1 and executor.state.ledger.usage.model_calls == 1
    assert not executor.state.ledger.usage.token_usage_complete
    assert executor.state.ledger.usage.output_tokens == 8192


@pytest.mark.parametrize("config, expected_calls", [
    (DesignerConfig(max_prompt_bytes=100), 0),
    (DesignerConfig(max_response_bytes=100), 1),
])
def test_explicit_context_and_response_bounds_fail_without_truncation(tmp_path, config, expected_calls):
    executor, provider = make_run(tmp_path, response())
    outcome = invoke(executor, adapter=Designer(config))
    assert outcome.status == "failed" and outcome.error.code == "invalid_submission"
    assert len(provider.prompts) == expected_calls and executor.state.design is None


def test_role_and_recorded_configuration_cannot_silently_change(tmp_path):
    executor, provider = make_run(tmp_path, response())
    assert invoke(executor, role="player").error.code == "permission_denied"
    assert not provider.prompts
    assert invoke(executor).status == "completed"
    outcome = invoke(executor, adapter=Designer(DesignerConfig(max_output_tokens=1000)))
    assert outcome.error.code == "invalid_submission" and len(provider.prompts) == 1


def test_old_markdown_design_is_visible_during_structured_conversion(tmp_path):
    executor, provider = make_run(tmp_path, response())

    async def legacy(context):
        path = await context.tools.write_text("legacy.md", "# Legacy design\nKeep the full 30-second loop.")
        ref = ArtifactRef(artifact_id="legacy", kind="document", path=path, created_by="designer", media_type="text/markdown")
        return result(context, artifact_refs=(ref,), proposed_updates=ProposedUpdates(design=DesignSpec(
            version=1, document_ref=ref, change_summary="Legacy fixture", assumptions=("Old assumption",),
        )))

    assert invoke(executor, adapter=legacy).status == "completed"
    assert invoke(executor).status == "completed"
    assert read_document(executor).version == 2
    assert "Keep the full 30-second loop" in provider.prompts[0]["input"]["legacy_design_markdown"]
    assert provider.prompts[0]["input"]["existing_assumptions"] == ["Old assumption"]


def test_explicit_text_attachment_enters_prompt_but_unrelated_issue_does_not(tmp_path):
    executor, provider = make_run(tmp_path, response(), {"status": "no_op", "summary": "Reviewed feedback"})
    invoke(executor)

    async def qa(context):
        path = await context.tools.write_text("feedback.md", "PUBLIC-FEEDBACK: clarify the retry focus")
        ref = ArtifactRef(artifact_id="feedback", kind="report", path=path, created_by="qa_reviewer")
        return result(context, artifact_refs=(ref,), proposed_updates=ProposedUpdates(issues=(
            IssueRecord(issue_id="I1", issue_type="design", severity="minor", suggested_role="designer",
                        description="Retry focus", requirement_ids=("R004",)),
            IssueRecord(issue_id="I2", issue_type="runtime", severity="major", suggested_role="developer",
                        description="Unrelated runtime problem", requirement_ids=("R001",)),
        )))

    qa_result = invoke(executor, role="qa_reviewer", adapter=qa)
    assert qa_result.status == "completed"
    assert invoke(executor, work_item_ids=("R004",), artifact_refs=qa_result.artifact_refs).status == "no_op"
    inputs = provider.prompts[-1]["input"]
    assert [issue["issue_id"] for issue in inputs["issues"]] == ["I1"]
    assert inputs["attachments"][0]["text"].startswith("PUBLIC-FEEDBACK")


def test_pinned_benchmark_source_is_verified_before_running_the_example(tmp_path, monkeypatch):
    demo = runpy.run_path(str(ROOT / "examples" / "designer_demo.py"))
    load_source = demo["load_benchmark_source"]
    metadata, path, data = load_source()
    assert path == INSTRUCTION and metadata == SOURCE
    assert hashlib.sha256(data).hexdigest() == SOURCE["instruction_sha256"]
    wrong = {**metadata, "instruction_sha256": "0" * 64}
    (tmp_path / "source.json").write_text(json.dumps(wrong), encoding="utf-8")
    monkeypatch.setitem(load_source.__globals__, "CASE", tmp_path)
    with pytest.raises(ValueError, match="pinned example source"):
        load_source()


def test_prompt_and_rendering_templates_use_english_and_a_new_version():
    from adaptive_game_generation.roles.designer.contracts import PROMPT_VERSION, SECTION_TITLES
    from adaptive_game_generation.roles.designer.prompt import INSTRUCTIONS
    assert PROMPT_VERSION == "designer.v3"
    assert "Write all authored output in English" in INSTRUCTIONS
    assert not re.search(r"[\u3400-\u9fff]", INSTRUCTIONS + json.dumps(SECTION_TITLES, ensure_ascii=False))


def test_verbatim_source_lines_preserve_instruction_and_legacy_prompt():
    from adaptive_game_generation.roles.designer.prompt import INSTRUCTIONS, build_prompt
    instruction = INSTRUCTION.read_text(encoding="utf-8")
    inputs = {"task": {"instruction": instruction}}
    current = json.loads(build_prompt(inputs, DesignerConfig()))
    assert current["input"] == inputs
    assert current["source_lines"] == instruction.splitlines()
    assert all(line in instruction for line in current["source_lines"])
    assert "the JSON escape \\n" in current["instructions"]
    legacy = json.loads(build_prompt(inputs, DesignerConfig(prompt_version="designer.v2")))
    assert "source_lines" not in legacy
    assert legacy["instructions"] == INSTRUCTIONS
    assert legacy["input"] == inputs


def test_flattening_a_real_instruction_line_break_is_canonicalized(tmp_path):
    draft = response()
    # This was the actual failure observed in the restored DeepSeek live run.
    quotation = "A chapter menu reveals four chapters of six levels each."
    instruction = INSTRUCTION.read_text(encoding="utf-8")
    assert quotation not in instruction
    assert quotation in " ".join(instruction.split())
    draft["requirements"][0]["source_excerpt"] = quotation
    executor, _ = make_run(tmp_path, draft)
    outcome = invoke(executor)
    assert outcome.status == "completed", outcome.error
    assert executor.state.requirements[0].source_excerpt in instruction
    assert executor.state.requirements[0].source_excerpt != quotation
