"""Real Art planner + explicit mock images, using a recorded real Designer input."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path

from adaptive_game_generation.models import ProposedUpdates, ResourceLimits, RoleResult, SharedState
from adaptive_game_generation.providers import GatewayConfig, OpenAICompatibleTextProvider
from adaptive_game_generation.providers.mock_image import MockImageProvider
from adaptive_game_generation.roles.art_producer import ArtManifest, ArtProducer, ArtProducerConfig
from adaptive_game_generation.roles.designer import DesignDocument
from adaptive_game_generation.runtime import Executor, RuntimeConfig, Services, Workspace
from adaptive_game_generation.runtime.asset_libraries import MountedAssetLibraries

ROOT = Path(__file__).resolve().parents[1]
DESIGNER_REVIEW = ROOT / "docs/examples/designer_review"


def write_json(path, value):
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def load_inputs():
    source = json.loads((ROOT / "examples/designer_case/source.json").read_text(encoding="utf-8"))
    instruction_path = ROOT / source["instruction_path"]
    instruction = instruction_path.read_bytes()
    if hashlib.sha256(instruction).hexdigest() != source["instruction_sha256"]:
        raise ValueError("Benchmark instruction no longer matches the pinned source")
    state = SharedState.model_validate_json((DESIGNER_REVIEW / "state_after.json").read_bytes())
    design = (DESIGNER_REVIEW / "design.json").read_bytes()
    document = DesignDocument.model_validate_json(design)
    if state.task.instruction != instruction.decode("utf-8") or state.task.task_id != document.task_id:
        raise ValueError("Recorded Designer task does not match the original instruction")
    if hashlib.sha256(design).hexdigest() != state.design.document_ref.sha256:
        raise ValueError("Recorded Designer artifact checksum mismatch")
    if [r.model_dump(exclude={"status", "verification"}) for r in state.requirements] != [r.model_dump() for r in document.requirements]:
        raise ValueError("Recorded Designer requirements differ from its document")
    return source, instruction_path, instruction, state, design


async def seed_recorded_designer(executor, recorded_state, design):
    """Explicitly import prior real output through the executor; no simulated model call."""
    async def import_recorded(context):
        path = await context.tools.write_bytes("design.json", design)
        original = recorded_state.design.document_ref
        ref = original.revised(path=path)
        return RoleResult(request_id=context.request.request_id, role="designer", state_revision=0,
            status="completed", summary="Imported the unmodified recorded real Designer artifact as test precondition; no new Designer model call",
            artifact_refs=(ref,), proposed_updates=ProposedUpdates(requirements=recorded_state.requirements,
                design=recorded_state.design.revised(document_ref=ref)))
    request = executor.request("designer", "Import the previously recorded real Designer output for this isolated Art Producer test",
        ("Preserve original Designer bytes and grounded requirements",), request_id="import-recorded-designer")
    outcome = await executor.execute(request, import_recorded)
    if outcome.status != "completed":
        raise RuntimeError("Cannot import recorded Designer precondition")


async def run_live(output, env_file):
    config = GatewayConfig.from_env(env_file, prefix="ART_PLANNER")
    source, instruction_path, instruction, recorded_state, design = load_inputs()
    if output.exists():
        raise ValueError("Run destination must be a new directory")
    output.mkdir(parents=True)
    (output / "instruction.md").write_bytes(instruction)
    (output / "designer_input.json").write_bytes(design)
    designer_metadata = json.loads((DESIGNER_REVIEW / "summary.json").read_text(encoding="utf-8"))
    write_json(output / "source.json", {**source, "designer_input": {
        "source": "docs/examples/designer_review/design.json", "sha256": hashlib.sha256(design).hexdigest(),
        "response_id": designer_metadata["response_id"], "model": designer_metadata["response_model"],
        "mode": "Unmodified previous real Designer output imported as a precondition; no new Designer call"},
        "planner": "real configured model", "image_generation": "mock-image.v1"})
    write_json(output / "provider_config.json", config.public_settings())
    workspace, task = Workspace.prepare(output / "workspace", instruction_path.parent,
        task_id=source["task_id"], benchmark_snapshot=source["snapshot"])
    task = task.revised(environment_constraints=(
        "All authored role inputs, prompts, and outputs must be in English. The original instruction is unchanged.",
        "Library paths in the instruction refer to the target server. Actual mount availability is supplied by the host.",
        "Both library selection and self-generation are allowed for this experiment. Actual image generation is mocked in this test only.",
        "No engine or hidden evaluator is run. Do not claim visual inspection, game integration, or official acceptance.",
    ))
    libraries = MountedAssetLibraries.from_env(env_file, forbidden_roots=(ROOT / "benchmark_resources", workspace.root))
    image = MockImageProvider()
    write_json(output / "asset_sources.json", {"libraries": await libraries.availability(),
        "library_configuration_id": libraries.configuration_id, "library_snapshot": libraries.snapshot_id,
        "image_service": image.info.model_dump(mode="json")})
    art_config = ArtProducerConfig(max_output_tokens=config.max_output_tokens, max_assets=12)
    limits = ResourceLimits(max_model_calls=art_config.max_model_turns, max_input_tokens=800000,
        max_output_tokens=config.max_output_tokens * art_config.max_model_turns, max_image_calls=12,
        max_tool_calls=160, max_tool_seconds=config.timeout_seconds * 4 + 120.0,
        max_wall_seconds=config.timeout_seconds * 4 + 180.0)
    async with OpenAICompatibleTextProvider(config, capture_directory=workspace.private / "model_calls") as text:
        executor = Executor.create(workspace, task, run_id="art-producer-live", controller="fixed_loop", budget=limits,
            config=RuntimeConfig(role_limits=limits, tool_timeout_seconds=config.timeout_seconds,
                invocation_timeout_seconds=config.timeout_seconds * 4 + 180.0,
                service_configuration_id=config.configuration_id + ":mock-image.v1:" + libraries.configuration_id),
            services=Services(text=text, image=image, image_info=image.info, libraries=libraries))
        await seed_recorded_designer(executor, recorded_state, design)
        before = executor.state
        write_json(output / "state_before.json", before.model_dump(mode="json"))
        request = executor.request("art_producer",
            "Create the initial Time Loop art package and Developer handoff using available library assets or generated assets",
            ("Cover the required player/ghost, level, interaction, hazard and UI visual needs within twelve reusable assets",
             "Preserve source licenses, precise dimensions, frame layout, target paths and affected scenes",
             "Mark mock generation as placeholders with final visual acceptance pending"), request_id="art-initial")
        write_json(output / "role_request.json", request.model_dump(mode="json"))
        result = await executor.execute(request, ArtProducer(art_config))
        state = executor.state
    write_json(output / "role_result.json", result.model_dump(mode="json"))
    write_json(output / "state_after.json", state.model_dump(mode="json"))
    write_json(output / "image_calls.json", image.calls)
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
        relative = ref.path.split("/", 3)[3]
        destination = output / "deliverables" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(workspace.read_public(ref.path).read_bytes())
    manifest_path = output / "deliverables/asset_manifest.json"
    manifest = ArtManifest.model_validate_json(manifest_path.read_bytes()) if manifest_path.exists() else None
    checks = {
        "original_instruction_unchanged": instruction_path.read_bytes() == (output / "instruction.md").read_bytes(),
        "workspace_instruction_bytes_unchanged": workspace.read_public("input/instruction.md").read_bytes() == instruction,
        "real_planner_completed": bool(responses) and len(responses) == state.ledger.usage.model_calls and all(
            r["choices"][0]["finish_reason"] == "stop" for r in responses),
        "assets_committed": state.state_revision == 2 and bool(state.assets),
        "requirements_and_design_unchanged": state.requirements == before.requirements and state.design == before.design,
        "no_game_or_qa_claims": not state.game_revisions and not state.evidence and state.readiness is None,
        "mock_images_labelled": manifest is not None and all(a.is_mock == (a.request.source == "generate") for a in manifest.assets),
        "status_matches_placeholders": manifest is not None and result.status == ("partial" if any(a.is_mock for a in manifest.assets) else "completed"),
        "image_usage_matches_mock_calls": state.ledger.usage.image_calls == len(image.calls),
        "text_usage_matches_provider": state.ledger.usage.token_usage_complete and
            state.ledger.usage.input_tokens == sum(r["usage"]["prompt_tokens"] for r in responses) and
            state.ledger.usage.output_tokens == sum(r["usage"]["completion_tokens"] for r in responses),
        "english_authored_output_no_cjk": bool(responses) and all(not re.search(r"[\u3400-\u9fff]", r["choices"][0]["message"]["content"]) for r in responses),
    }
    summary = {"mode": "live-text/mock-image", "status": "completed" if all(checks.values()) else "failed",
        "role_status": result.status.value, "model": config.model, "asset_count": len(state.assets),
        "mock_asset_count": sum(a.is_mock for a in manifest.assets) if manifest else 0,
        "usage": state.ledger.usage.model_dump(mode="json"), "checks": checks,
        "error": result.error.model_dump(mode="json") if result.error else None}
    write_json(output / "summary.json", summary)
    lines = ["# Art Producer review: Time Loop", "",
        f"Integration check: **{summary['status']}**. Role status: **{result.status.value}**.", "",
        "The planner uses the real configured model. Only image generation is mocked. The initial Designer input is the unmodified previously recorded real response, imported as a test precondition with no new model call.", "",
        "The original Benchmark instruction is unchanged. Both Kenney and OpenGameArt interfaces are enabled; actual mount availability is recorded. No hidden evaluation data was read.", "",
        "## Inputs and full model I/O", "",
        "- [Original instruction](instruction.md)", "- [Source and Designer provenance](source.json)",
        "- [Recorded real Designer input](designer_input.json)", "- [Role request](role_request.json)",
        "- [Actual asset sources](asset_sources.json)", "- [Public model configuration](provider_config.json)", ""]
    for directory in sorted((output / "model_calls").iterdir()):
        lines.extend([f"- [Call {directory.name}: full API request](model_calls/{directory.name}/request.json)",
                      f"- [Call {directory.name}: full prompt](model_calls/{directory.name}/prompt.json)"])
        for name in ("response.json", "response.txt", "metadata.json"):
            if (directory / name).exists():
                lines.append(f"- [Call {directory.name}: {name}](model_calls/{directory.name}/{name})")
    lines.extend(["", "## Outputs", "", "- [Role result](role_result.json)", "- [Committed state](state_after.json)",
        "- [Summary and checks](summary.json)", "- [Tool audit](tool_audit.jsonl)", "- [Mock image inputs and output hashes](image_calls.json)"])
    if manifest:
        lines.extend(["- [Asset manifest](deliverables/asset_manifest.json)", "- [Developer handoff](deliverables/art_handoff.md)",
            "", "## Image outputs", "", "These images are test placeholders where marked MOCK. They are not generated artwork or visual acceptance evidence.", ""])
        for asset in manifest.assets:
            lines.append(f"- [{asset.record.asset_id}](deliverables/{asset.request.target_path}): {asset.width} x {asset.height}; mock={asset.is_mock}")
    (output / "review.md").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return summary


def run_demo(output, *, env_file=ROOT / ".env"):
    return asyncio.run(run_live(output, env_file))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    result = run_demo(args.output, env_file=args.env_file)
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "completed" else 1)
