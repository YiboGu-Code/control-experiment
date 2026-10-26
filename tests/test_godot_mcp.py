"""Offline MCP boundaries and Developer feedback loop; no online model calls."""
import asyncio
import hashlib
import io
import json
from pathlib import Path
import runpy

from PIL import Image
import pytest

from adaptive_game_generation.models import ArtifactRef, ResourceLimits, RoleResult
from adaptive_game_generation.runtime import Executor, Services, TextReply, GodotInspectionRequest
from adaptive_game_generation.runtime.godot_mcp import DockerGodotMCP
from adaptive_game_generation.runtime.policy import RolePolicy
from adaptive_game_generation.runtime.errors import PermissionDenied, InvalidSubmission
from adaptive_game_generation.roles.developer import DeveloperConfig
from test_developer import make_run, invoke, proposal, report, ScriptedSandbox

ROOT = Path(__file__).resolve().parents[1]


def test_scoped_preview_uses_scanner_warnings_without_disabling_security():
    driver = runpy.run_path(str(ROOT / "configs/godot_mcp/mcp_driver.py"))
    env = driver["preview_environment"](Path("/tmp/mcp-reviewed"), {
        "GODOT_MCP_STRICT": "true", "GODOT_MCP_DISABLE_SECURITY": "true",
        "GODOT_MCP_DISABLE_ELICITATION": "true", "GAMECRAFT_PREVIEW_SCENARIO": "stale"})
    assert not any(k in env for k in ("GODOT_MCP_STRICT", "GODOT_MCP_DISABLE_SECURITY", "GODOT_MCP_DISABLE_ELICITATION", "GAMECRAFT_PREVIEW_SCENARIO"))
    assert env["LIBGL_ALWAYS_SOFTWARE"] == "1"
    assert driver["preview_environment"](Path("/tmp/mcp-reviewed"), {}, "loop_rewrite_imminent")["GAMECRAFT_PREVIEW_SCENARIO"] == "loop_rewrite_imminent"
    assert driver["TOOLS"] == ("get_scene_tree", "run_project", "simulate_input", "take_screenshot", "get_ui_elements", "get_debug_output", "stop_project")


def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (1280, 720), (30, 50, 70)).save(output, "PNG")
    return output.getvalue()


class MCPFixture:
    """Explicit engine boundary fixture; not a real MCP server or quality result."""
    def __init__(self, fault=None):
        self.calls, self.fault = [], fault

    async def inspect(self, workspace, project, destination, job, timeout):
        self.calls.append(job)
        root = workspace.output_path(destination)
        (root / "session").mkdir(parents=True)
        pixels = image_bytes()
        (root / "job.json").write_text(json.dumps(job))
        (root / "session/preview.png").write_bytes(pixels)
        result = {"version": "godot-mcp-inspection.v1", "project_sha256": job["project_sha256"],
            "status": "passed", "network": "none", "fresh_process": True, "fresh_user_data": True,
            "acceptance_evidence": False, "deterministic_replay": False, "original_project_unchanged": True,
            "mcp_tool_calls": 7, "screenshot_sha256": hashlib.sha256(pixels).hexdigest(),
            "observations": {"get_scene_tree": {"text": "Synthetic scene inspection fixture", "truncated": False}}}
        if self.fault == "image_hash":
            result["screenshot_sha256"] = "0" * 64
        elif self.fault == "isolation":
            result["network"] = "host"
        elif self.fault == "mutation":
            workspace.read_path(project + "/Main.tscn").write_text("mutated")
        elif self.fault == "second_inspection_failed" and len(self.calls) == 2:
            result["status"] = "failed"
            result["runtime_errors"] = True
        (root / "session/result.json").write_text(json.dumps(result))
        return result


