"""Developer MCP review demo: real Benchmark instruction, mock art, isolated Godot.

Use --offline-fixture to validate the repair protocol without any online calls.
Use --live yourself to call the configured Developer and visual models.
"""
import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import runpy

from adaptive_game_generation.models import ResourceLimits
from adaptive_game_generation.providers import GatewayConfig, OpenAICompatibleTextProvider
from adaptive_game_generation.roles.developer import Developer, DeveloperConfig
from adaptive_game_generation.runtime import DockerSandbox, DockerGodotMCP, Executor, RuntimeConfig, Services, TextReply, Workspace
from adaptive_game_generation.runtime.workspace import no_links
from adaptive_game_generation.controllers.results import export_run, write_json

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = runpy.run_path(str(ROOT / "examples/developer_demo.py"))
RUNNER = runpy.run_path(str(ROOT / "examples/fixed_loop_demo.py"))


class RecordedFixture:
    """Scripted protocol test, never presented as a real model or visual judgment."""
    def __init__(self, directory, replies):
        self.directory, self.replies, self.call_count = directory, replies, 0

    def input_token_upper_bound(self, prompt, *, images=()):
        return len(json.dumps(OpenAICompatibleTextProvider.messages(prompt, images)).encode()) + 65536 * len(images) + 1024

    async def generate(self, prompt, *, max_output_tokens, timeout, images=()):
        reply = self.replies[self.call_count]
        self.call_count += 1
        folder = self.directory / f"{self.call_count:03}"
        write_json(folder / "request.json", {"mode": "offline-scripted-fixture", "max_output_tokens": max_output_tokens,
            "messages": OpenAICompatibleTextProvider.messages(prompt, images)})
        write_json(folder / "response.json", {"mode": "offline-scripted-fixture", "response": reply,
            "usage": {"input_tokens": 1000, "output_tokens": 1000, "synthetic": True}})
        return TextReply(json.dumps(reply), 1000, 1000)


def fixtures(target):
    def script(scale):
        return ('extends Node2D\nfunc _ready() -> void:\n'
                '\tvar sprite := Sprite2D.new()\n'
                f'\tsprite.texture = load({json.dumps("res://" + target)})\n'
                f'\tsprite.scale = Vector2({scale}, {scale})\n'
                '\tsprite.position = Vector2(320, 350)\n\tadd_child(sprite)\n'
                '\tvar label := Label.new()\n\tlabel.text = "Offline MCP integration fixture"\n'
                '\tlabel.position = Vector2(40, 40)\n\tadd_child(label)\n')
    base = {"status": "partial", "summary": "Create an explicitly incomplete engine inspection fixture",
        "affected_requirement_ids": ["R002"], "implemented_requirement_ids": [],
        "controls": ["No gameplay controls in this integration fixture"], "scenarios": [],
        "remaining_work": ["This protocol fixture does not implement the complete Benchmark task"]}
    first = {**base, "files": [{"path": "project.godot", "content": 'config_version=5\n[application]\nconfig/name="MCP Fixture"\nrun/main_scene="res://Main.tscn"\n[rendering]\nrenderer/rendering_method="gl_compatibility"\n'},
        {"path": "Main.tscn", "content": '[gd_scene load_steps=2 format=3]\n[ext_resource type="Script" path="res://main.gd" id="1"]\n[node name="Main" type="Node2D"]\nscript=ExtResource("1")\n'},
        {"path": "main.gd", "content": script(8)}]}
    second = {**base, "summary": "Apply a scripted scale repair to verify feedback plumbing",
              "files": [{"path": "main.gd", "content": script(1)}]}
    finish = {"status": "finish", "summary": "The scripted repair protocol completed; no real model quality claim",
              "remaining_work": ["The full game remains unimplemented and artwork remains mock"]}
    first_review = {"summary": "Scripted visual feedback: request a sprite scale adjustment",
        "findings": [{"severity": "warning", "observation": "Synthetic finding for the repair-loop test",
                      "suggested_fix": "Set the fixture sprite scale to one"}],
        "limitations": ["No online visual model was called; this response is an explicit fixture"]}
    second_review = {"summary": "Scripted feedback after the repair", "findings": [],
                     "limitations": first_review["limitations"]}
    return [first, second, finish], [first_review, second_review]


