"""Failures found in the user's opt-001 run; all verification is offline."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from adaptive_game_generation.roles.developer import DeveloperConfig, DeveloperContext, DeveloperProposal
from adaptive_game_generation.roles.developer.context import ContextWindow, stage_for
from adaptive_game_generation.roles.developer.contracts import BuildAttempt
from adaptive_game_generation.roles.developer.diagnostics import source_findings
from adaptive_game_generation.runtime import InvalidSubmission
from test_developer_optimization import minimal_inputs, SCRIPT, FIXED, edit_proposal


def request(*ids, **kwargs):
    return DeveloperContext(status="context", summary="Read verified current modules", reads=[{"resource_id": k} for k in ids], **kwargs)


def prepare(window, files):
    return window.prepare(minimal_inputs(), files, [], {}, None, [], None, "deep")


def test_three_read_batches_keep_earlier_full_source_and_allow_repeat_at_exhaustion():
    files = {"scripts/main.gd": SCRIPT, "ui.gd": "extends Control", "state.gd": "extends Node"}
    window = ContextWindow(DeveloperConfig(auto_source_bytes=1))
    prepare(window, files)
    for path in files:
        window.request(request("source/" + path))
        prepare(window, files)
    assert window.read_count == 3
    window.request(request("source/scripts/main.gd"))
    _, selected, catalog, _ = prepare(window, files)
    assert selected == files and catalog["reads_left"] == 0 and not catalog["evicted_resources"]
    plan = DeveloperProposal.model_validate(edit_proposal(files=[{"path": "scripts/main.gd", "content": FIXED}], edits=[]))
    window.validate_scope(plan, files, "deep")  # The actual run incorrectly rejected this rewrite twice.


def test_retained_sources_refresh_hashes_after_patch_without_rereading_other_files():
    window = ContextWindow(DeveloperConfig(auto_source_bytes=1))
    prepare(window, {"scripts/main.gd": SCRIPT, "ui.gd": "old"})
    window.request(request("source/scripts/main.gd", "source/ui.gd"))
    _, files, _, _ = prepare(window, {"scripts/main.gd": FIXED, "ui.gd": "old"})
    assert files == {"scripts/main.gd": FIXED, "ui.gd": "old"} and window.read_count == 1
    _, files, catalog, _ = prepare(window, {"scripts/main.gd": FIXED})
    assert files == {"scripts/main.gd": FIXED} and catalog["evicted_resources"][0]["resource_id"] == "source/ui.gd"


def test_small_auto_loaded_modules_do_not_disappear_after_first_build():
    window = ContextWindow(DeveloperConfig(auto_source_bytes=64))
    files = {"project.godot": "entry", "state.gd": "small state"}
    prepare(window, files)
    build = BuildAttempt(attempt=1, model_turn=1, status="passed", source_digest="x", steps=())
    _, selected, _, _ = window.prepare(minimal_inputs(), files, [build], {}, None, [], None, "deep")
    assert selected == files and window.read_count == 0


def test_retained_budget_is_atomic_and_explicit_release_does_not_remove_source():
    window = ContextWindow(DeveloperConfig(auto_source_bytes=1, max_context_response_bytes=20))
    files = {"a.gd": "a" * 15, "b.gd": "b" * 15}
    prepare(window, files)
    window.request(request("source/a.gd"))
    with pytest.raises(InvalidSubmission, match="byte budget"):
        window.request(request("source/b.gd"))
    assert [r.resource_id for r in window.pending] == ["source/a.gd"] and window.read_count == 1
    window.request(request("source/b.gd", drop_resources=["source/a.gd"]))
    _, selected, catalog, _ = prepare(window, files)
    assert selected == {"b.gd": "b" * 15} and "source/a.gd" in window.resources
    assert catalog["retained_bytes"] <= 20
    # Growth invalidates only over-budget retained data, never silently claims it is loaded.
    _, _, catalog, _ = prepare(window, {"a.gd": "a" * 15, "b.gd": "b" * 30})
    assert catalog["evicted_resources"] and catalog["retained_bytes"] == 0


def test_exhausted_preview_can_finalize_honestly_but_not_implement_in_low_mode():
    build = BuildAttempt(attempt=1, model_turn=3, status="passed", source_digest="x", steps=())
    record = SimpleNamespace(model_turn=3, project_sha256="x", result={"status": "passed"},
        visual_review={"findings": [{"severity": "error", "observation": "Source art remains invalid"}]})
    inspection = SimpleNamespace(records=[record])
    assert stage_for({}, [build], {}, inspection)[0] == "deep"
    assert stage_for({}, [build], {}, inspection, inspections_exhausted=True)[0] == "finalize"
    assert stage_for({}, [build], {}, inspection, inspections_exhausted=True, forced_deep=True)[0] == "deep"
    record.model_turn = 2
    assert stage_for({}, [build], {}, inspection, inspections_exhausted=True)[0] == "deep"


def test_draw_wall_clock_fails_static_check_but_comments_strings_and_logging_do_not():
    text = 'extends Node2D\nfunc _draw_exit_crystal():\n var pulse = sin(Time.get_ticks_msec() * 0.005)\n draw_circle(Vector2.ZERO, pulse, Color.WHITE)\n'
    findings = source_findings({"game.gd": text})
    assert findings[0]["code"] == "nondeterministic_render_clock" and findings[0]["severity"] == "error"
    for source in (text.replace('Time.get_ticks_msec()', 'simulation_tick'),
        'func _draw():\n # Time.get_ticks_msec()\n print(Time.get_ticks_msec())\n var s="Time.get_ticks_msec()"\n',
        'var documentation = """\nfunc _draw():\n var pulse = Time.get_ticks_msec()\n"""\n'):
        assert not source_findings({"game.gd": source})


@pytest.mark.parametrize("kind,setting", [("TextureRect", "expand_mode = TextureRect.EXPAND_IGNORE_SIZE"),
                                          ("TextureButton", "ignore_texture_size = true")])
def test_native_ui_dimensions_need_the_correct_control_setting(kind, setting):
    source = f'func menu():\n var emblem := {kind}.new()\n emblem.size = Vector2(64, 64)\n'
    found = source_findings({"ui.gd": source})
    assert found[0]["code"] == "native_texture_control_size" and found[0]["severity"] == "advisory"
    assert not source_findings({"ui.gd": source + f' emblem.{setting}\n'})


@pytest.mark.parametrize("mode", ["whole", "regions"])
def test_single_object_image_prompt_does_not_request_a_character_or_animation_sheet(mode):
    from test_image_provider import job
    from adaptive_game_generation.providers.openai_image import OpenAICompatibleImageProvider
    from adaptive_game_generation.models.asset_pipeline import AssetLayout
    service = SimpleNamespace(config=SimpleNamespace(transparency="chroma_key"))
    layout = AssetLayout(kind="sprite", mode=mode, regions=(
        [{"name": "closed", "x": 2, "y": 1, "width": 20, "height": 12}] if mode == "regions" else []))
    single = job().revised(prompt="One closed metal door alone.", columns=1, rows=1,
        frame_names=("closed",), layout=layout)
    prompt = OpenAICompatibleImageProvider._image_prompt(service, single)
    assert "exactly ONE instance" in prompt and "do not add a character" in prompt
    assert "only the requested character poses" not in prompt and "isolated actor animation" not in prompt
    assert "columns and" not in prompt and "across animation frames" not in prompt
    assert "No text of any language" in prompt and "#FF00FF" in prompt
    if mode == "regions":
        assert '"x": 2' in prompt and '"width": 20' in prompt


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_lightweight_finalization_retains_visual_errors_in_both_controllers(tmp_path, controller):
    from test_godot_mcp import setup, FINISH, VisualFixture, TextReply, invoke, report, proposal
    from adaptive_game_generation.runtime import Executor, Services
    from test_developer import ScriptedSandbox
    class ErrorVisual(VisualFixture):
        async def generate(self, prompt, *, images, **kwargs):
            return TextReply(json.dumps({"summary": "Unresolved source art defect", "findings": [
                {"severity": "error", "observation": "Actor contains background tiles",
                 "suggested_fix": "Replace the rejected source art"}], "limitations": ["Offline fixture"]}), 100, 100)
    executor, text, mcp, _ = setup(tmp_path, proposal(), FINISH, controller=controller)
    executor = Executor.open(executor.workspace, services=Services(text=text, sandbox=ScriptedSandbox(),
        godot_mcp=mcp, vision=ErrorVisual()))
    outcome = invoke(executor, DeveloperConfig(prompt_version="developer.v4", optimize_execution=True,
        max_mcp_inspections=1, max_model_turns=2))
    assert outcome.status == "partial", outcome.error
    doc = report(executor)
    assert [c["stage"] for c in doc.optimization_calls] == ["deep", "finalize"]
    assert doc.self_review_completed and any("Unresolved visual error" in item for item in doc.limitations)
    assert executor.state.readiness is None


def test_successful_deep_repair_allows_later_clean_finalization(tmp_path):
    from test_godot_mcp import setup, FINISH, invoke, report, proposal
    request_deep = {"status": "context", "summary": "A gameplay repair needs full reasoning",
                    "reads": [{"resource_id": "source/scripts/main.gd"}], "request_deep_reasoning": True}
    executor, _, _, _ = setup(tmp_path, proposal(), request_deep,
        proposal(script="extends Node2D\n# Repaired after deep escalation\n"), FINISH)
    outcome = invoke(executor, DeveloperConfig(prompt_version="developer.v4", optimize_execution=True, max_model_turns=4))
    assert outcome.status == "partial", outcome.error  # Mock artwork is preserved as a limitation.
    doc = report(executor)
    assert [c["stage"] for c in doc.optimization_calls] == ["deep", "deep", "deep", "finalize"]
    assert doc.self_review_completed


def test_stream_disconnect_is_a_provider_failure_and_never_a_completed_qa_reply(tmp_path):
    httpx = pytest.importorskip("httpx")
    from adaptive_game_generation.providers import OpenAICompatibleTextProvider
    from adaptive_game_generation.providers.openai_compatible import ProviderFailure
    from test_provider import settings
    calls = []
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunk = {"id": "offline", "object": "chat.completion.chunk", "created": 1, "model": "fixture",
                     "choices": [{"index": 0, "delta": {"content": '{"ready":'}, "finish_reason": None}]}
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            raise httpx.RemoteProtocolError("offline broken connection")
    def handle(request):
        calls.append(request)
        return httpx.Response(200, stream=Interrupted(), headers={"content-type": "text/event-stream"})
    async def run():
        async with OpenAICompatibleTextProvider(settings(stream=True), capture_directory=tmp_path / "calls",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))) as provider:
            with pytest.raises(ProviderFailure, match="unconfirmed"):
                await provider.generate("{}", max_output_tokens=200, timeout=10)
    asyncio.run(run())
    meta = json.loads((tmp_path / "calls/001/metadata.json").read_bytes())
    assert len(calls) == 1 and meta["error_code"] == "provider_stream_transport_error"
    assert meta["transport_error_type"] == "RemoteProtocolError" and meta["stream_chunks"] == 1
    assert "usage" not in meta and not (tmp_path / "calls/001/response.json").exists()
    assert (tmp_path / "calls/001/response_chunks.jsonl").is_file()