class VisualFixture:
    def __init__(self):
        self.prompts = []

    def input_token_upper_bound(self, prompt, *, images):
        return len(prompt.encode()) + 100000 * len(images)

    async def generate(self, prompt, *, images, **kwargs):
        self.prompts.append(json.loads(prompt))
        assert len(images) == 1 and images[0].data == image_bytes()
        return TextReply(json.dumps({"summary": "Synthetic visual feedback for a repair-loop test",
            "findings": [{"severity": "warning", "observation": "Fixture asks to correct sprite placement",
                          "suggested_fix": "Adjust the scene position"}] if len(self.prompts) == 1 else [],
            "limitations": ["Offline fixture, not a real visual model or gameplay assessment"]}), 1000, 400)


FINISH = {"status": "finish", "summary": "Review the latest preview and retain honest limitations", "remaining_work": []}


def setup(tmp_path, *replies, fault=None, budget=None, controller="fixed_loop"):
    executor, text = make_run(tmp_path, *replies, backend=ScriptedSandbox(), budget=budget, controller=controller)
    mcp, visual = MCPFixture(fault), VisualFixture()
    executor = Executor.open(executor.workspace, services=Services(text=text, sandbox=ScriptedSandbox(),
        godot_mcp=mcp, vision=visual))
    return executor, text, mcp, visual


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_developer_repairs_after_actual_tool_feedback_and_preserves_boundaries(tmp_path, controller):
    repair = proposal(script="extends Node2D\n# Adjusted sprite placement from visual-tool feedback\n")
    executor, text, mcp, visual = setup(tmp_path, proposal(), repair, FINISH, controller=controller)
    result = invoke(executor, DeveloperConfig(prompt_version="developer.v4", max_model_turns=5))
    assert result.status == "partial", result.error  # Upstream art is explicitly mock.
    doc = report(executor)
    assert doc.self_review_completed and len(doc.mcp_inspections) == 2
    assert len(mcp.calls) == len(visual.prompts) == 2
    assert executor.state.ledger.usage.model_calls == 5
    assert "sprite placement" in json.dumps(text.prompts[1]["inspection_feedback"])
    assert text.prompts[0]["input"]["task"]["instruction"] == executor.state.task.instruction
    assert all("HIDDEN-" not in json.dumps(p) and "SECRET-CREDENTIAL" not in json.dumps(p) for p in visual.prompts)
    assert all(e.kind == "build_check" for e in executor.state.evidence)
    assert executor.state.readiness is None
    assert all(r.verification is None for r in executor.state.requirements)
    assert doc.mcp_inspections[-1].project_sha256 == doc.project_ref.sha256
    assert all(r in executor.state.artifacts for d in doc.mcp_inspections for r in d.artifact_refs)
    assert all(not d.result["deterministic_replay"] for d in doc.mcp_inspections)


def test_inspection_selection_and_finish_require_a_staged_current_project(tmp_path):
    executor, text, mcp, _ = setup(tmp_path, FINISH, proposal(),
        {"status": "inspect", "summary": "Inspect the current scene", "inspection": {"scene_path": "Main.tscn", "frames": 5}}, FINISH)
    outcome = invoke(executor, DeveloperConfig(prompt_version="developer.v4", max_model_turns=5))
    assert outcome.status == "partial", outcome.error
    assert len(mcp.calls) == 2 and mcp.calls[-1]["frames"] == 5
    assert report(executor).proposal_rejections[0].model_turn == 1
    assert "passing build" in json.dumps(text.prompts[1]["proposal_feedback"])


def test_visual_helper_calls_share_the_role_model_budget(tmp_path):
    limits = ResourceLimits(max_model_calls=2, max_input_tokens=2000000, max_output_tokens=300000,
                            max_tool_calls=1000)
    executor, text, mcp, visual = setup(tmp_path, proposal(), FINISH, budget=limits)
    outcome = invoke(executor, DeveloperConfig(prompt_version="developer.v4", max_model_turns=5))
    assert outcome.status == "failed" and outcome.error.code == "budget_exceeded"
    assert len(text.prompts) == len(visual.prompts) == len(mcp.calls) == 1
    assert executor.state.ledger.usage.model_calls == 2


