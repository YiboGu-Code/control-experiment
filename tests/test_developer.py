"""Offline contract/failure tests; live Developer tests use a real model and engine."""

import asyncio
import json
import runpy
from pathlib import Path

import pytest

from adaptive_game_generation.models import ResourceLimits, RoleResult
from adaptive_game_generation.roles.developer import Developer, DeveloperConfig, DeveloperReport
from adaptive_game_generation.runtime import Executor, ProcessResult, RuntimeConfig, Services, TextReply, Workspace
from adaptive_game_generation.runtime.errors import SandboxCleanupFailed, SandboxUnavailable

ROOT = Path(__file__).resolve().parents[1]
DEMO = runpy.run_path(str(ROOT / "examples/developer_demo.py"))


class ScriptedText:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def input_token_upper_bound(self, prompt):
        return len(prompt.encode()) + 64

    async def generate(self, prompt, *, max_output_tokens, timeout):
        self.prompts.append(json.loads(prompt))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return TextReply(reply if isinstance(reply, str) else json.dumps(reply), 1000, 2000)


class ScriptedSandbox:
    """Deterministic boundary fixture only; never used by the live demo."""
    def __init__(self, *results, mutate_asset=False, mutate_source=False):
        self.results, self.commands = list(results), []
        self.mutate_asset, self.mutate_source = mutate_asset, mutate_source

    async def run(self, workspace, argv, timeout):
        self.commands.append(argv)
        if "--version" in argv:
            return ProcessResult(0, "4.5.1.offline-test-fixture", "")
        cache = workspace.output_path("game/.godot/test.cache")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("Synthetic cache")
        if self.mutate_asset:
            next(workspace.output_path("game/assets").rglob("*.png")).write_bytes(b"modified")
        if self.mutate_source:
            workspace.output_path("game/scripts/main.gd").write_text("modified")
        result = self.results.pop(0) if self.results else ProcessResult(0, "Offline check fixture", "")
        if isinstance(result, BaseException):
            raise result
        return result


def proposal(*, script='extends Node2D\n\nfunc _ready() -> void:\n\tpass\n', **changes):
    return {"status": "completed", "summary": "Implement the requested source patch",
        "files": [
            {"path": "project.godot", "content": 'config_version=5\n[application]\nconfig/name="Time Loop"\nrun/main_scene="res://Main.tscn"\n'},
            {"path": "Main.tscn", "content": '[gd_scene load_steps=2 format=3]\n[ext_resource type="Script" path="res://scripts/main.gd" id="1"]\n[node name="Main" type="Node2D"]\nscript=ExtResource("1")\n'},
            {"path": "scripts/main.gd", "content": script}],
        "affected_requirement_ids": ["R002"], "implemented_requirement_ids": ["R002"],
        "controls": ["Space jumps"], "scenarios": [{"scenario_id": "near_rewind", "description": "A deterministic late-loop state"}],
        **changes}


def make_run(tmp_path, *replies, backend=None, controller="fixed_loop", budget=None, seed=True):
    source, _, instruction, ds, design, art_state, art_data = DEMO["load_recorded_inputs"]()
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_bytes(instruction)
    (task_dir / "tests").mkdir()
    (task_dir / "tests/rubric.json").write_text("HIDDEN-RUBRIC-SENTINEL")
    (task_dir / ".env").write_text("SECRET-CREDENTIAL-SENTINEL")
    workspace, task = Workspace.prepare(tmp_path / "run", task_dir, task_id=source["task_id"])
    text = ScriptedText(*replies)
    limits = budget or ResourceLimits(max_model_calls=10, max_input_tokens=2000000, max_output_tokens=300000,
        max_image_calls=0, max_tool_calls=1000)
    executor = Executor.create(workspace, task, run_id="developer-test", controller=controller, budget=limits,
        config=RuntimeConfig(role_limits=limits), services=Services(text=text, sandbox=backend))
    if seed:
        asyncio.run(DEMO["seed_recorded_inputs"](executor, ds, design, art_state, art_data))
    return executor, text


def invoke(executor, config=None, **kwargs):
    request = executor.request("developer", "Implement the public Time Loop game", ("Integrate Art and run basic checks",), **kwargs)
    return asyncio.run(executor.execute(request, Developer(config)))


