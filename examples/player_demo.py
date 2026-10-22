"""Real Player model, rendered play and fresh replay of the recorded Developer game."""

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
import traceback
from pathlib import Path

from adaptive_game_generation.models import ProposedUpdates, ResourceLimits, RoleResult, SharedState
from adaptive_game_generation.providers import GatewayConfig, OpenAICompatibleTextProvider
from adaptive_game_generation.roles.developer import DeveloperReport
from adaptive_game_generation.roles.player import Player, PlayerConfig, PlayerReport
from adaptive_game_generation.runtime import DockerSandbox, Executor, RuntimeConfig, Services, Workspace
from adaptive_game_generation.runtime.commit import digest_path

ROOT = Path(__file__).resolve().parents[1]
DEVELOPER_REVIEW = ROOT / "docs/examples/developer_review"
RECORDED_GAME = ROOT / "examples/inputs/player_developer_game"
HELPERS = runpy.run_path(str(ROOT / "examples/developer_demo.py"))
write_json = HELPERS["write_json"]


def load_recorded_inputs():
    upstream = HELPERS["load_recorded_inputs"]()
    state = SharedState.model_validate_json((DEVELOPER_REVIEW / "state_after.json").read_bytes())
    result = RoleResult.model_validate_json((DEVELOPER_REVIEW / "role_result.json").read_bytes())
    if state.task.instruction.encode() != upstream[2]:
        raise ValueError("Developer instruction differs from the original Benchmark")
    data = {}
    for ref in result.artifact_refs:
        path = DEVELOPER_REVIEW / "deliverables" / ref.path.split("/", 3)[3]
        if ref.kind == "game_project":
            path = RECORDED_GAME
        if digest_path(path) != ref.sha256:
            raise ValueError("Recorded Developer artifact checksum mismatch")
        data[ref.artifact_id] = (ref, path)
    return upstream, state, result, data


async def seed_recorded_inputs(executor, recorded=None):
    upstream, state, original_result, data = recorded or load_recorded_inputs()
    await HELPERS["seed_recorded_inputs"](executor, *upstream[3:])
    async def import_developer(context):
        refs, replacements = [], {r.key: r for r in context.state.artifacts}
        for identity, (original, source) in data.items():
            if identity == "developer-report":
                continue
            relative = original.path.split("/", 3)[3]
            if source.is_dir():
                for path in sorted(source.rglob("*")):
                    if path.is_file():
                        await context.tools.write_bytes(relative + "/" + path.relative_to(source).as_posix(), path.read_bytes())
                logical = context.tools.output_path(relative)
            else:
                logical = await context.tools.write_bytes(relative, source.read_bytes())
            ref = original.revised(path=logical)
            refs.append(ref)
            replacements[ref.key] = ref
        original, source = data["developer-report"]
        document = DeveloperReport.model_validate_json(source.read_bytes())
        document = document.revised(project_ref=replacements[document.project_ref.key], design_ref=context.state.design.document_ref,
            art_manifest_ref=replacements[document.art_manifest_ref.key], integrated_assets=tuple(
                a.revised(artifact_ref=replacements[a.artifact_ref.key]) for a in document.integrated_assets))
        raw = (document.model_dump_json(indent=2) + "\n").encode()
        path = await context.tools.write_bytes("developer_report.json", raw)
        report_ref = original.revised(path=path, sha256=hashlib.sha256(raw).hexdigest())
        refs.append(report_ref)
        replacements[report_ref.key] = report_ref
        game = original_result.proposed_updates.game_revision
        game = game.revised(project_ref=replacements[game.project_ref.key])
        evidence = tuple(e.revised(artifact_refs=tuple(replacements[r.key] for r in e.artifact_refs))
                         for e in original_result.proposed_updates.evidence)
        return RoleResult(role="developer", request_id=context.request.request_id, state_revision=context.request.state_revision,
            status="partial", summary="Imported recorded real Developer game unchanged; only logical references rebased; no new upstream calls",
            artifact_refs=tuple(refs), evidence_refs=tuple(e.evidence_id for e in evidence), proposed_updates=ProposedUpdates(
                requirements=original_result.proposed_updates.requirements, game_revision=game, evidence=evidence))
    request = executor.request("developer", "Import recorded Developer output as Player preconditions",
        ("Preserve every authored project and mock asset byte",), request_id="import-recorded-developer")
    result = await executor.execute(request, import_developer)
    if result.status != "partial":
        raise RuntimeError(f"Developer import failed: {result.error}")