@pytest.mark.parametrize("fault", ["image_hash", "isolation", "mutation"])
def test_mcp_corruption_or_mutation_never_reaches_visual_model(tmp_path, fault):
    executor, _, _, visual = setup(tmp_path, proposal(), FINISH, fault=fault)
    outcome = invoke(executor, DeveloperConfig(prompt_version="developer.v4"))
    assert outcome.status == "failed" and outcome.error.code == "permission_denied"
    assert not visual.prompts and not executor.state.game_revisions


@pytest.mark.parametrize("role", ["designer", "art_producer", "qa_reviewer", "scheduler"])
def test_engine_inspection_is_not_a_new_permission_for_other_roles(role):
    with pytest.raises(PermissionDenied):
        RolePolicy().allow_tool(role, "godot_inspect")
    with pytest.raises(PermissionDenied):
        RolePolicy().allow_tool(role, "vision")


@pytest.mark.parametrize("values", [{"scene_path": "../secret.tscn"}, {"scene_path": "C:/secret.tscn"},
    {"scene_path": ".private/scene.tscn"}, {"frames": 181}, {"scenario": "--script"}, {"tool": "run_script"}])
def test_mcp_requests_cannot_expand_the_fixed_tool_capability(values):
    with pytest.raises((ValueError, PermissionDenied)):
        GodotInspectionRequest(**values)


def test_only_the_authorized_isolated_launch_confirmation_is_accepted():
    driver = runpy.run_path(str(ROOT / "configs/godot_mcp/mcp_driver.py"))
    confirmation = {"message": "Launching a Godot project executes arbitrary code in its autoloads and main scene. Proceed?",
        "requestedSchema": {"required": ["confirm"], "properties": {"confirm": {"type": "boolean"}}}}
    params = {"name": "run_project", "arguments": {"projectPath": "/tmp/mcp-abc/game"}}
    assert driver["authorized_launch"](confirmation, "tools/call", params)
    assert not driver["authorized_launch"](confirmation, "tools/call", {**params, "name": "run_script"})
    assert not driver["authorized_launch"](confirmation, "tools/call", {**params, "arguments": {"projectPath": "/workspace"}})
    assert not driver["authorized_launch"]({**confirmation, "message": "Send credentials"}, "tools/call", params)


def test_runner_records_mcp_and_helper_budgets_without_changing_default():
    from adaptive_game_generation.providers import GatewayConfig
    from adaptive_game_generation.models import Role
    runner = runpy.run_path(str(ROOT / "examples/fixed_loop_demo.py"))
    gateways = {r: GatewayConfig(model="fixture", api_key="unused", base_url="https://example.test") for r in runner["PREFIXES"]}
    old, _, _ = runner["role_setup"](gateways)
    new, _, budgets = runner["role_setup"](gateways, godot_mcp=True)
    assert old[Role.DEVELOPER].prompt_version == "developer.v3"
    assert new[Role.DEVELOPER].prompt_version == "developer.v4"
    assert new[Role.DEVELOPER].optimize_execution
    assert budgets[Role.DEVELOPER].max_model_calls == 14  # 8 implementation + 3 context + 3 visual calls.
    legacy, _, legacy_limits = runner["role_setup"](gateways, godot_mcp=True, values={"CODE_DEVELOPER_OPTIMIZE_EXECUTION": "false"})
    assert not legacy[Role.DEVELOPER].optimize_execution and legacy_limits[Role.DEVELOPER].max_model_calls == 11