def report(executor):
    ref = max((r for r in executor.state.artifacts if r.artifact_id == "developer-report"), key=lambda r: r.version)
    return DeveloperReport.model_validate_json(executor.workspace.read_public(ref.path).read_bytes())


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_developer_contract_upstream_isolation_and_protected_mock_art(tmp_path, controller):
    backend = ScriptedSandbox()
    executor, text = make_run(tmp_path, proposal(), backend=backend, controller=controller)
    before = executor.state
    result = invoke(executor, request_id="initial")
    assert result.status == "partial", result.error
    state, document = executor.state, report(executor)
    assert state.state_revision == 3 and state.game_revisions[-1].build_status == "passed"
    assert state.design == before.design and state.assets == before.assets
    assert all(r.verification is None for r in state.requirements) and state.readiness is None
    assert next(r for r in state.requirements if r.requirement_id == "R002").status == "implemented_unverified"
    assert state.ledger.usage.image_calls == 0 and state.ledger.usage.model_calls == 1
    assert len(state.evidence) == 1 and state.evidence[0].kind == "build_check"
    assert len(document.integrated_assets) == 12 and all(a.is_mock for a in document.integrated_assets)
    project = executor.workspace.read_public(state.game_revisions[-1].project_ref.path)
    assert not (project / ".godot").exists()
    for asset in document.integrated_assets:
        assert (project / asset.target_path).read_bytes() == executor.workspace.read_public(asset.artifact_ref.path).read_bytes()
    prompt = text.prompts[0]
    assert prompt["input"]["task"]["instruction"] == before.task.instruction
    assert "HIDDEN-" not in json.dumps(prompt) and "SECRET-CREDENTIAL" not in json.dumps(prompt)
    assert "controller" not in prompt["input"] and "ledger" not in prompt["input"]
    assert all("--" not in command or command.index("--quit-after") < command.index("--") for command in backend.commands)
    assert Executor.open(executor.workspace, services=executor.services).state == state


def test_same_initial_prompt_for_both_controllers(tmp_path):
    prompts = []
    for controller in ("fixed_loop", "adaptive_scheduler"):
        executor, provider = make_run(tmp_path / controller, proposal(), backend=ScriptedSandbox(), controller=controller)
        assert invoke(executor, request_id="initial").status == "partial"
        prompts.append(provider.prompts[0])
    assert prompts[0] == prompts[1]


def test_real_diagnostic_shape_triggers_bounded_repair_even_with_exit_zero(tmp_path):
    backend = ScriptedSandbox(ProcessResult(0, "", "SCRIPT ERROR: Parse Error: bad type at scripts/main.gd:3"))
    repair = proposal(files=[{"path": "scripts/main.gd", "content": "extends Node2D\n# repaired\n"}])
    executor, text = make_run(tmp_path, proposal(), repair, backend=backend)
    outcome = invoke(executor)
    assert outcome.status == "partial", outcome.error
    document = report(executor)
    assert [a.status for a in document.build_attempts] == ["failed", "passed"]
    assert "bad type" in json.dumps(text.prompts[1]["build_feedback"])
    assert "project.godot" in text.prompts[1]["current_project_files"]
    assert text.prompts[1]["input"]["current_delivery_contract"]["controls"] == ["Space jumps"]
    assert executor.state.ledger.usage.model_calls == 2


def test_exhausted_repairs_publish_failed_build_without_progress_claims(tmp_path):
    failed = ProcessResult(1, "", "Parse Error: fixture")
    executor, text = make_run(tmp_path, proposal(), proposal(), backend=ScriptedSandbox(failed, failed))
    outcome = invoke(executor, DeveloperConfig(max_model_turns=2))
    assert outcome.status == "partial" and len(text.prompts) == 2
    assert executor.state.game_revisions[-1].build_status == "failed"
    assert executor.state.evidence[-1].outcome == "failed"
    assert all(r.status == "not_implemented" for r in executor.state.requirements)
    from adaptive_game_generation.controllers.results import export_run
    summary = export_run(executor, tmp_path / "review")
    assert summary["delivery_exported"] and not summary["delivery_ready"]
    assert summary["final_build_status"] == "failed"
    assert summary["game_builds"] == [{"revision_id": "game-0001", "build_status": "failed"}]
    assert "player" in summary["roles_not_invoked"]


@pytest.mark.parametrize("backend", [None, ScriptedSandbox(SandboxUnavailable("No isolated engine"))])
def test_unavailable_engine_is_recorded_not_mocked_as_success(tmp_path, backend):
    executor, text = make_run(tmp_path, proposal(), backend=backend)
    outcome = invoke(executor)
    assert outcome.status == "partial", outcome.error
    assert executor.state.game_revisions[-1].build_status == "not_run"
    assert not executor.state.evidence and len(text.prompts) == 1
    assert all(r.status == "not_implemented" for r in executor.state.requirements)


