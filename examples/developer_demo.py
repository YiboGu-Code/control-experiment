"""Real Developer model and isolated Godot checks, with recorded mock Art inputs."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
from pathlib import Path

from adaptive_game_generation.models import ProposedUpdates, ResourceLimits, RoleResult, SharedState
from adaptive_game_generation.providers import GatewayConfig, OpenAICompatibleTextProvider
from adaptive_game_generation.roles.art_producer import ArtManifest
from adaptive_game_generation.roles.developer import Developer, DeveloperConfig, DeveloperReport
from adaptive_game_generation.runtime import DockerSandbox, Executor, RuntimeConfig, Services, Workspace

ROOT = Path(__file__).resolve().parents[1]
ART_REVIEW = ROOT / "docs/examples/art_producer_review"
HELPERS = runpy.run_path(str(ROOT / "examples/art_producer_demo.py"))
write_json = HELPERS["write_json"]


def load_recorded_inputs():
    source, path, instruction, designer_state, design = HELPERS["load_inputs"]()
    art_state = SharedState.model_validate_json((ART_REVIEW / "state_after.json").read_bytes())
    result = RoleResult.model_validate_json((ART_REVIEW / "role_result.json").read_bytes())
    if art_state.task.instruction.encode("utf-8") != instruction or art_state.requirements != designer_state.requirements:
        raise ValueError("Recorded Art task/requirements differ from the original Designer input")
    data = {}
    for ref in result.artifact_refs:
        raw = (ART_REVIEW / "deliverables" / ref.path.split("/", 3)[3]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref.sha256:
            raise ValueError("Recorded Art artifact checksum mismatch")
        data[ref.artifact_id] = (ref, raw)
    return source, path, instruction, designer_state, design, art_state, data


async def seed_recorded_inputs(executor, designer_state, design, art_state, data):
    await HELPERS["seed_recorded_designer"](executor, designer_state, design)
    async def import_art(context):
        refs, replacements = [], {}
        for artifact_id, (original, raw) in data.items():
            if artifact_id in ("art-manifest", "art-handoff"):
                continue
            path = await context.tools.write_bytes(original.path.split("/", 3)[3], raw)
            ref = original.revised(path=path)
            refs.append(ref)
            replacements[original.key] = ref
        original_manifest = ArtManifest.model_validate_json(data["art-manifest"][1])
        assets = tuple(a.revised(record=a.record.revised(artifact_ref=replacements[a.record.artifact_ref.key]),
            license_ref=replacements[a.license_ref.key] if a.license_ref else None) for a in original_manifest.assets)
        manifest = original_manifest.revised(assets=assets, design_ref=context.state.design.document_ref)
        original = data["art-manifest"][0]
        raw = (manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
        path = await context.tools.write_bytes("asset_manifest.json", raw)
        refs.append(original.revised(path=path, sha256=hashlib.sha256(raw).hexdigest()))
        # Retain the original readable handoff verbatim as an imported source document.
        original, raw = data["art-handoff"]
        path = await context.tools.write_bytes("original_art_handoff.md", raw)
        refs.append(original.revised(path=path))
        return RoleResult(request_id=context.request.request_id, role="art_producer", state_revision=context.request.state_revision,
            status="partial", summary="Imported recorded Art assets; images remain mock. Only artifact references were rebased; no new Art model or image calls",
            artifact_refs=tuple(refs), proposed_updates=ProposedUpdates(assets=tuple(a.record for a in assets)))
    request = executor.request("art_producer", "Import the recorded real Art plan and mock image outputs as Developer preconditions",
        ("Preserve asset bytes and source metadata; rebase only workspace references",), request_id="import-recorded-art")
    outcome = await executor.execute(request, import_art)
    if outcome.status != "partial":
        raise RuntimeError("Cannot import recorded Art precondition")


def sandbox_settings(env_file):
    from dotenv import dotenv_values
    values = {**dotenv_values(env_file, interpolate=False), **os.environ}
    selected = values.get("DEVELOPER_SANDBOX_IMAGE") or "gamecraft-godot:4.5.1"
    result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", selected],
                            capture_output=True, text=True, timeout=20)
    image_id = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise RuntimeError("A preloaded isolated Godot image is required; see docs/developer.md")
    return {"requested_image": selected, "image_id": image_id, "network": "none", "non_root": True,
            "read_only_root": True, "host_execution_fallback": False}


async def run_live(output, env_file):
    config = GatewayConfig.from_env(env_file, prefix="CODE_DEVELOPER")
    sandbox = sandbox_settings(env_file)
    source, source_path, instruction, designer_state, design, art_state, art_data = load_recorded_inputs()
    if output.exists():
        raise ValueError("Review destination must be a new directory")
    output.mkdir(parents=True)
    (output / "instruction.md").write_bytes(instruction)
    (output / "designer_input.json").write_bytes(design)
    (output / "original_art_manifest.json").write_bytes(art_data["art-manifest"][1])
    write_json(output / "source.json", {**source, "designer": {
        "source": "docs/examples/designer_review/design.json", "sha256": hashlib.sha256(design).hexdigest()},
        "art": {"source": "docs/examples/art_producer_review/deliverables/asset_manifest.json",
                "sha256": hashlib.sha256(art_data["art-manifest"][1]).hexdigest(), "images": "Previously generated mock placeholders"},
        "preconditions": "Prior real Designer and Art plans are imported; only references are rebased. No new upstream model or image calls."})
    write_json(output / "provider_config.json", config.public_settings())
    write_json(output / "sandbox_config.json", sandbox)
    workspace, task = Workspace.prepare(output / "workspace", source_path.parent,
        task_id=source["task_id"], benchmark_snapshot=source["snapshot"])
    task = task.revised(environment_constraints=(
        "Use English for all authored output. The original public instruction is preserved verbatim.",
        "Godot 4.5.1 is available in a network-disabled container. The staged project is /output/game; deployment target remains /workspace/game.",
        "Recorded Designer and Art inputs are preconditions; all supplied image assets are mock placeholders, not finished art.",
        "Treat the original up-to-four-loops requirement as at most three recorded ghosts plus the currently controlled player loop; explain this choice where the Designer draft differs.",
        "Player will supply actual input traces and replay evidence later. Implement deterministic named gameplay scenarios, including near_rewind and near_victory.",
        "List near_rewind and near_victory first in scenarios so the two bounded scenario launch checks exercise these entries.",
    ))
    developer_config = DeveloperConfig(max_output_tokens=config.max_output_tokens)
    limits = ResourceLimits(max_model_calls=developer_config.max_model_turns, max_input_tokens=1500000,
        max_output_tokens=config.max_output_tokens * developer_config.max_model_turns, max_image_calls=0,
        max_tool_calls=600, max_tool_seconds=config.timeout_seconds * 3 + 600.0,
        max_wall_seconds=config.timeout_seconds * 3 + 720.0)
    async with OpenAICompatibleTextProvider(config, capture_directory=workspace.private / "model_calls") as provider:
        executor = Executor.create(workspace, task, run_id="developer-live", controller="fixed_loop", budget=limits,
            config=RuntimeConfig(role_limits=limits, tool_timeout_seconds=config.timeout_seconds,
                invocation_timeout_seconds=config.timeout_seconds * 3 + 720.0,
                service_configuration_id=config.configuration_id + ":godot:" + sandbox["image_id"] + ":recorded-mock-art"),
            services=Services(text=provider, sandbox=DockerSandbox(image=sandbox["image_id"], memory_mb=1536, cpus=2.0)))
        await seed_recorded_inputs(executor, designer_state, design, art_state, art_data)
        before = executor.state
        write_json(output / "state_before.json", before.model_dump(mode="json"))
        request = executor.request("developer",
            "Implement the Time Loop Godot game from the original task, Designer specification and supplied Art package",
            ("Implement the playable title/chapter/menu/level/reset/result flows and twenty-four levels across four chapters",
             "Implement real 30-second rewind, deterministic recorded ghosts, temporal interactions and completion gating",
             "Integrate the supplied asset bytes, implement near_rewind and near_victory using the real game systems, and pass isolated import/startup checks",
             "Record remaining mock-art, gameplay verification and Player trace work without claiming QA acceptance"), request_id="developer-initial")
        write_json(output / "role_request.json", request.model_dump(mode="json"))
        result = await executor.execute(request, Developer(developer_config))
        state = executor.state
    write_json(output / "role_result.json", result.model_dump(mode="json"))
    write_json(output / "state_after.json", state.model_dump(mode="json"))
    (output / "tool_audit.jsonl").write_bytes((workspace.private / "tools.jsonl").read_bytes())
    shutil.copytree(workspace.private / "model_calls", output / "model_calls")
    responses = []
    for directory in sorted((output / "model_calls").iterdir()):
        if (directory / "request.json").exists():
            body = json.loads((directory / "request.json").read_text(encoding="utf-8"))
            write_json(directory / "prompt.json", json.loads(body["messages"][-1]["content"]))
        if (directory / "response.json").exists():
            responses.append(json.loads((directory / "response.json").read_text(encoding="utf-8")))
    for ref in result.artifact_refs:
        original = workspace.read_public(ref.path)
        target = output / "deliverables" / ref.path.split("/", 3)[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        if original.is_dir():
            shutil.copytree(original, target)
        else:
            target.write_bytes(original.read_bytes())
    report_path = output / "deliverables/developer_report.json"
    report = DeveloperReport.model_validate_json(report_path.read_bytes()) if report_path.exists() else None
    current = state.game_revisions[-1] if state.game_revisions else None
    checks = {
        "original_instruction_unchanged": source_path.read_bytes() == instruction == workspace.read_public("input/instruction.md").read_bytes(),
        "actual_model_completed": bool(responses) and len(responses) == state.ledger.usage.model_calls and all(r["choices"][0]["finish_reason"] == "stop" for r in responses),
        "game_revision_committed": current is not None and state.state_revision == 3,
        "real_basic_build_passed": current is not None and current.build_status == "passed",
        "upstream_artifacts_unchanged": state.design == before.design and state.assets == before.assets,
        "no_qa_or_play_claims": state.readiness is None and all(r.verification is None for r in state.requirements) and all(e.kind == "build_check" for e in state.evidence),
        "mock_art_preserved": report is not None and bool(report.integrated_assets) and all(a.is_mock for a in report.integrated_assets) and result.status == "partial",
        "no_developer_image_calls": state.ledger.usage.image_calls == 0,
        "usage_matches_provider": state.ledger.usage.token_usage_complete and
            state.ledger.usage.input_tokens == sum(r["usage"]["prompt_tokens"] for r in responses) and
            state.ledger.usage.output_tokens == sum(r["usage"]["completion_tokens"] for r in responses),
        "english_output_no_cjk": bool(responses) and all(not re.search(r"[\u3400-\u9fff]", r["choices"][0]["message"]["content"]) for r in responses),
    }
    summary = {"mode": "live-text/real-isolated-godot/recorded-mock-art", "status": "completed" if all(checks.values()) else "failed",
        "role_status": result.status.value, "model": config.model, "game_revision": current.revision_id if current else None,
        "build_status": current.build_status.value if current else "not_run", "model_calls": state.ledger.usage.model_calls,
        "build_attempts": len(report.build_attempts) if report else 0, "integrated_assets": len(report.integrated_assets) if report else 0,
        "source_files": len(report.changed_files) if report else 0, "usage": state.ledger.usage.model_dump(mode="json"),
        "checks": checks, "error": result.error.model_dump(mode="json") if result.error else None}
    write_json(output / "summary.json", summary)
    lines = ["# Developer review: Time Loop", "", f"Integration check: **{summary['status']}**. Role status: **{result.status.value}**. Basic build: **{summary['build_status']}**.", "",
        "The Developer called the real configured model. Godot checks use an actual network-disabled container. Upstream Designer/Art plans come from their recorded real calls; imported images remain mock placeholders. No hidden evaluation files are imported.", "",
        "## Inputs", "", "- [Original Benchmark instruction](instruction.md)", "- [Source and upstream provenance](source.json)",
        "- [Designer input](designer_input.json)", "- [Original Art manifest](original_art_manifest.json)", "- [Role request](role_request.json)",
        "- [State before Developer, with rebased references](state_before.json)", "- [Non-secret model configuration](provider_config.json)",
        "- [Isolated engine configuration and immutable image ID](sandbox_config.json)", "", "## Complete model I/O", ""]
    for directory in sorted((output / "model_calls").iterdir()):
        for name in ("request.json", "prompt.json", "response.json", "response.txt", "metadata.json"):
            if (directory / name).exists():
                lines.append(f"- [Call {directory.name}: {name}](model_calls/{directory.name}/{name})")
    lines.extend(["", "## Outputs", "", "- [Role result](role_result.json)", "- [Committed state and ledger](state_after.json)",
        "- [Summary and checks](summary.json)", "- [Tool audit](tool_audit.jsonl)"])
    if report:
        lines.extend(["- [Developer handoff and controls](deliverables/developer_handoff.md)", "- [Structured Developer report](deliverables/developer_report.json)",
            "- [Actual build diagnostics, including repair attempts](deliverables/build_log.json)", "- [Godot project](deliverables/game/project.godot)",
            "- [Main scene](deliverables/game/Main.tscn)"])
    lines.extend(["", "Basic import/startup checks do not demonstrate full gameplay or replay success. Player and QA work is still required. Mock images are not finished art.", ""])
    (output / "review.md").write_bytes("\n".join(lines).encode("utf-8"))
    return summary


def run_demo(output, *, env_file=ROOT / ".env"):
    return asyncio.run(run_live(output, env_file))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    summary = run_demo(args.output, env_file=args.env_file)
    print(json.dumps(summary))
    raise SystemExit(0 if summary["status"] == "completed" else 1)
