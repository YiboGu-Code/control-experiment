"""Fresh five-role Fixed Loop: configured text/image models + real isolated Godot."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import platform
from importlib.metadata import version
from contextlib import AsyncExitStack
from pathlib import Path

from adaptive_game_generation.controllers import AdaptiveScheduler, FixedLoop, FixedLoopConfig, ModelScheduler, SchedulerConfig
from adaptive_game_generation.controllers.results import export_run, write_json
from adaptive_game_generation.models import ResourceLimits, Role
from adaptive_game_generation.providers import (
    GatewayConfig, OpenAICompatibleTextProvider, create_image_provider, image_config_from_env,
)
from adaptive_game_generation.providers.mock_image import MockImageProvider
from adaptive_game_generation.roles.art_producer import ArtProducer, ArtProducerConfig
from adaptive_game_generation.roles.designer import Designer, DesignerConfig
from adaptive_game_generation.roles.developer import Developer, DeveloperConfig
from adaptive_game_generation.roles.player import Player, PlayerConfig
from adaptive_game_generation.roles.qa_reviewer import QAConfig, QAReviewer
from adaptive_game_generation.runtime import DockerSandbox, Executor, MountedAssetLibraries, RuntimeConfig, Services, Workspace
from adaptive_game_generation.runtime.godot_mcp import DockerGodotMCP, MCP_PACKAGE
from adaptive_game_generation.runtime.commit import digest_path
from adaptive_game_generation.runtime.workspace import no_links

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASK = ROOT / "benchmark_resources/FreedomIntelligence-gamecraft-bench-a433475/tasks/platformer-time-loop"
PREFIXES = {Role.DESIGNER: "DESIGNER", Role.ART_PRODUCER: "ART_PLANNER", Role.DEVELOPER: "CODE_DEVELOPER",
            Role.PLAYER: "PLAYER", Role.QA_REVIEWER: "QA"}
LIVE_MODE = "fixed-loop/real-text-models/real-image-generation/real-isolated-engine"
MOCK_MODE = "fixed-loop/real-text-models/mock-image-generation/real-isolated-engine"


def implementation_fingerprint():
    source = ROOT / "src/adaptive_game_generation"
    files = {p.relative_to(source).as_posix(): digest_path(p) for p in sorted(source.rglob("*.py"))}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def snapshot_implementation(output):
    for source in sorted((ROOT / "src/adaptive_game_generation").rglob("*.py")):
        target = output / "implementation_snapshot" / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    target = output / "implementation_snapshot/examples/fixed_loop_demo.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__), target)
    scheduler_entry = ROOT / "examples/scheduler_demo.py"
    if scheduler_entry.exists():
        shutil.copyfile(scheduler_entry, output / "implementation_snapshot/examples/scheduler_demo.py")
    for directory in ("godot_mcp", "player"):
        for source in (ROOT / "configs" / directory).iterdir():
            if source.is_file():
                target = output / "implementation_snapshot" / source.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)


def engine_settings(values):
    settings = {}
    engines = [
        ("developer", "DEVELOPER_SANDBOX_IMAGE", "gamecraft-godot:4.5.1"),
        ("player", "PLAYER_SANDBOX_IMAGE", "gamecraft-player:4.5.1"),
    ]
    if values.get("GAMECRAFT_MCP_ENABLED") == "true":
        engines.append(("godot_mcp", "GODOT_MCP_IMAGE", "gamecraft-godot-mcp:4.5.1-mcp3.8.1"))
    for role, key, default in engines:
        selected = values.get(key) or default
        result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", selected],
                                capture_output=True, text=True, timeout=20)
        image_id = result.stdout.strip()
        if result.returncode or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise RuntimeError(f"Preloaded {role} Docker image is unavailable; see docs/fixed_loop.md")
        settings[role] = {"requested_image": selected, "image_id": image_id,
            "cpus": 2.0 if role == "player" else 1.0, "memory_mb": 1536 if role == "player" else 1024,
            "tmpfs_mb": 512 if role == "player" else 256, "pids": 128}
        if role == "godot_mcp":
            settings[role].update(cpus=2.0, memory_mb=2048, tmpfs_mb=1024, server_package=MCP_PACKAGE)
            expected = {name: digest_path(ROOT / "configs/godot_mcp" / name)
                        for name in ("mcp_driver.py", "godot_wrapper.py")}
            actual = subprocess.run(["docker", "run", "--rm", "--network=none", "--read-only",
                "--entrypoint", "sha256sum", image_id, *["/opt/gamecraft/" + n for n in expected]],
                capture_output=True, text=True, timeout=30)
            found = {Path(line.split()[-1]).name: line.split()[0] for line in actual.stdout.splitlines() if len(line.split()) == 2}
            if actual.returncode or found != expected:
                raise RuntimeError("Godot MCP image is stale; rebuild it using docs/godot_mcp.md")
            settings[role]["driver_hashes"] = expected
            settings[role]["package_lock_sha256"] = digest_path(ROOT / "configs/godot_mcp/package-lock.json")
    settings["isolation"] = {"network": "none", "non_root": True, "read_only_root": True,
        "fresh_process_and_user_data": True, "host_execution_fallback": False, "fps": 30, "resolution": [1280, 720]}
    settings["player_driver_sha256"] = digest_path(ROOT / "configs/player/player_driver.py")
    settings["player_harness_sha256"] = digest_path(ROOT / "configs/player/player_harness.gd")
    return settings


def developer_visual_settings(qa_gateway, values):
    if not qa_gateway.vision and not values.get("DEVELOPER_VISUAL_MODEL"):
        raise ValueError("Enable QA_VISION or set DEVELOPER_VISUAL_MODEL to a vision-capable model")
    return qa_gateway.revised(vision=True,
        model=values.get("DEVELOPER_VISUAL_MODEL") or qa_gateway.model,
        max_output_tokens=int(values.get("DEVELOPER_VISUAL_MAX_OUTPUT_TOKENS") or "8192"))


def role_prompt_settings(config, prefix, values):
    """Local UTF-8 request cap, separate from the provider's context window."""
    value = values.get(f"{prefix}_MAX_PROMPT_BYTES")
    return config if value is None else config.revised(max_prompt_bytes=int(value))


