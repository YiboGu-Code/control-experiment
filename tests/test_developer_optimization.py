"""Offline optimization contracts. Real Benchmark instruction; no live calls."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from adaptive_game_generation.roles.developer import DeveloperConfig, DeveloperProposal
from adaptive_game_generation.roles.developer.contracts import FileEdit, DeveloperContext, BuildAttempt, BuildStep
from adaptive_game_generation.roles.developer.context import ContextWindow, stage_for
from adaptive_game_generation.roles.developer.diagnostics import repair_brief
from adaptive_game_generation.roles.developer.edits import apply_edits, source_sha256
from adaptive_game_generation.runtime import InvalidSubmission, PermissionDenied, ProcessResult
from test_developer import proposal, make_run, invoke, report, ScriptedSandbox

SCRIPT = "extends Node2D\nfunc _ready() -> void\n\tpass\n" + "# retained unrelated implementation\n" * 1800
FIXED = SCRIPT.replace("func _ready() -> void\n", "func _ready() -> void:\n")
PARSE = ProcessResult(1, "", "SCRIPT ERROR: Parse Error: Expected ':'\n at: res://scripts/main.gd:2")


def edit(text=SCRIPT, old="func _ready() -> void\n", new="func _ready() -> void:\n", **changes):
    return FileEdit(path="scripts/main.gd", expected_sha256=source_sha256(text),
                    replacements=({"old_text": old, "new_text": new},)).revised(**changes)


def edit_proposal(**changes):
    result = proposal()
    result["files"] = []
    result["edits"] = [edit().model_dump(mode="json")]
    return {**result, **changes}


def context_read(resource="source/scripts/main.gd", **changes):
    return {"status": "context", "summary": "Read exact source before a bounded repair",
            "reads": [{"resource_id": resource, "start_line": 1, "end_line": 4}], **changes}


def test_exact_edits_preserve_every_other_byte_and_match_against_original():
    source = "alpha\r\nbeta\r\ngamma\r\n"
    request = edit(text=source, old="alpha", new="beta").revised(replacements=(
        {"old_text": "alpha", "new_text": "beta"}, {"old_text": "beta", "new_text": "OMEGA"}))
    files = {"scripts/main.gd": source, "keep.gd": "unchanged"}
    updated = apply_edits(files, (request,))
    assert updated["scripts/main.gd"] == "beta\r\nOMEGA\r\ngamma\r\n"
    assert files["scripts/main.gd"] == source and updated["keep.gd"] == files["keep.gd"]


@pytest.mark.parametrize("text,old,new", [("aaa", "aa", "b"), ("abc", "missing", "b"), ("abc", "a", "a")])
def test_ambiguous_absent_and_noop_edits_are_rejected(text, old, new):
    with pytest.raises(InvalidSubmission):
        apply_edits({"scripts/main.gd": text}, (edit(text, old, new),))


def test_stale_missing_overlapping_and_multi_file_failure_are_atomic():
    files = {"scripts/main.gd": SCRIPT, "other.gd": "untouched"}
    for invalid in (edit(expected_sha256="0" * 64), edit(path="missing.gd"),
        edit().revised(replacements=({"old_text": "func _ready()", "new_text": "A"},
                                    {"old_text": "_ready() -> void", "new_text": "B"}))):
        with pytest.raises(InvalidSubmission):
            apply_edits(files, (edit(text="untouched", old="untouched", new="changed", path="other.gd"), invalid))
        assert files == {"scripts/main.gd": SCRIPT, "other.gd": "untouched"}


@pytest.mark.parametrize("path", ["../secret.gd", "assets/player.png", "demo_outputs/trace.json", ".godot/a.gd", "tests/rubric.json"])
def test_edits_cannot_target_protected_or_private_files(path):
    with pytest.raises((ValidationError, InvalidSubmission, PermissionDenied, ValueError)):
        edit(path=path)


def test_write_edit_delete_collisions_are_rejected():
    request = edit_proposal(files=[{"path": "scripts/main.gd", "content": "bad"}])
    with pytest.raises(ValidationError):
        DeveloperProposal.model_validate(request)
    with pytest.raises(ValidationError):
        DeveloperProposal.model_validate(edit_proposal(delete_paths=["scripts/main.gd"]))


@pytest.mark.parametrize("new,config", [("\x00", DeveloperConfig()),
    ("x" * 1025, DeveloperConfig(max_file_bytes=1024)),
    ("x" * 800, DeveloperConfig(max_source_bytes=500))])
def test_local_edits_retain_binary_file_and_total_source_limits(new, config):
    from adaptive_game_generation.roles.developer import Developer
    context = SimpleNamespace(state=SimpleNamespace(requirements=[SimpleNamespace(requirement_id="R002")]))
    original = "extends Node2D\n"
    files = {f["path"]: f["content"] for f in proposal(script=original)["files"]}
    plan = DeveloperProposal.model_validate(edit_proposal(edits=[edit(original, original, new).model_dump(mode="json")]))
    with pytest.raises(InvalidSubmission, match="source"):
        Developer(config)._validate_patch(context, plan, files, {})
    assert files["scripts/main.gd"] == original


def attempts(*errors):
    return [BuildAttempt(attempt=i + 1, source_digest="fixture", status="failed",
        steps=(BuildStep(name="import", argv=("godot",), status="failed", stderr=error),)) for i, error in enumerate(errors)]


def test_stage_selection_keeps_initial_complex_repeated_and_escalated_work_deep():
    files = {"scripts/main.gd": SCRIPT}
    assert stage_for(files, [], {}, None)[0] == "deep"
    first = attempts(PARSE.stderr)
    assert stage_for(files, first, repair_brief(files, first), None)[0] == "local_repair"
    twice = attempts(PARSE.stderr, PARSE.stderr)
    assert stage_for(files, twice, repair_brief(files, twice), None)[0] == "deep"
    runtime = attempts("SCRIPT ERROR: Invalid access during ghost replay\n at: res://scripts/main.gd:2")
    assert stage_for(files, runtime, repair_brief(files, runtime), None)[0] == "deep"
    types = attempts("SCRIPT ERROR: Parse Error: Cannot infer the type of ghost_state\n at: res://scripts/main.gd:2")
    assert stage_for(files, types, repair_brief(files, types), None)[0] == "deep"
    missing = attempts(PARSE.stderr.replace("scripts/main.gd", "missing.gd"))
    assert stage_for(files, missing, repair_brief(files, missing), None)[0] == "deep"
    assert stage_for(files, first, repair_brief(files, first), None, forced_deep=True)[0] == "deep"


def test_clean_preview_from_before_the_latest_patch_cannot_select_finalization():
    build = BuildAttempt(attempt=2, model_turn=3, source_digest="new", status="passed", steps=())
    record = SimpleNamespace(model_turn=1, project_sha256="old", result={"status": "passed"},
                             visual_review={"findings": []})
    inspection = SimpleNamespace(records=[record])
    assert stage_for({}, [build], {}, inspection)[0] == "deep"
    record.model_turn = 3
    assert stage_for({}, [build], {}, inspection)[0] == "finalize"


def minimal_inputs():
    from test_designer import INSTRUCTION
    return {"task": {"instruction": INSTRUCTION.read_text(encoding="utf-8")}, "requirements": [], "design": "Verified design",
            "art_manifest": None, "asset_packages": [], "previous_report": {"long_history": "H" * 100000}}


def test_context_is_indexed_lossless_on_request_and_cannot_load_unknown_paths():
    window = ContextWindow(DeveloperConfig(auto_source_bytes=20))
    inputs, files = minimal_inputs(), {"scripts/main.gd": SCRIPT, "keep.gd": "extends Node\n"}
    compact, selected, catalog, _ = window.prepare(inputs, files, [], {}, None, [], None, "deep")
    assert compact["task"] == inputs["task"] and "scripts/main.gd" not in selected
    assert len(json.dumps(compact)) < len(json.dumps(inputs))
    window.request(DeveloperContext.model_validate(context_read()))
    _, _, result, _ = window.prepare(inputs, files, [], {}, None, [], None, "deep")
    assert result["read_results"][0]["text"] == "".join(SCRIPT.splitlines(keepends=True)[:4])
    window.request(DeveloperContext.model_validate(context_read(reads=[{"resource_id": "source/scripts/main.gd"}])))
    _, selected, _, _ = window.prepare(inputs, files, [], {}, None, [], None, "deep")
    assert selected["scripts/main.gd"] == SCRIPT and "scripts/main.gd" in window.visible_full_files
    for resource in (".env", "source/../secret", "input/tests/rubric.json", "https://example.test/code"):
        with pytest.raises(InvalidSubmission, match="Unknown"):
            window.request(DeveloperContext.model_validate(context_read(resource)))


def test_context_read_bounds_and_budget_do_not_expand_permission():
    window = ContextWindow(DeveloperConfig(max_context_reads=1, max_context_response_bytes=32))
    window.prepare(minimal_inputs(), {"scripts/main.gd": SCRIPT}, [], {}, None, [], None, "deep")
    with pytest.raises(InvalidSubmission, match="byte budget"):
        window.request(DeveloperContext.model_validate(context_read(reads=[{"resource_id": "source/scripts/main.gd"}])))
    with pytest.raises(InvalidSubmission, match="range"):
        window.request(DeveloperContext.model_validate(context_read(reads=[{"resource_id": "source/scripts/main.gd", "start_line": 1, "end_line": 99999}])))
    window.request(DeveloperContext.model_validate(context_read(reads=[], request_deep_reasoning=True)))
    with pytest.raises(InvalidSubmission, match="allowance"):
        window.request(DeveloperContext.model_validate(context_read()))


def test_unread_full_rewrites_and_large_light_repairs_are_rejected():
    window = ContextWindow(DeveloperConfig())
    plan = DeveloperProposal.model_validate(proposal(script=FIXED))
    files = {"scripts/main.gd": SCRIPT}
    with pytest.raises(InvalidSubmission, match="complete current source"):
        window.validate_scope(plan, files, "deep")
    window.visible_full_files.add("scripts/main.gd")
    with pytest.raises(InvalidSubmission, match="local profile"):
        window.validate_scope(plan, files, "local_repair")
    with pytest.raises(InvalidSubmission, match="Finalization"):
        window.validate_scope(DeveloperProposal.model_validate(edit_proposal()), files, "finalize")


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_developer_reads_then_applies_exact_repair_without_losing_code(tmp_path, controller):
    backend = ScriptedSandbox(PARSE)
    executor, model = make_run(tmp_path, proposal(script=SCRIPT), context_read(), edit_proposal(),
                               backend=backend, controller=controller)
    result = invoke(executor, DeveloperConfig(prompt_version="developer.v3", optimize_execution=True,
                                             auto_source_bytes=1024, max_model_turns=5))
    assert result.status == "partial", result.error  # Recorded upstream Art is mock.
    doc = report(executor)
    assert [c["stage"] for c in doc.optimization_calls] == ["deep", "local_repair", "local_repair"]
    assert executor.workspace.read_public(doc.project_ref.path + "/scripts/main.gd").read_text(encoding="utf-8") == FIXED
    assert len(doc.build_attempts) == 2 and doc.build_attempts[-1].status == "passed"
    assert len(backend.commands) == 6  # version+failed import, then version/import/launch/scenario
    assert executor.state.ledger.usage.model_calls == 3
    assert model.prompts[2]["optimization"]["context"]["read_results"]
    assert model.prompts[0]["input"]["task"]["instruction"] == executor.state.task.instruction
    assert all("HIDDEN-" not in json.dumps(p) and "SECRET-CREDENTIAL" not in json.dumps(p) for p in model.prompts)
    assert all(c["profile"]["configured"] is False for c in doc.optimization_calls)  # Safe legacy-provider fallback.
    assert sum(c["prompt_bytes"] for c in doc.optimization_calls) < sum(c["full_context_prompt_bytes"] for c in doc.optimization_calls)


def test_stale_edit_feedback_escalates_and_retry_uses_original_snapshot(tmp_path):
    bad = edit_proposal(edits=[edit(expected_sha256="0" * 64).model_dump(mode="json")])
    executor, model = make_run(tmp_path, proposal(script=SCRIPT), bad, edit_proposal(), backend=ScriptedSandbox(PARSE))
    result = invoke(executor, DeveloperConfig(prompt_version="developer.v3", optimize_execution=True, max_model_turns=4))
    assert result.status == "partial", result.error
    doc = report(executor)
    assert [c["stage"] for c in doc.optimization_calls] == ["deep", "local_repair", "deep"]
    assert len(doc.build_attempts) == 2 and len(doc.proposal_rejections) == 1
    assert "Stale source digest" in str(model.prompts[2]["proposal_feedback"])


def test_context_calls_consume_the_same_model_budget_before_dispatch(tmp_path):
    from adaptive_game_generation.models import ResourceLimits
    limits = ResourceLimits(max_model_calls=1, max_input_tokens=2000000, max_output_tokens=100000, max_tool_calls=1000)
    executor, model = make_run(tmp_path, context_read("input/design", reads=[{"resource_id": "input/design"}]),
        proposal(), backend=ScriptedSandbox(), budget=limits)
    result = invoke(executor, DeveloperConfig(optimize_execution=True))
    assert result.status == "failed" and result.error.code == "budget_exceeded"
    assert len(model.prompts) == 1 and not executor.state.game_revisions


def test_clean_mcp_finish_uses_light_profile_but_new_code_requires_escalation(tmp_path):
    from test_godot_mcp import setup, FINISH
    executor, model, mcp, visual = setup(tmp_path, proposal(), FINISH)
    visual.prompts.append({"fixture": "make the next feedback clean"})
    result = invoke(executor, DeveloperConfig(prompt_version="developer.v4", optimize_execution=True))
    assert result.status == "partial", result.error
    doc = report(executor)
    assert [c["stage"] for c in doc.optimization_calls] == ["deep", "finalize"]
    assert doc.self_review_completed and doc.mcp_inspections[-1].project_sha256 == doc.project_ref.sha256


@pytest.mark.parametrize("role", ["designer", "art_producer", "player", "qa_reviewer"])
def test_other_roles_cannot_request_developer_reasoning_profiles(tmp_path, role):
    executor, model = make_run(tmp_path, proposal(), backend=ScriptedSandbox())
    async def unauthorized(context):
        await context.tools.generate_text("Attempt a Developer profile", max_output_tokens=10, developer_profile="local_repair")
    outcome = asyncio.run(executor.execute(executor.request(role, "Check scope", ("No expanded permissions",)), unauthorized))
    assert outcome.status == "failed" and outcome.error.code == "permission_denied"
    assert not model.prompts


@pytest.mark.parametrize("kwargs", [{"developer_profile": "custom"}, {"developer_profile": "finalize", "use_vision": True}])
def test_profiles_cannot_select_arbitrary_settings_or_change_visual_calls(tmp_path, kwargs):
    executor, model = make_run(tmp_path, proposal(), backend=ScriptedSandbox())
    async def unauthorized(context):
        await context.tools.generate_text("Unauthorized profile", max_output_tokens=10, **kwargs)
    outcome = asyncio.run(executor.execute(executor.request("developer", "Check scope", ("Fixed profiles only",)), unauthorized))
    assert outcome.status == "failed" and outcome.error.code == "permission_denied"
    assert not model.prompts and executor.state.ledger.usage.model_calls == 0
