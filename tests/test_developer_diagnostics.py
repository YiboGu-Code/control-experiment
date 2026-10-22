"""Actual failed source/log shapes, with explicitly offline correction fixtures."""

import json
from pathlib import Path

import pytest

from adaptive_game_generation.roles.developer import DeveloperConfig
from adaptive_game_generation.roles.developer.contracts import BuildAttempt
from adaptive_game_generation.roles.developer.diagnostics import repair_brief, source_findings
from adaptive_game_generation.runtime import ProcessResult
from test_developer import ScriptedSandbox, invoke, make_run, proposal, report

FIXTURES = Path(__file__).parent / "fixtures/developer_real_images_002"


def test_real_saved_sources_identify_serialization_errors_and_lifetime_hazard():
    sources = json.loads((FIXTURES / "sources.json").read_bytes())
    findings = source_findings(sources)
    resource = [f for f in findings if f["code"] == "resource_method_call"]
    assert len(resource) == 7
    assert {f["line"] for f in resource if f["path"] == "scenes/Gameplay.tscn"} == {97, 126}
    assert sum(f["code"] == "input_event_dictionary" for f in findings) == 4
    lifecycle = [f for f in findings if f["code"] == "scene_lifetime"]
    assert lifecycle and lifecycle[0]["severity"] == "advisory"
    assert "scene_changed" in lifecycle[0]["message"]


def test_scanner_does_not_treat_multiline_property_data_as_code():
    text = '''[gd_scene format=3]
[node name="Root" type="Label"]
text = "line one
add_theme_font_size_override(\\"font_size\\", 40)
line three"
metadata/data = {
"value": [
Vector2(
    1,
    2
)
]}
; add_theme_font_size_override("font_size", 20)
theme_override_font_sizes/font_size = 30
'''
    assert source_findings({"Main.tscn": text}) == []
    assert source_findings({"project.godot": '[input]\na={"events":null}\n'}) == []


def test_unquoted_string_ids_are_flagged_but_numeric_and_quoted_ids_are_allowed():
    assert source_findings({"a.tscn": '[sub_resource type="RectangleShape2D" id=RectangleShape2D_1]\n'})[0]["code"] == "unquoted_resource_id"
    for resource_id in ('"RectangleShape2D_1"', '1'):
        assert not source_findings({"a.tscn": f'[sub_resource type="RectangleShape2D" id={resource_id}]\n'})


def test_multiline_input_event_dictionary_is_also_rejected():
    config = '[input] ; public action configuration\nmove = {\n"deadzone": 0.5,\n"events": [\n{"physical_keycode": 65}\n]\n}\n'
    finding = source_findings({"project.godot": config})
    assert len(finding) == 1 and finding[0]["code"] == "input_event_dictionary"
    assert finding[0]["line"] == 2


def test_repair_brief_focuses_current_errors_and_counts_repeats_across_invocations():
    step = json.loads((FIXTURES / "failed_step.json").read_bytes())
    attempt = BuildAttempt(attempt=1, status="failed", source_digest="fixture", steps=(step,))
    files = json.loads((FIXTURES / "sources.json").read_bytes())
    brief = repair_brief(files, [attempt], {"build_attempts": [attempt.model_dump(mode="json")]})
    assert brief["latest_build_status"] == "failed"
    assert all(d["attempts_with_same_error"] == 2 for d in brief["diagnostics"])
    assert any("has vanished" in d["message"] for d in brief["diagnostics"])
    locations = [p for d in brief["diagnostics"] for p in d["locations"]]
    assert {"path": "scenes/Gameplay.tscn", "line": None} in locations
    assert all("'" not in p["path"] for p in locations)
    assert "scripts/main.gd" in brief["source_excerpts"]
    assert "scenes/Gameplay.tscn" in brief["source_excerpts"]
    assert len(json.dumps(brief)) < 16000


def test_invalid_resource_is_corrected_before_first_engine_run(tmp_path):
    bad = proposal()
    bad["files"][1]["content"] += '[node name="Label" type="Label" parent="."]\nadd_theme_font_size_override("font_size", 32)\n'
    corrected = proposal()
    corrected["files"][1]["content"] += '[node name="Label" type="Label" parent="."]\ntheme_override_font_sizes/font_size = 32\n'
    class GuardedSandbox(ScriptedSandbox):
        async def run(self, workspace, argv, timeout):
            assert len(text.prompts) == 2
            return await super().run(workspace, argv, timeout)
    backend = GuardedSandbox()
    executor, text = make_run(tmp_path, bad, corrected, backend=backend)
    result = invoke(executor, DeveloperConfig(prompt_version="developer.v3"))
    assert result.status == "partial", result.error
    document = report(executor)
    assert document.build_attempts[-1].status == "passed"
    assert len(document.build_attempts) == 1 and document.build_attempts[0].model_turn == 2
    assert "resource_method_call" in str(text.prompts[1]["proposal_feedback"])
    assert text.prompts[1]["rejected_proposal"] is not None
    assert text.prompts[1]["current_project_files"] == {}
    assert all(p["input"]["task"]["instruction"] == executor.state.task.instruction for p in text.prompts)
    assert "SECRET-CREDENTIAL" not in json.dumps(text.prompts) and "HIDDEN-" not in json.dumps(text.prompts)
    assert executor.state.ledger.usage.model_calls == 2


def test_source_errors_exhaust_allowance_without_running_engine_or_publishing(tmp_path):
    bad = proposal()
    bad["files"][0]["content"] += '[input]\nmove={"deadzone":0.5,"events":[{"physical_keycode":65}]}\n'
    backend = ScriptedSandbox()
    executor, text = make_run(tmp_path, bad, bad, backend=backend)
    outcome = invoke(executor, DeveloperConfig(prompt_version="developer.v3", max_model_turns=2))
    assert outcome.status == "failed" and "input_event_dictionary" in outcome.error.message
    assert len(text.prompts) == 2 and not backend.commands
    assert not executor.state.game_revisions


def test_parent_vanished_warning_alone_fails_engine_check_and_is_sent_for_repair(tmp_path):
    warning = 'WARNING: Parent path "./UI/TimelineBar" for node "Marker" has vanished when instantiating: "res://Main.tscn".'
    backend = ScriptedSandbox(ProcessResult(0, "", warning))
    executor, text = make_run(tmp_path, proposal(), proposal(), backend=backend)
    outcome = invoke(executor, DeveloperConfig(prompt_version="developer.v3"))
    assert outcome.status == "partial", outcome.error
    assert [a.status for a in report(executor).build_attempts] == ["failed", "passed"]
    brief = text.prompts[1]["repair_brief"]
    assert brief["diagnostics"][0]["message"] == warning


@pytest.mark.parametrize("version", ["developer.v1", "developer.v2"])
def test_legacy_versions_do_not_enable_new_static_gate(tmp_path, version):
    bad = proposal()
    bad["files"][1]["content"] += 'add_theme_font_size_override("font_size", 32)\n'
    executor, text = make_run(tmp_path, bad, backend=ScriptedSandbox())
    outcome = invoke(executor, DeveloperConfig(prompt_version=version))
    assert outcome.status == "partial"
    assert len(text.prompts) == 1 and "repair_brief" not in text.prompts[0]