def sandbox_settings(env_file):
    from dotenv import dotenv_values
    values = {**dotenv_values(env_file, interpolate=False), **os.environ}
    selected = values.get("PLAYER_SANDBOX_IMAGE") or "gamecraft-player:4.5.1"
    result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", selected], capture_output=True, text=True, timeout=20)
    image_id = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise RuntimeError("Build the Player image described in docs/player.md before running the demo")
    return {"requested_image": selected, "image_id": image_id, "network": "none", "non_root": True,
        "read_only_root": True, "fresh_process_and_user_data": True, "host_execution_fallback": False,
        "fps": 30, "resolution": [1280, 720], "cpus": 2.0, "memory_mb": 1536, "tmpfs_mb": 512,
        "driver_sha256": digest_path(ROOT / "configs/player/player_driver.py"),
        "harness_sha256": digest_path(ROOT / "configs/player/player_harness.gd")}


async def run_live(output, env_file):
    config = GatewayConfig.from_env(env_file, prefix="PLAYER")
    sandbox = sandbox_settings(env_file)
    recorded = load_recorded_inputs()
    source, source_path, instruction, *_ = recorded[0]
    if output.exists():
        raise ValueError("Review destination must be a new directory")
    output.mkdir(parents=True)
    (output / "instruction.md").write_bytes(instruction)
    write_json(output / "source.json", {**source, "developer_review": "docs/examples/developer_review",
        "project_sha256": recorded[3]["game-project"][0].sha256, "game_source": "examples/inputs/player_developer_game",
        "preconditions": "Recorded real Designer/Art/Developer model outputs; unchanged mock images. No new upstream calls or hidden evaluation input."})
    write_json(output / "provider_config.json", config.public_settings())
    write_json(output / "sandbox_config.json", sandbox)
    workspace, task = Workspace.prepare(output / "workspace", source_path.parent, task_id=source["task_id"], benchmark_snapshot=source["snapshot"])
    task = task.revised(environment_constraints=(
        "Use English for all authored output; the original public instruction is unchanged.",
        "Play the recorded Developer game in a network-disabled Godot 4.5.1 rendering container; all image assets remain mock.",
        "The immutable external observer provides real scene state and screenshots; the text model has no vision input.",))
    player_config = PlayerConfig(max_output_tokens=config.max_output_tokens)
    limits = ResourceLimits(max_model_calls=player_config.max_model_turns, max_input_tokens=3000000,
        max_output_tokens=config.max_output_tokens * player_config.max_model_turns, max_image_calls=0,
        max_tool_calls=2000, max_tool_seconds=3000.0, max_wall_seconds=3300.0)
    async with OpenAICompatibleTextProvider(config, capture_directory=workspace.private / "model_calls") as provider:
        executor = Executor.create(workspace, task, run_id="player-live", controller="fixed_loop", budget=limits,
            config=RuntimeConfig(role_limits=limits, tool_timeout_seconds=max(config.timeout_seconds, 180),
                invocation_timeout_seconds=3300, service_configuration_id=config.configuration_id + ":player:" + sandbox["image_id"]),
            services=Services(text=provider, sandbox=DockerSandbox(image=sandbox["image_id"], cpus=2.0, memory_mb=1536, tmpfs_mb=512)))
        await seed_recorded_inputs(executor, recorded)
        before = executor.state
        write_json(output / "state_before.json", before.model_dump(mode="json"))
        request = executor.request("player", "Play the Time Loop game using actual inputs and investigate reproducible behavior",
            ("Explore the ordinary title/menu/game path and a meaningful gameplay scenario such as near_rewind",
             "Capture executed input traces and render screenshots, then replay each trace twice from a fresh process and empty save",
             "Report frame-level state/pixel agreement and disagreement with honest coverage limits; do not modify the game or make QA verdicts"),
            request_id="player-initial")
        write_json(output / "role_request.json", request.model_dump(mode="json"))
        async def invoke_player(context):
            try:
                return await Player(player_config)(context)
            except Exception as exc:
                # Private, secret-free diagnostics: no exception message or local values.
                write_json(workspace.private / "player_error.json", {"type": type(exc).__name__,
                    "winerror": getattr(exc, "winerror", None), "errno": getattr(exc, "errno", None),
                    "frames": [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
                               for f in traceback.extract_tb(exc.__traceback__)]})
                raise
        result = await executor.execute(request, invoke_player)
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
        target = output / "deliverables" / ref.path.split("/", 3)[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(workspace.read_public(ref.path).read_bytes())
    report_path = output / "deliverables/player_report.json"
    report = PlayerReport.model_validate_json(report_path.read_bytes()) if report_path.exists() else None
    captures = [s for s in report.sessions if s.trace_ref] if report else []
    comparisons = list(report.comparisons) if report else []
    checks = {
        "original_instruction_unchanged": source_path.read_bytes() == instruction == workspace.read_public("input/instruction.md").read_bytes(),
        "real_model_completed": bool(responses) and len(responses) == state.ledger.usage.model_calls and all(r["choices"][0]["finish_reason"] == "stop" for r in responses),
        "observations_committed": state.state_revision == 4 and report is not None,
        "real_trace_and_screenshots": bool(captures) and all(s.status == "passed" and len(s.screenshot_refs) >= 2 for s in captures),
        "two_fresh_replays_per_capture": bool(captures) and len(comparisons) == len(captures) * player_config.replay_runs,
        "all_sessions_executed": report is not None and all(s.status == "passed" for s in report.sessions),
        "model_concluded_from_observations": report is not None and bool(report.decisions) and report.decisions[-1].action == "finish",
        "published_game_unchanged": state.game_revisions == before.game_revisions and digest_path(workspace.read_public(state.game_revisions[-1].project_ref.path)) == recorded[3]["game-project"][0].sha256,
        "no_qa_or_issue_mutation": state.requirements == before.requirements and state.issues == before.issues and state.readiness == before.readiness,
        "mock_art_unchanged": state.assets == before.assets and state.ledger.usage.image_calls == 0,
        "usage_matches_provider": state.ledger.usage.token_usage_complete and state.ledger.usage.input_tokens == sum(r["usage"]["prompt_tokens"] for r in responses) and state.ledger.usage.output_tokens == sum(r["usage"]["completion_tokens"] for r in responses),
        "english_model_output": bool(responses) and all(not re.search(r"[\u3400-\u9fff]", r["choices"][0]["message"]["content"]) for r in responses),
    }
    summary = {"mode": "live-text/real-rendered-godot/recorded-mock-art", "status": "completed" if all(checks.values()) else "failed",
        "role_status": result.status.value, "model": config.model, "model_calls": state.ledger.usage.model_calls,
        "captured_traces": len(captures), "replay_matches": sum(c.status == "passed" for c in comparisons),
        "replay_mismatches": sum(c.status == "failed" for c in comparisons), "replay_incomplete": sum(c.status == "not_run" for c in comparisons),
        "usage": state.ledger.usage.model_dump(mode="json"), "checks": checks, "error": result.error.model_dump(mode="json") if result.error else None}
    write_json(output / "summary.json", summary)
    lines = ["# Player review: Time Loop", "", f"Integration: **{summary['status']}**; Player status: **{result.status.value}**.", "",
        f"Actual traces: {len(captures)}. Exact fresh replay matches: {summary['replay_matches']}; mismatches: {summary['replay_mismatches']}.", "",
        "A successful integration check means the Player pipeline ran correctly. It does not mean that the game passes QA or that every replay matches.", "",
        "## Inputs", "", "- [Original Benchmark instruction](instruction.md)", "- [Source provenance](source.json)",
        "- [Role request](role_request.json)", "- [Complete input state](state_before.json)",
        "- [Public model settings](provider_config.json)", "- [Immutable rendering image](sandbox_config.json)", "",
        "## Complete model I/O", ""]
    for directory in sorted((output / "model_calls").iterdir()):
        for name in ("request.json", "prompt.json", "response.json", "response.txt", "metadata.json"):
            if (directory / name).exists():
                lines.append(f"- [Call {directory.name}: {name}](model_calls/{directory.name}/{name})")
    lines.extend(["", "## Outputs", "", "- [Summary](summary.json)", "- [Role result](role_result.json)",
        "- [Committed state and ledger](state_after.json)", "- [Complete tool audit](tool_audit.jsonl)",
        "- [Player handoff](deliverables/player_handoff.md)", "- [Structured sessions and frame comparisons](deliverables/player_report.json)", ""])
    for session in report.sessions if report else []:
        lines.append(f"### {session.session_id}: {session.status}")
        lines.append("")
        refs = [session.log_ref, *([session.trace_ref] if session.trace_ref else []), *session.screenshot_refs, *session.observation_refs]
        for ref in refs:
            relative = ref.path.split("/", 3)[3]
            lines.append(f"- [{relative}](deliverables/{relative})")
        lines.append("")
    lines.extend(["All screenshots are real rendered frames. The text model received scene observations and image references, not image pixels. Art textures remain the original mock assets. No hidden evaluator inputs or official scores are involved.", ""])
    (output / "review.md").write_bytes("\n".join(lines).encode())
    return summary


def run_demo(output, *, env_file=ROOT / ".env"):
    return asyncio.run(run_live(output, env_file))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    result = run_demo(args.output, env_file=args.env_file)
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "completed" else 1)
