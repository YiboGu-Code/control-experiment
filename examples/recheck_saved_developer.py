"""Recheck a saved Developer project with local Godot; no model or image calls."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adaptive_game_generation.roles.developer import DeveloperReport
from adaptive_game_generation.roles.developer.contracts import project_path
from adaptive_game_generation.roles.developer.diagnostics import ENGINE_FAILURE
from adaptive_game_generation.runtime import DockerSandbox, Workspace
from adaptive_game_generation.runtime.commit import digest_path
from adaptive_game_generation.runtime.workspace import inspect_tree, safe_path


def authored_sources(project):
    sources = {}
    for path in project.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(project).as_posix()
        try:
            project_path(relative)
        except ValueError:
            continue
        sources[relative] = path.read_bytes()
    return sources


async def run(saved_run: Path, output: Path, image: str, revision: str | None = None, restore_default_cache=False):
    saved_run, output = saved_run.resolve(), output.absolute()
    if output.resolve().is_relative_to(saved_run):
        raise ValueError("Diagnostic output must be outside the original run directory")
    if output.exists():
        raise ValueError("Use a new output directory; original run files must be preserved")
    reports = [(p, DeveloperReport.model_validate_json(p.read_bytes())) for p in
               (saved_run / "workspace/public/artifacts/developer").glob("*/developer_report.json")]
    reports = [(p, r) for p, r in reports if revision is None or r.game_revision_id == revision]
    if not reports:
        raise ValueError("No matching committed Developer report found")
    report_path, report = max(reports, key=lambda item: item[1].project_ref.version)
    source = safe_path(saved_run / "workspace/public", report.project_ref.path)
    inspect_tree(source)
    original_hash = digest_path(source)
    if original_hash != report.project_ref.sha256:
        raise ValueError("Saved project checksum differs from its Developer report")
    # Pin the installed image, never pull or contact a model endpoint.
    image_id = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                                       text=True, timeout=20).strip()
    output.mkdir(parents=True)
    task_dir = output / "public_task"
    task_dir.mkdir()
    instruction = (saved_run / "instruction.md").read_bytes()
    (task_dir / "instruction.md").write_bytes(instruction)
    (output / "instruction.md").write_bytes(instruction)
    workspace, _ = Workspace.prepare(output / "workspace", task_dir, task_id=report.task_id)
    invocation = workspace.begin("developer", "offline-engine-recheck")
    shutil.copytree(source, invocation.output_path("game"), copy_function=shutil.copyfile)
    project = invocation.output_path("game")
    patch = None
    if restore_default_cache:
        path = project / "project.godot"
        before = path.read_text(encoding="utf-8")
        after = re.sub(r"(?m)^config/(?:project_data_dir_name|use_hidden_project_data_directory)\s*=.*\n?", "", before)
        path.write_text(after, encoding="utf-8")
        patch = {"path": "project.godot", "before": before, "after": after,
                 "reason": "Explicit offline diagnostic patch: restore the default .godot cache; original run unchanged"}
        (output / "diagnostic_patch.json").write_text(json.dumps(patch, indent=2) + "\n", encoding="utf-8")
    expected = authored_sources(project)
    config = report.config
    executable = config.godot_executable
    commands = [("engine_version", (executable, "--headless", "--version")),
                ("import", (executable, "--headless", "--path", "/output/game", "--import")),
                ("ordinary_launch", (executable, "--headless", "--path", "/output/game", "--quit-after", "5"))]
    commands.extend(("scenario:" + s.scenario_id, (executable, "--headless", "--path", "/output/game",
        "--fixed-fps", "60", "--quit-after", str(config.scenario_frames), "--", "--scenario", s.scenario_id))
        for s in report.scenarios[:config.max_scenario_checks])
    backend = DockerSandbox(image_id)
    summary = {"mode": "offline-saved-project-engine-recheck", "models_called": False,
        "images_generated": False, "acceptance_evidence": False, "image_id": image_id,
        "source_run": str(saved_run), "source_report": str(report_path), "game_revision_id": report.game_revision_id,
        "source_project_sha256": original_hash, "diagnostic_patch_applied": patch is not None,
        "steps": [], "status": "not_run"}
    (output / "developer_report_original.json").write_bytes(report_path.read_bytes())
    try:
        for name, argv in commands:
            result = await backend.run(invocation, argv, config.check_timeout_seconds)
            passed = result.returncode == 0 and not ENGINE_FAILURE.search(result.stdout + "\n" + result.stderr)
            step = {"name": name, "argv": argv, "status": "passed" if passed else "failed", **asdict(result)}
            summary["steps"].append(step)
            summary["status"] = step["status"]
            if not passed:
                break
        # Match Developer's post-execution source and protected-Art checks.
        actual = authored_sources(project)
        changed = sorted({p for p, data in expected.items() if actual.get(p) != data} |
                         {p for p in actual if p not in expected and not p.endswith(".uid")})
        modified_art = [a.target_path for a in report.integrated_assets
                        if digest_path(project / a.target_path) != a.artifact_ref.sha256]
        summary["source_integrity"] = {"status": "failed" if changed else "passed", "changed_or_unexpected": changed}
        summary["art_integrity"] = {"status": "failed" if modified_art else "passed", "modified": modified_art}
        if changed or modified_art:
            summary["status"] = "failed"
    except Exception as exc:
        summary.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        summary["source_unchanged"] = digest_path(source) == original_hash
        (output / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="gamecraft-godot:4.5.1")
    parser.add_argument("--revision")
    parser.add_argument("--restore-default-cache", action="store_true",
                        help="Record an explicit patch in the diagnostic copy only; preserve the original run")
    args = parser.parse_args()
    result = asyncio.run(run(args.run, args.output, args.image, args.revision, args.restore_default_cache))
    print(json.dumps({k: result[k] for k in ("status", "game_revision_id", "models_called", "source_unchanged")}, indent=2))
    sys.exit(0 if result["status"] == "passed" else 1)