@pytest.mark.parametrize("allowance,expected,calls", [(9, "failed", 0), (10, "partial", 1)])
def test_seven_mcp_tools_are_reserved_before_dispatch(tmp_path, allowance, expected, calls):
    limits = ResourceLimits(max_tool_calls=allowance)
    executor, _ = make_run(tmp_path, seed=False, budget=limits)
    backend = MCPFixture()
    executor = Executor.open(executor.workspace, services=Services(godot_mcp=backend))
    async def inspect(context):
        await context.tools.write_text("game/project.godot", "config_version=5")
        path = await context.tools.write_text("game/Main.tscn", '[gd_scene format=3]')
        project = path.removesuffix("/Main.tscn")
        ref = ArtifactRef(artifact_id="staged", kind="game_project", created_by="developer",
                          path=project, sha256=await context.tools.fingerprint(project))
        response = await context.tools.inspect_godot(ref, GodotInspectionRequest())
        return RoleResult(request_id=context.request.request_id, state_revision=context.request.state_revision,
                          role="developer", status="partial", summary="Budget boundary inspection only",
                          artifact_refs=(ref, *response["artifact_refs"]))
    outcome = asyncio.run(executor.execute(executor.request("developer", "Inspect", ("Inspect",)), inspect))
    assert outcome.status == expected, outcome.error
    assert len(backend.calls) == calls
    assert executor.state.ledger.usage.tool_calls == (10 if calls else 3)
    if not calls:
        assert outcome.error.code == "budget_exceeded"


def test_backend_mounts_only_the_selected_project_and_inspection_directory(tmp_path):
    from adaptive_game_generation.runtime import DockerSandbox, ProcessResult
    executor, _ = make_run(tmp_path, seed=False)
    work = executor.workspace.begin("developer", "mount-test")
    project = work.output_path("game")
    project.mkdir()
    (project / "Main.tscn").write_text('[gd_scene format=3]')
    class Boundary:
        async def run(self, narrowed, argv, timeout):
            assert narrowed.public == project
            assert narrowed.staging == work.output_path("godot_mcp/001")
            assert narrowed.public != work.public and narrowed.staging != work.staging
            command = DockerSandbox("pinned-image").command(narrowed, argv, "probe")
            assert "--network=none" in command and "--read-only" in command
            mounts = [command[i + 1] for i, part in enumerate(command) if part == "--mount"]
            assert mounts == [f"type=bind,src={project},dst=/workspace,readonly",
                              f"type=bind,src={narrowed.staging},dst=/output"]
            assert not any(".env" in part for part in command)
            (narrowed.staging / "session").mkdir()
            (narrowed.staging / "session/result.json").write_text('{"status":"passed"}')
            return ProcessResult(0, "Offline backend boundary fixture", "")
    result = asyncio.run(DockerGodotMCP(Boundary()).inspect(work, work.logical_output("game"),
        "godot_mcp/001", {"fixture": True}, 2.0))
    assert result["status"] == "passed"
    assert work.output_path("godot_mcp/001/process.json").is_file()


def test_player_can_reuse_readonly_inspection_but_qa_cannot_launch_it(tmp_path):
    executor, _ = make_run(tmp_path, proposal(), backend=ScriptedSandbox())
    assert invoke(executor).status == "partial"
    project = executor.state.game_revisions[-1].project_ref
    backend = MCPFixture()
    executor = Executor.open(executor.workspace, services=Services(godot_mcp=backend))
    prior_evidence = executor.state.evidence
    async def inspect(context):
        response = await context.tools.inspect_godot(project, GodotInspectionRequest())
        return RoleResult(request_id=context.request.request_id, state_revision=context.request.state_revision,
            role=context.request.role, status="partial", summary="Read-only diagnostic, not replay evidence",
            artifact_refs=response["artifact_refs"])
    for role, expected in (("player", "partial"), ("qa_reviewer", "failed")):
        outcome = asyncio.run(executor.execute(executor.request(role, "Inspect", ("Inspect",)), inspect))
        assert outcome.status == expected, outcome.error
    assert outcome.error.code == "permission_denied"
    assert len(backend.calls) == 1 and executor.state.evidence == prior_evidence