async def run(output, *, live=False, offline_fixture=False, env_file=ROOT / ".env"):
    from dotenv import dotenv_values
    if live == offline_fixture:
        raise ValueError("Select exactly one of live models or the explicit offline fixture")
    output = Path(output).absolute()
    no_links(output)
    if output.exists():
        raise ValueError("Use a new output directory")
    output.mkdir(parents=True)
    source, _, instruction, designer_state, design, art_state, art_data = UPSTREAM["load_recorded_inputs"]()
    workspace, task = Workspace.prepare(output / "workspace", RUNNER["DEFAULT_TASK"], task_id=source["task_id"])
    if workspace.read_public("input/instruction.md").read_bytes() != instruction:
        raise ValueError("Recorded upstream instruction differs from the public Benchmark task")
    (output / "instruction.md").write_bytes(instruction)
    values = {**dotenv_values(env_file, interpolate=False), **os.environ, "GAMECRAFT_MCP_ENABLED": "true"}
    settings = await asyncio.to_thread(RUNNER["engine_settings"], values)
    config = DeveloperConfig(prompt_version="developer.v4", max_model_turns=8)
    text_settings, visual_settings = None, None
    if live:
        text_settings = GatewayConfig.from_env(Path(env_file), prefix="CODE_DEVELOPER")
        visual_settings = RUNNER["developer_visual_settings"](GatewayConfig.from_env(Path(env_file), prefix="QA"), values)
        config = config.revised(max_output_tokens=text_settings.max_output_tokens, visual_review_tokens=visual_settings.max_output_tokens)
        config = RUNNER["role_prompt_settings"](config, "CODE_DEVELOPER", values)
    duration = max(18000.0, config.max_model_turns * text_settings.timeout_seconds
        + config.max_mcp_inspections * (visual_settings.timeout_seconds + config.mcp_timeout_seconds) + 1800.0) if live else 18000.0
    limits = ResourceLimits(max_model_calls=11,
        max_input_tokens=int(values.get("FIXED_LOOP_ROLE_MAX_INPUT_TOKENS", "6000000")) if live else 6000000,
        max_output_tokens=max(800000, config.max_model_turns * config.max_output_tokens
                              + config.max_mcp_inspections * config.visual_review_tokens),
        max_tool_calls=8000, max_tool_seconds=duration, max_wall_seconds=duration)
    write_json(output / "configuration.json", {"mode": "live-models/mock-art" if live else "offline-scripted-models/mock-art",
        "engines": settings, "developer": config.model_dump(mode="json"), "upstream": source,
        "budget": limits.model_dump(mode="json"),
        "text_provider": text_settings.public_settings() if live else "scripted fixture",
        "visual_provider": visual_settings.public_settings() if live else "scripted fixture"})
    RUNNER["snapshot_implementation"](output)
    async with AsyncExitStack() as stack:
        if live:
            text = await stack.enter_async_context(OpenAICompatibleTextProvider(text_settings, capture_directory=output / "model_calls/developer"))
            visual = await stack.enter_async_context(OpenAICompatibleTextProvider(visual_settings, capture_directory=output / "model_calls/developer_visual"))
        else:
            from adaptive_game_generation.roles.art_producer import ArtManifest
            manifest = ArtManifest.model_validate_json(art_data["art-manifest"][1])
            replies, reviews = fixtures(manifest.assets[0].request.target_path)
            text = RecordedFixture(output / "model_calls/developer", replies)
            visual = RecordedFixture(output / "model_calls/developer_visual", reviews)
        services = Services(text=text, vision=visual,
            sandbox=DockerSandbox(settings["developer"]["image_id"]),
            godot_mcp=DockerGodotMCP(DockerSandbox(settings["godot_mcp"]["image_id"], cpus=2.0, memory_mb=2048, tmpfs_mb=1024)))
        executor = Executor.create(workspace, task, run_id="developer-mcp-demo", controller="fixed_loop", budget=limits,
            config=RuntimeConfig(role_limits=limits, invocation_timeout_seconds=duration,
                tool_timeout_seconds=max(1200.0, text_settings.timeout_seconds, visual_settings.timeout_seconds) if live else 1200.0),
            services=services)
        await UPSTREAM["seed_recorded_inputs"](executor, designer_state, design, art_state, art_data)
        request = executor.request("developer", "Implement and inspect the public task using the current design and art",
            ("Inspect the assembled game and respond to observed defects",), request_id="developer-mcp-001")
        write_json(output / "state_before.json", executor.state.model_dump(mode="json"))
        write_json(output / "role_input.json", request.model_dump(mode="json"))
        outcome = await executor.execute(request, Developer(config))
        write_json(output / "role_output.json", outcome.model_dump(mode="json"))
        if not executor.state.ledger.termination:
            executor.finish("iteration_limit", "Single-role demonstration ended; this is not full-game acceptance")
        summary = export_run(executor, output, mode="developer-mcp/live" if live else "developer-mcp/offline-fixture")
        summary["online_models_called"] = live
        write_json(output / "summary.json", summary)
        (output / "review.md").write_text(
            "# Developer MCP single-role review\n\n"
            + ("Real online Developer and visual-model calls; recorded mock art.\n\n" if live else
               "Explicit scripted model fixtures; real isolated Godot MCP and screenshots; recorded mock art. No online models were called.\n\n")
            + "The original Benchmark instruction is preserved. The offline fixture tests the repair protocol, not game quality.\n\n"
            "- [Configuration](configuration.json)\n- [Instruction](instruction.md)\n"
            "- [Input state](state_before.json)\n- [Role request](role_input.json)\n- [Role output](role_output.json)\n"
            "- [Final state and ledger](state_final.json)\n- [Summary](summary.json)\n"
            "- [Delivery manifest](delivery/manifest.json)\n- [Tool audit](workspace/.private/tools.jsonl)\n\n"
            "Full model requests/responses are under `model_calls/developer/` and `model_calls/developer_visual/`. "
            "Artifact paths in the role output resolve below `workspace/public/`; they include MCP wire transcripts, "
            "scene/UI/log observations, preview PNGs, visual feedback, build logs and the Developer report. "
            "A self-review is diagnostic only; Player and QA were not run.\n", encoding="utf-8")
        return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--live", action="store_true")
    group.add_argument("--offline-fixture", action="store_true")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    print(json.dumps(asyncio.run(run(**vars(parser.parse_args()))), indent=2))