def role_setup(gateways, image_timeout=0.0, *, godot_mcp=False, visual_tokens=8192, values=None):
    values = {} if values is None else values
    configs = {
        Role.DESIGNER: DesignerConfig(max_output_tokens=gateways[Role.DESIGNER].max_output_tokens),
        Role.ART_PRODUCER: ArtProducerConfig(max_model_turns=6, max_search_turns=3, normalize_assets=True,
                                           max_output_tokens=gateways[Role.ART_PRODUCER].max_output_tokens),
        Role.DEVELOPER: DeveloperConfig(prompt_version="developer.v3", max_model_turns=5, require_normalized_assets=True,
                                       max_output_tokens=gateways[Role.DEVELOPER].max_output_tokens),
        Role.PLAYER: PlayerConfig(prompt_version="player.v2", max_traces=5, max_model_turns=5,
                                 max_visual_images=8 if gateways[Role.PLAYER].vision else 0,
                                 max_output_tokens=gateways[Role.PLAYER].max_output_tokens),
        Role.QA_REVIEWER: QAConfig(max_grounding_repairs=1,
                                  max_visual_images=8 if gateways[Role.QA_REVIEWER].vision else 0,
                                  max_output_tokens=gateways[Role.QA_REVIEWER].max_output_tokens),
    }
    if godot_mcp:
        configs[Role.DEVELOPER] = DeveloperConfig(prompt_version="developer.v4", max_model_turns=8, require_normalized_assets=True,
            max_output_tokens=gateways[Role.DEVELOPER].max_output_tokens, visual_review_tokens=visual_tokens)
    optimize = str(values.get("CODE_DEVELOPER_OPTIMIZE_EXECUTION", "true")).lower()
    if optimize not in ("true", "false"):
        raise ValueError("CODE_DEVELOPER_OPTIMIZE_EXECUTION must be true or false")
    if optimize == "true":
        current = configs[Role.DEVELOPER]
        configs[Role.DEVELOPER] = current.revised(optimize_execution=True,
            max_model_turns=min(12, current.max_model_turns + current.max_context_reads))
    configs = {role: role_prompt_settings(config, PREFIXES[role], values) for role, config in configs.items()}
    classes = {Role.DESIGNER: Designer, Role.ART_PRODUCER: ArtProducer, Role.DEVELOPER: Developer,
               Role.PLAYER: Player, Role.QA_REVIEWER: QAReviewer}
    limits = {}
    for role, config in configs.items():
        turns = getattr(config, "max_model_turns", 1)
        # Bounds reserve full request bytes before dispatch. Actual tokens, model
        # calls, images and tool time are still metered by the common executor.
        duration = max(3600.0, turns * gateways[role].timeout_seconds + 1800.0)
        if role == Role.ART_PRODUCER:
            duration = max(duration, turns * gateways[role].timeout_seconds + config.max_assets * image_timeout + 1800.0)
        helper_calls = config.max_mcp_inspections if role == Role.DEVELOPER and godot_mcp else 0
        duration += helper_calls * (gateways[Role.QA_REVIEWER].timeout_seconds + 180.0)
        limits[role] = ResourceLimits(max_model_calls=turns + helper_calls,
            max_input_tokens=int(values.get("FIXED_LOOP_ROLE_MAX_INPUT_TOKENS", "6000000")),
            max_output_tokens=turns * config.max_output_tokens + helper_calls * visual_tokens,
            max_image_calls=config.max_assets if role == Role.ART_PRODUCER else 0,
            max_tool_calls=5000, max_tool_seconds=duration, max_wall_seconds=duration)
    return configs, {r: classes[r](c) for r, c in configs.items()}, limits