def test_qa_reads_diagnostics_without_promoting_them_to_acceptance(tmp_path):
    from types import SimpleNamespace
    from adaptive_game_generation.roles.qa_reviewer import QAConfig
    from adaptive_game_generation.roles.qa_reviewer.evidence import EvidenceReader
    from adaptive_game_generation.roles.qa_reviewer.role import validate_criterion
    executor, _, _, _ = setup(tmp_path, proposal(), FINISH)
    assert invoke(executor, DeveloperConfig(prompt_version="developer.v4")).status == "partial"
    captured = {}
    async def inspect(context):
        reader = EvidenceReader(context, QAConfig(), context.state.game_revisions[-1])
        await reader.collect()
        captured["reader"] = reader
        return RoleResult(request_id=context.request.request_id, state_revision=context.request.state_revision,
                          role="qa_reviewer", status="no_op", summary="Diagnostic provenance checked")
    outcome = asyncio.run(executor.execute(executor.request("qa_reviewer", "Review", ("Review",)), inspect))
    assert outcome.status == "no_op", outcome.error
    reader = captured["reader"]
    source = reader.sources["developer-mcp:0"]
    assert source["kind"] == "interpretation" and source["evidence_ids"] == []
    assert source["content"]["current_project"] and not source["content"]["acceptance_evidence"]
    criterion = SimpleNamespace(requirement_id="R002", evidence_ids=list(reader.evidence), source_ids=["developer-mcp:0"],
                                verdict="pass", basis="runtime")
    with pytest.raises(InvalidSubmission, match="corresponding inspected source"):
        validate_criterion(criterion, reader)


def test_new_patch_cannot_finish_using_stale_preview(tmp_path):
    repair = proposal(script="extends Node2D\n# changed after preview allowance\n")
    executor, text, mcp, _ = setup(tmp_path, proposal(), repair, FINISH)
    outcome = invoke(executor, DeveloperConfig(prompt_version="developer.v4", max_mcp_inspections=1, max_model_turns=3))
    assert outcome.status == "failed" and "stale" in outcome.error.message.lower()
    assert len(mcp.calls) == 1 and not executor.state.game_revisions


def test_passing_preview_does_not_mask_another_failed_scene_check(tmp_path):
    inspect = {"status": "inspect", "summary": "Inspect another entry point",
               "inspection": {"scene_path": "Main.tscn", "frames": 5}}
    executor, _, _, _ = setup(tmp_path, proposal(), inspect, FINISH, fault="second_inspection_failed")
    outcome = invoke(executor, DeveloperConfig(prompt_version="developer.v4", max_model_turns=4))
    assert outcome.status == "partial", outcome.error
    doc = report(executor)
    assert len({r.project_sha256 for r in doc.mcp_inspections}) == 1
    assert [r.result["status"] for r in doc.mcp_inspections] == ["passed", "failed"]
    assert not doc.self_review_completed
    assert any("self-review is incomplete" in text for text in doc.limitations)


