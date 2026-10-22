"""Inspect an existing public game using real MCP/Godot, with zero model calls."""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess

from adaptive_game_generation.runtime import DockerSandbox
from adaptive_game_generation.runtime.godot_mcp import DockerGodotMCP, GodotInspectionRequest
from adaptive_game_generation.runtime.commit import digest_path
from adaptive_game_generation.runtime.workspace import InvocationWorkspace, inspect_tree, no_links

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASK = ROOT / "benchmark_resources/FreedomIntelligence-gamecraft-bench-a433475/tasks/platformer-time-loop"


async def run(output, project, *, task_dir=DEFAULT_TASK, scene="Main.tscn", scenario=None,
              frames=60, image="gamecraft-godot-mcp:4.5.1-mcp3.8.1"):
    output, project, task_dir = Path(output).absolute(), Path(project).absolute(), Path(task_dir).absolute()
    no_links(output)
    inspect_tree(project, public_input=True)
    if output.is_relative_to(project) or output.exists():
        raise ValueError("Use a new output directory outside the inspected game")
    output.mkdir(parents=True)
    instruction = task_dir / "instruction.md"
    no_links(instruction)
    (output / "instruction.md").write_bytes(instruction.read_bytes())
    image_id = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True).strip()
    request = GodotInspectionRequest(scene_path=scene, scenario=scenario, frames=frames)
    job = {**request.model_dump(), "project_sha256": digest_path(project), "execution_authorized": True}
    config = {"request": job, "image_id": image_id, "instruction_sha256": digest_path(instruction),
              "models_called": False, "acceptance_evidence": False}
    (output / "configuration.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    backend = DockerGodotMCP(DockerSandbox(image_id, memory_mb=2048, cpus=2.0, tmpfs_mb=1024))
    work = InvocationWorkspace(project.parent, output, "artifacts/developer/inspection")
    result = await backend.inspect(work, project.name, "inspection", job, 180)
    summary = {k: v for k, v in result.items() if k != "observations"}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--task-dir", type=Path, default=DEFAULT_TASK)
    parser.add_argument("--scene", default="Main.tscn")
    parser.add_argument("--scenario")
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--image", default="gamecraft-godot-mcp:4.5.1-mcp3.8.1")
    print(json.dumps(asyncio.run(run(**vars(parser.parse_args()))), indent=2))