@pytest.mark.parametrize("mutation", ["outside", "asset", "script_binary", "traces", "unknown_requirement", "forged_evidence", "duplicate_json", "missing_main", "case_collision", "delete_missing", "project_prefix"])
def test_invalid_proposals_never_execute_or_publish(tmp_path, mutation):
    value = proposal()
    if mutation == "outside":
        value["files"][2]["path"] = "../escape.gd"
    elif mutation == "asset":
        value["files"][2]["path"] = "assets/altered.gd"
    elif mutation == "script_binary":
        value["files"][2]["path"] = "native.dll"
    elif mutation == "traces":
        value["files"][2]["path"] = "demo_outputs/fake.json"
    elif mutation == "unknown_requirement":
        value["affected_requirement_ids"] = ["UNKNOWN"]
        value["implemented_requirement_ids"] = []
    elif mutation == "forged_evidence":
        value["build_status"] = "passed"
    elif mutation == "duplicate_json":
        value = '{"status":"completed","status":"no_op"}'
    elif mutation == "missing_main":
        value["files"].pop(1)
    elif mutation == "case_collision":
        value["files"].append({"path": "scripts/MAIN.gd", "content": "extends Node"})
    elif mutation == "delete_missing":
        value["delete_paths"] = ["missing.gd"]
    else:
        value["files"].append({"path": "scripts/main.gd/other.gd", "content": "extends Node"})
    backend = ScriptedSandbox()
    executor, _ = make_run(tmp_path, value, backend=backend)
    result = invoke(executor, DeveloperConfig(max_model_turns=1))
    assert result.status == "failed" and not backend.commands
    assert executor.state.state_revision == 2 and not executor.state.game_revisions


def test_incremental_history_and_source_deletion(tmp_path):
    first = proposal()
    first["files"].append({"path": "scripts/unused.gd", "content": "extends Node\n"})
    second = proposal(files=[{"path": "scripts/main.gd", "content": "extends Node2D\n# Revision two\n"}], delete_paths=["scripts/unused.gd"])
    executor, _ = make_run(tmp_path, first, second, backend=ScriptedSandbox())
    assert invoke(executor).status == "partial"
    old = executor.state.game_revisions[-1]
    old_bytes = executor.workspace.read_public(old.project_ref.path + "/scripts/main.gd").read_bytes()
    assert invoke(executor).status == "partial"
    new = executor.state.game_revisions[-1]
    assert new.parent_revision_id == old.revision_id and new.project_ref.version == 2
    assert executor.workspace.read_public(old.project_ref.path + "/scripts/main.gd").read_bytes() == old_bytes
    assert not executor.workspace.read_public(new.project_ref.path + "/scripts/unused.gd").exists()


def test_frozen_developer_configuration(tmp_path):
    executor, text = make_run(tmp_path, proposal(), backend=ScriptedSandbox())
    assert invoke(executor).status == "partial"
    assert invoke(executor, DeveloperConfig(max_model_turns=1)).status == "failed"
    assert len(text.prompts) == 1


def test_no_op_and_missing_preconditions(tmp_path):
    noop = {"status": "no_op", "summary": "No changes needed"}
    executor, text = make_run(tmp_path / "a", proposal(), seed=False)
    assert invoke(executor).status == "blocked" and not text.prompts
    executor, _ = make_run(tmp_path / "b", noop, proposal(), noop, backend=ScriptedSandbox())
    assert invoke(executor).status == "failed"
    assert invoke(executor).status == "partial"
    before = executor.state.state_revision
    assert invoke(executor).status == "no_op" and executor.state.state_revision == before


def test_mock_art_cannot_be_modified_by_game_code(tmp_path):
    executor, _ = make_run(tmp_path, proposal(), backend=ScriptedSandbox(mutate_asset=True))
    assert invoke(executor).status == "failed"
    assert not executor.state.game_revisions and executor.state.state_revision == 2


def test_source_mutation_is_not_reported_as_a_passing_build(tmp_path):
    executor, _ = make_run(tmp_path, proposal(), backend=ScriptedSandbox(mutate_source=True))
    assert invoke(executor, DeveloperConfig(max_model_turns=1)).status == "partial"
    assert executor.state.game_revisions[-1].build_status == "failed"
    assert report(executor).build_attempts[-1].steps[-1].name == "source_integrity"