def observed_adapter(role, adapter, provider, output, auxiliary_provider=None):
    async def invoke(context):
        start = provider.call_count
        auxiliary_start = auxiliary_provider.call_count if auxiliary_provider else 0
        try:
            return await adapter(context)
        finally:
            paths = [f"model_calls/{role.value}/{index:03}" for index in range(start + 1, provider.call_count + 1)]
            write_json(output / "model_call_index" / (context.request.request_id + ".json"),
                {"request_id": context.request.request_id, "role": role.value, "calls": paths,
                 **({"visual_tool_calls": [f"model_calls/developer_visual/{i:03}" for i in
                    range(auxiliary_start + 1, auxiliary_provider.call_count + 1)]} if auxiliary_provider else {})})
    return invoke


def observed_scheduler(adapter, provider, output):
    async def invoke(context, candidates, folder):
        start = provider.call_count
        try:
            return await adapter(context, candidates, folder)
        finally:
            write_json(output / "model_call_index" / (folder.name + ".json"),
                {"decision_id": folder.name, "actor": "scheduler",
                 "calls": [f"model_calls/scheduler/{i:03}" for i in range(start + 1, provider.call_count + 1)]})
    return invoke


def run_wall_seconds(explicit, values):
    """The same frozen deadline for both controllers; explicit CLI wins over env."""
    import math
    value = float(explicit if explicit is not None else values.get("RUN_MAX_WALL_SECONDS", "43200"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("RUN_MAX_WALL_SECONDS / --max-wall-seconds must be finite and positive")
    return value


async def run_live(output, *, task_dir=DEFAULT_TASK, env_file=ROOT / ".env", max_cycles=3,
                   max_model_calls=32, max_wall_seconds=None, preflight_only=False, mock_images=False, godot_mcp=False,
                   controller="fixed_loop", max_role_calls=15, max_decisions=30):
    from dotenv import dotenv_values

    output, task_dir, env_file = Path(output).absolute(), Path(task_dir).absolute(), Path(env_file).absolute()
    no_links(output)
    if output.exists():
        raise ValueError("Choose a new output directory; existing runs are never overwritten or silently resumed")
    output.mkdir(parents=True)
    executor = None
    image_service = None
    mode = MOCK_MODE if mock_images else LIVE_MODE
    try:
        if controller not in ("fixed_loop", "adaptive_scheduler"):
            raise ValueError("Unknown controller")
        adaptive = controller == "adaptive_scheduler"
        if adaptive:
            mode = mode.replace("fixed-loop/", "adaptive-scheduler/", 1)
        loop_config = FixedLoopConfig(max_cycles=max_cycles, max_role_calls=max_role_calls)
        gateways = {role: GatewayConfig.from_env(env_file, prefix=prefix) for role, prefix in PREFIXES.items()}
        image_config = None if mock_images else image_config_from_env(env_file)
        image_info = MockImageProvider.info if mock_images else image_config.info
        image_timeout = 0.0 if mock_images else image_config.timeout_seconds
        values = {**dotenv_values(env_file, interpolate=False), **os.environ}
        max_wall_seconds = run_wall_seconds(max_wall_seconds, values)
        scheduler_gateway, scheduler_limits = None, None
        if adaptive:
            scheduler_gateway = GatewayConfig.from_env(env_file, prefix="SCHEDULER")
            loop_config = SchedulerConfig(max_role_calls=max_role_calls, max_decisions=max_decisions,
                max_output_tokens=scheduler_gateway.max_output_tokens,
                max_prompt_bytes=int(values.get("SCHEDULER_MAX_PROMPT_BYTES", "1048576")))
            scheduler_limits = ResourceLimits(max_model_calls=1,
                max_input_tokens=int(values.get("SCHEDULER_MAX_INPUT_TOKENS", "2097152")),
                max_output_tokens=scheduler_gateway.max_output_tokens, max_image_calls=0, max_tool_calls=10,
                max_tool_seconds=scheduler_gateway.timeout_seconds + 60.0,
                max_wall_seconds=scheduler_gateway.timeout_seconds + 60.0)
        values["GAMECRAFT_MCP_ENABLED"] = "true" if godot_mcp else "false"
        visual_gateway = None
        if godot_mcp:
            visual_gateway = developer_visual_settings(gateways[Role.QA_REVIEWER], values)
        sandbox = await asyncio.to_thread(engine_settings, values)
        workspace, task = Workspace.prepare(output / "workspace", task_dir, task_id=task_dir.name,
            benchmark_snapshot=task_dir.parent.parent.name)
        task = task.revised(environment_constraints=(
            "Use English for all model inputs and authored outputs. Preserve the original instruction verbatim.",
            "Only the public instruction, optional public starter project and explicitly mounted asset libraries are available.",
            "Godot 4.5.1 runs in network-disabled containers. " +
            ("Image generation uses explicitly labeled mock placeholders." if mock_images else
             "Image generation uses the configured real image model through a host-managed, metered service. "
             "The host preserves native image pixels in lossless PNG and records logical display sizes separately. Image quality still requires review."),
            "Player uses an external public input/replay harness. Vision-enabled Player and QA models receive selected "
            "integrity-checked capture screenshots as actual image inputs. " +
            ("Developer receives metered MCP diagnostics and visual-tool feedback; previews are not replay evidence."
             if godot_mcp else "Other roles receive textual evidence."),
        ))
        raw = workspace.read_public("input/instruction.md").read_bytes()
        (output / "instruction.md").write_bytes(raw)
        libraries = MountedAssetLibraries.from_env(env_file, forbidden_roots=(task_dir.parent, output))
        configs, roles, limits = role_setup(gateways, image_timeout, godot_mcp=godot_mcp,
            visual_tokens=visual_gateway.max_output_tokens if visual_gateway else 8192, values=values)
        budget = ResourceLimits(max_model_calls=max_model_calls,
            max_input_tokens=int(values.get("FIXED_LOOP_MAX_INPUT_TOKENS", "15000000")),
            max_output_tokens=int(values.get("FIXED_LOOP_MAX_OUTPUT_TOKENS", "1000000")),
            max_image_calls=32, max_tool_calls=20000, max_tool_seconds=max_wall_seconds, max_wall_seconds=max_wall_seconds)
        configuration = {
            "controller_kind": controller,
            "controller": loop_config.model_dump(mode="json"), "task_id": task.task_id,
            "instruction_sha256": hashlib.sha256(raw).hexdigest(), "benchmark_snapshot": task.benchmark_snapshot,
            "providers": {r.value: g.public_settings() for r, g in gateways.items()},
            "provider_configuration_ids": {r.value: g.configuration_id for r, g in gateways.items()},
            "roles": {r.value: c.model_dump(mode="json") for r, c in configs.items()},
            "role_limits": {r.value: limit.model_dump(mode="json") for r, limit in limits.items()},
            "total_budget": budget.model_dump(mode="json"), "engines": sandbox,
            "libraries": {"configuration_id": libraries.configuration_id, "availability": await libraries.availability()},
            "images": image_info.model_dump(mode="json"),
            "image_configuration": image_config.public_settings() if image_config else {"adapter": "mock-image.v1"},
            "image_configuration_id": image_info.provider_id,
            "implementation_sha256": implementation_fingerprint(),
            "runner_sha256": digest_path(Path(__file__)),
            "python_version": platform.python_version(),
            "dependencies": {name: version(name) for name in (
                "pydantic", "Pillow", "openai", "httpx", "python-dotenv",
                *(("dashscope",) if image_config and image_config.public_settings()["adapter"].startswith("dashscope-") else ()))},
            "final_version_selection": "latest_committed_revision",
            "godot_mcp": {"enabled": godot_mcp, "visual_provider": visual_gateway.public_settings() if visual_gateway else None,
                          "visual_configuration_id": visual_gateway.configuration_id if visual_gateway else None,
                          "preview_is_acceptance_evidence": False},
        }
        common_fields = ("task_id", "instruction_sha256", "benchmark_snapshot", "providers", "provider_configuration_ids",
            "roles", "role_limits", "total_budget", "engines", "libraries", "images", "image_configuration_id",
            "implementation_sha256", "runner_sha256", "python_version", "dependencies", "godot_mcp", "final_version_selection")
        conditions = {key: configuration[key] for key in common_fields}
        conditions["max_role_calls"] = max_role_calls
        configuration["comparison_conditions_sha256"] = hashlib.sha256(json.dumps(conditions, sort_keys=True).encode()).hexdigest()
        configuration["scheduler"] = {"enabled": adaptive,
            "provider": scheduler_gateway.public_settings() if adaptive else None,
            "provider_configuration_id": scheduler_gateway.configuration_id if adaptive else None,
            "limits": scheduler_limits.model_dump(mode="json") if adaptive else None,
            "cost_included_in_total_budget": True}
        write_json(output / "experiment.json", configuration)
        snapshot_implementation(output)
        if preflight_only:
            summary = {"status": "preflight_passed", "models_called": False, "engine_sessions_started": False,
                       "image_mode": "mock" if mock_images else "real",
                       "instruction_sha256": configuration["instruction_sha256"]}
            write_json(output / "summary.json", summary)
            return summary
        public_id = controller + "-" + hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()[:24]
        async with AsyncExitStack() as stack:
            image_service = MockImageProvider() if mock_images else await stack.enter_async_context(
                create_image_provider(image_config, capture_directory=output / "image_calls"))
            providers = {r: await stack.enter_async_context(OpenAICompatibleTextProvider(g, capture_directory=output / "model_calls" / r.value))
                         for r, g in gateways.items()}
            visual_provider = await stack.enter_async_context(OpenAICompatibleTextProvider(visual_gateway,
                capture_directory=output / "model_calls/developer_visual")) if visual_gateway else None
            scheduler_provider = await stack.enter_async_context(OpenAICompatibleTextProvider(scheduler_gateway,
                capture_directory=output / "model_calls/scheduler")) if adaptive else None
            mcp_service = DockerGodotMCP(DockerSandbox(sandbox["godot_mcp"]["image_id"], cpus=2.0,
                memory_mb=2048, tmpfs_mb=1024)) if godot_mcp else None
            services = {r: Services(text=p) for r, p in providers.items()}
            services[Role.ART_PRODUCER] = Services(text=providers[Role.ART_PRODUCER], image=image_service,
                image_info=image_service.info, libraries=libraries)
            services[Role.DEVELOPER] = Services(text=providers[Role.DEVELOPER],
                sandbox=DockerSandbox(image=sandbox["developer"]["image_id"]), godot_mcp=mcp_service, vision=visual_provider)
            services[Role.PLAYER] = Services(text=providers[Role.PLAYER],
                sandbox=DockerSandbox(image=sandbox["player"]["image_id"], cpus=2.0, memory_mb=1536, tmpfs_mb=512))
            runtime_options = {"scheduler_limits": scheduler_limits} if adaptive else {}
            scheduler_timeout = scheduler_limits.max_wall_seconds if adaptive else 0.0
            executor = Executor.create(workspace, task, run_id=controller.replace("_", "-"), controller=controller, budget=budget,
                config=RuntimeConfig(role_limits_by_role=limits,
                    invocation_timeout_seconds=max(scheduler_timeout, *(l.max_wall_seconds for l in limits.values())),
                    tool_timeout_seconds=max(180.0, image_timeout, scheduler_timeout, *(g.timeout_seconds for g in gateways.values())),
                    service_configuration_id=public_id, **runtime_options), services_by_role=services,
                services=Services(text=scheduler_provider))
            observed = {r: observed_adapter(r, adapter, providers[r], output,
                visual_provider if r == Role.DEVELOPER else None) for r, adapter in roles.items()}
            progress = lambda entry: print(json.dumps({"slot": entry["id"], "status": entry.get("status", "starting"),
                    "reason": entry["reason"]}), flush=True)
            if adaptive:
                await AdaptiveScheduler(executor, observed, loop_config, progress=progress,
                    decision_adapter=observed_scheduler(ModelScheduler(loop_config), scheduler_provider, output)).run()
            else:
                await FixedLoop(executor, observed, loop_config, progress=progress).run()
        return export_run(executor, output, mode=mode)
    except BaseException as exc:
        # SDK and host exceptions may contain endpoints or credentials. Preserve
        # safe types; detailed redacted provider diagnostics live in model_calls/.
        write_json(output / "failure.json", {"error_type": type(exc).__name__,
            "phase": "execution" if executor else "preflight", "message": "Inspect the saved configuration, ledger and redacted provider metadata."})
        if executor:
            if executor.state.ledger.termination is None:
                executor.finish("cancelled" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else "failed",
                                "The host runner stopped; committed outputs are preserved.")
            try:
                export_run(executor, output, mode=mode)
            except Exception as export_error:
                write_json(output / "export_failure.json", {"error_type": type(export_error).__name__})
        else:
            write_json(output / "summary.json", {"status": "preflight_failed", "error_type": type(exc).__name__, "models_called": False})
        raise
    finally:
        if image_service is not None:
            write_json(output / "image_calls.json", image_service.calls)


def main(controller="fixed_loop"):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-dir", type=Path, default=DEFAULT_TASK)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    if controller == "fixed_loop":
        parser.add_argument("--max-cycles", type=int, default=3)
    else:
        parser.add_argument("--max-decisions", type=int, default=30)
    parser.add_argument("--max-role-calls", type=int, default=15)
    parser.add_argument("--max-model-calls", type=int, default=32)
    parser.add_argument("--max-wall-seconds", type=float, default=None,
                        help="Shared deadline: overrides RUN_MAX_WALL_SECONDS (default 43200 seconds / 12 hours)")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--godot-mcp", action="store_true",
                        help="Enable isolated Developer MCP inspection and metered visual feedback")
    parser.add_argument("--mock-images", action="store_true",
                        help="Explicitly use image placeholders for development; the default calls ART_IMAGE_MODEL")
    args = parser.parse_args()
    try:
        summary = asyncio.run(run_live(**vars(args), controller=controller))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Run cancelled. Saved evidence remains in the output directory.")
        return 130
    except Exception as exc:
        print(f"Run stopped ({type(exc).__name__}). Inspect failure.json and the saved run directory.")
        return 1
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("status") == "preflight_passed" or summary.get("delivery_ready") else 1 if summary.get("termination_reason") == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