@pytest.mark.parametrize("remaining,expected", [([], "completed"), (["Gameplay repair still pending"], "partial")])
def test_finish_supersedes_interim_partial_after_current_review(tmp_path, remaining, expected):
    # No Art inputs: completion must be tested independently of the mock-art gate.
    from test_developer import DEMO
    first = proposal(status="partial", remaining_work=["Review the current MCP preview"])
    executor, text = make_run(tmp_path, first, {**FINISH, "remaining_work": remaining}, seed=False,
                            backend=ScriptedSandbox())
    _, _, _, design_state, design, _, _ = DEMO["load_recorded_inputs"]()
    asyncio.run(DEMO["HELPERS"]["seed_recorded_designer"](executor, design_state, design))
    executor = Executor.open(executor.workspace, services=Services(text=text, sandbox=ScriptedSandbox(),
        godot_mcp=MCPFixture(), vision=VisualFixture()))
    result = invoke(executor, DeveloperConfig(prompt_version="developer.v4", max_model_turns=3))
    assert result.status == expected, result.error
    document = report(executor)
    assert document.self_review_completed and document.build_attempts[-1].status == "passed"
    assert "Review the current MCP preview" not in document.limitations
    assert all(item in document.limitations for item in remaining)
    assert executor.state.readiness is None  # Developer completion is not QA acceptance.


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_finish_cannot_erase_current_visual_errors(tmp_path, controller):
    from test_developer import DEMO
    class ErrorVisual(VisualFixture):
        async def generate(self, prompt, *, images, **kwargs):
            self.prompts.append(json.loads(prompt))
            return TextReply(json.dumps({"summary": "Offline reproduction of the unresolved actor defect",
                "findings": [{"severity": "error", "observation": "Actor contains grass tiles and magenta background",
                              "suggested_fix": "Inspect source art before guessing another atlas crop"}],
                "limitations": ["Fixture based on permissions-002, not live acceptance"]}), 1000, 400)
    executor, text = make_run(tmp_path, proposal(), FINISH, seed=False, controller=controller)
    _, _, _, design_state, design, _, _ = DEMO["load_recorded_inputs"]()
    asyncio.run(DEMO["HELPERS"]["seed_recorded_designer"](executor, design_state, design))
    executor = Executor.open(executor.workspace, services=Services(text=text, sandbox=ScriptedSandbox(),
        godot_mcp=MCPFixture(), vision=ErrorVisual()))
    result = invoke(executor, DeveloperConfig(prompt_version="developer.v4", max_model_turns=3))
    assert result.status == "partial", result.error
    document = report(executor)
    assert document.self_review_completed  # Reviewed, not visually approved.
    assert any("Unresolved visual error" in item for item in document.limitations)
    assert "Host retained unresolved errors" in document.self_review_summary
    assert executor.state.readiness is None


def test_visual_helper_receives_referenced_current_assets_with_budgeted_images(tmp_path):
    executor, text = make_run(tmp_path)
    ref = next(r for r in executor.state.artifacts if r.artifact_id == "art-manifest")
    manifest = json.loads(executor.workspace.read_public(ref.path).read_bytes())
    assets = [a for a in manifest["assets"] if a["record"]["artifact_ref"]["media_type"] == "image/png"]
    paths = [a["request"]["target_path"] for a in assets]
    text.replies = [proposal(script="extends Node2D\n# " + " ".join(paths)), FINISH]
    class AssetVisual(VisualFixture):
        async def generate(self, prompt, *, images, **kwargs):
            data = json.loads(prompt)
            self.prompts.append(data)
            assert len(images) == 3  # One actual preview plus a configured maximum of two assets.
            assert images[0].data == image_bytes()
            for image, description in zip(images[1:], data["source_assets"]):
                asset = next(a for a in assets if a["record"]["asset_id"] == description["asset_id"])
                assert image.data == executor.workspace.read_public(asset["record"]["artifact_ref"]["path"]).read_bytes()
                assert image.source_id == description["image_source_id"]
                assert not description["layout_visually_verified"]
            return TextReply(json.dumps({"summary": "Offline asset routing check", "findings": [],
                                        "limitations": ["No live visual judgment"]}), 1000, 400)
    visual = AssetVisual()
    executor = Executor.open(executor.workspace, services=Services(text=text, sandbox=ScriptedSandbox(),
        godot_mcp=MCPFixture(), vision=visual))
    result = invoke(executor, DeveloperConfig(prompt_version="developer.v4", max_visual_asset_images=2))
    assert result.status == "partial", result.error  # Preserved upstream mock-art gate.
    assert len(visual.prompts) == 1
    assert executor.state.ledger.usage.model_calls == 3
    assert visual.prompts[0]["instruction"] == executor.state.task.instruction
    assert not executor.state.readiness