def test_cleanup_failure_keeps_runtime_terminal_guard(tmp_path):
    executor, _ = make_run(tmp_path, proposal(), backend=ScriptedSandbox(SandboxCleanupFailed("Cleanup failed")))
    outcome = invoke(executor)
    assert outcome.status == "failed" and executor.state.ledger.termination is not None
    assert not executor.state.game_revisions


def test_model_budget_prevents_dispatch(tmp_path):
    limits = ResourceLimits(max_model_calls=0, max_tool_calls=1000)
    executor, text = make_run(tmp_path, proposal(), budget=limits, backend=ScriptedSandbox())
    outcome = invoke(executor)
    assert outcome.status == "failed" and outcome.error.code == "budget_exceeded"
    assert not text.prompts


def test_timeout_is_recorded_as_failure_and_repaired(tmp_path):
    backend = ScriptedSandbox(asyncio.TimeoutError())
    executor, text = make_run(tmp_path, proposal(), proposal(), backend=backend)
    outcome = invoke(executor)
    assert outcome.status == "partial", outcome.error
    attempts = report(executor).build_attempts
    assert [a.status for a in attempts] == ["failed", "passed"]
    assert attempts[0].steps[-1].returncode is None
    assert "timed out" in attempts[0].steps[-1].reason
    assert "timed out" in json.dumps(text.prompts[1]["build_feedback"])


def test_repair_can_withdraw_earlier_implementation_claim(tmp_path):
    backend = ScriptedSandbox(ProcessResult(1, "", "SCRIPT ERROR: incomplete implementation"))
    repair = proposal(status="partial", implemented_requirement_ids=[], remaining_work=["Movement still needs implementation"])
    executor, _ = make_run(tmp_path, proposal(), repair, backend=backend)
    outcome = invoke(executor)
    assert outcome.status == "partial", outcome.error
    assert report(executor).model_implementation_claims == ()
    assert executor.state.game_revisions[-1].build_status == "passed"
    assert next(r for r in executor.state.requirements if r.requirement_id == "R002").status == "not_implemented"


def test_invalid_protocol_is_returned_to_model_before_staging(tmp_path):
    invalid = '{"status":"completed","status":"partial"}'
    backend = ScriptedSandbox()
    executor, text = make_run(tmp_path, invalid, proposal(), backend=backend)
    outcome = invoke(executor)
    assert outcome.status == "partial", outcome.error
    document = report(executor)
    assert len(document.proposal_rejections) == 1
    assert document.proposal_rejections[0].errors == ("Duplicate JSON key",)
    assert document.build_attempts[0].model_turn == 2
    assert text.prompts[1]["current_project_files"] == {}
    assert text.prompts[1]["rejected_proposal"] == invalid
    assert executor.state.ledger.usage.model_calls == 2


def test_invalid_protocol_exhaustion_never_runs_code(tmp_path):
    backend = ScriptedSandbox()
    executor, text = make_run(tmp_path, "{", "{", backend=backend)
    outcome = invoke(executor, DeveloperConfig(max_model_turns=2))
    assert outcome.status == "failed" and len(text.prompts) == 2
    assert "validation budget exhausted" in outcome.error.message
    assert not backend.commands and not executor.state.game_revisions


@pytest.mark.parametrize("role", ["designer", "art_producer", "player", "qa_reviewer"])
def test_removal_tool_is_developer_only(tmp_path, role):
    executor, _ = make_run(tmp_path)
    async def adapter(context):
        await context.tools.write_text("temporary.txt", "test")
        await context.tools.remove_output("temporary.txt")
        return RoleResult(request_id=context.request.request_id, role=role, state_revision=context.request.state_revision,
                          status="no_op", summary="No changes")
    request = executor.request(role, "Try output removal", ("Access should be denied",))
    assert asyncio.run(executor.execute(request, adapter)).error.code == "permission_denied"


def test_remove_cannot_reach_published_state(tmp_path):
    executor, _ = make_run(tmp_path)
    async def adapter(context):
        await context.tools.remove_output("../public/input/instruction.md")
    request = executor.request("developer", "Try removing a published input", ("Access should be denied",))
    assert asyncio.run(executor.execute(request, adapter)).error.code == "permission_denied"
    assert executor.workspace.read_public("input/instruction.md").is_file()
