"""Run Designer on the original Time Loop task with the real configured model.

No mock response, manual response editing, or automatic API retry is used.
The output directory preserves the runtime workspace and full review captures.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path

from adaptive_game_generation.models import ResourceLimits
from adaptive_game_generation.providers import GatewayConfig, OpenAICompatibleTextProvider
from adaptive_game_generation.roles.designer import DesignDocument, Designer, DesignerConfig
from adaptive_game_generation.runtime import Executor, RuntimeConfig, Services, Workspace

CASE = Path(__file__).with_name("designer_case")
ROOT = Path(__file__).resolve().parents[1]


def load_benchmark_source() -> tuple[dict, Path, bytes]:
    metadata = json.loads((CASE / "source.json").read_text(encoding="utf-8"))
    source = ROOT / metadata["instruction_path"]
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != metadata["instruction_sha256"]:
        raise ValueError("Benchmark instruction differs from the pinned example source")
    return metadata, source, data


def write_json(path: Path, value) -> None:
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


async def _run_live(output: Path, config: GatewayConfig) -> dict:
    if output.exists():
        raise ValueError("Review destination must be a new directory")
    metadata, source, instruction_bytes = load_benchmark_source()
    metadata = {**metadata, "response_kind": "live model response; no mock or manual response editing"}
    output.mkdir(parents=True)
    (output / "instruction.md").write_bytes(instruction_bytes)
    write_json(output / "source.json", metadata)
    write_json(output / "provider_config.json", config.public_settings())
    workspace, task = Workspace.prepare(
        output / "workspace", source.parent, task_id=metadata["task_id"],
        benchmark_snapshot=metadata["snapshot"],
    )
    task = task.revised(environment_constraints=(
        "Use English for all authored design content. Preserve source excerpts verbatim.",
        "The instruction describes the target Godot 4 environment; this Designer call does not launch the engine.",
        "Only the public instruction is imported. Advertised asset libraries and helper tools are not mounted in this design test.",
    ))
    limits = ResourceLimits(
        max_model_calls=1, max_input_tokens=100000, max_output_tokens=config.max_output_tokens,
        max_tool_calls=10, max_tool_seconds=config.timeout_seconds + 30.0,
        max_wall_seconds=config.timeout_seconds + 60.0,
    )
    async with OpenAICompatibleTextProvider(config, capture_directory=workspace.private / "model_calls") as provider:
        executor = Executor.create(
            workspace, task, run_id="designer-live", controller="fixed_loop", budget=limits,
            config=RuntimeConfig(
                role_limits=limits, tool_timeout_seconds=config.timeout_seconds,
                invocation_timeout_seconds=config.timeout_seconds + 60.0,
                service_configuration_id=config.configuration_id,
            ), services=Services(text=provider),
        )
        request = executor.request(
            "designer", "Create the initial English specification for Time Loop from the complete public instruction.",
            ("Ground explicit requirements in exact source excerpts and observable acceptance criteria",
             "Cover all eight sections, preserve the public delivery protocol, and separate assumptions"),
            request_id="designer-initial",
        )
        write_json(output / "role_request.json", request.model_dump(mode="json"))
        result = await executor.execute(request, Designer(DesignerConfig(max_output_tokens=config.max_output_tokens)))
        state = executor.state
    write_json(output / "role_result.json", result.model_dump(mode="json"))
    write_json(output / "state_after.json", state.model_dump(mode="json"))
    audit = workspace.private / "tools.jsonl"
    if audit.exists():
        (output / "tool_audit.jsonl").write_bytes(audit.read_bytes())
    call = workspace.private / "model_calls" / "001"
    for original, destination in (
        ("request.json", "api_request.json"), ("response.json", "api_response.json"),
        ("response.txt", "model_response.txt"), ("metadata.json", "call_metadata.json"),
        ("response_chunks.jsonl", "response_chunks.jsonl"),
    ):
        if (call / original).exists():
            (output / destination).write_bytes((call / original).read_bytes())
    if (call / "request.json").exists():
        body = json.loads((call / "request.json").read_text(encoding="utf-8"))
        write_json(output / "model_prompt.json", json.loads(body["messages"][-1]["content"]))
    document = None
    if state.design is not None:
        document = DesignDocument.model_validate_json(workspace.read_public(state.design.document_ref.path).read_text(encoding="utf-8"))
        for ref in result.artifact_refs:
            (output / Path(ref.path).name).write_bytes(workspace.read_public(ref.path).read_bytes())
    raw_text = (output / "model_response.txt").read_text(encoding="utf-8") if (output / "model_response.txt").exists() else ""
    try:
        json.loads(raw_text)
    except ValueError:
        pass
    else:
        (output / "model_response.json").write_bytes((output / "model_response.txt").read_bytes())
    api_response = json.loads((output / "api_response.json").read_text(encoding="utf-8")) if (output / "api_response.json").exists() else {}
    api_usage = api_response.get("usage") or {}
    finish_reason = (api_response.get("choices") or [{}])[0].get("finish_reason")
    reasoning_tokens = (api_usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    checks = {
        "model_completed": finish_reason == "stop",
        "original_instruction_unchanged": (output / "instruction.md").read_bytes() == source.read_bytes(),
        "initial_design_committed": result.status == "completed" and state.state_revision == 1,
        "all_eight_sections": document is not None and len(document.sections) == 8,
        "no_fabricated_implementation": bool(state.requirements) and all(r.status == "not_implemented" for r in state.requirements),
        "english_output_no_cjk": bool(raw_text) and re.search(r"[\u3400-\u9fff]", raw_text) is None,
        "usage_matches_provider": state.ledger.usage.token_usage_complete and
            state.ledger.usage.input_tokens == api_usage.get("prompt_tokens") and
            state.ledger.usage.output_tokens == api_usage.get("completion_tokens"),
    }
    summary = {
        "mode": "live", "model": config.model, "response_model": api_response.get("model"),
        "response_id": api_response.get("id"), "role_status": result.status.value,
        "finish_reason": finish_reason, "reasoning_tokens": reasoning_tokens,
        "status": "completed" if all(checks.values()) else "failed",
        "requirements": len(state.requirements), "sections": len(document.sections) if document else 0,
        "state_revision": state.state_revision, "usage": state.ledger.usage.model_dump(mode="json"),
        "checks": checks, "error": result.error.model_dump(mode="json") if result.error else None,
    }
    write_json(output / "summary.json", summary)
    lines = [
        "# Live Designer review: Time Loop", "",
        f"Requested model: `{config.model}`. Status: **{summary['status']}**.", "",
        "This run called the configured model gateway. No mock response, hidden evaluation data, reference solution, manual JSON correction, or automatic API retry was used.", "",
        f"Source: `{metadata['instruction_path']}`.", "",
        f"Instruction SHA-256: `{metadata['instruction_sha256']}`.", "",
        "## Input and model call", "",
        "- [Original instruction](instruction.md)", "- [Source manifest](source.json)",
        "- [Role request](role_request.json)", "- [Complete Designer prompt and input](model_prompt.json)",
        "- [Actual API request body, including system message](api_request.json)",
        "- [Non-secret provider configuration](provider_config.json)", "",
        "## Output and validation", "",
        "- [Raw provider response, including usage](api_response.json)",
        "- [Unmodified model text](model_response.txt)", "- [Call timing and status](call_metadata.json)",
        "- [Role result](role_result.json)", "- [Committed state and ledger](state_after.json)",
        "- [Checks and usage summary](summary.json)", "- [Tool audit](tool_audit.jsonl)",
    ]
    if document:
        lines.extend(["- [Readable design](design.md)", "- [Structured design](design.json)"])
    if config.stream:
        lines.extend(["- [Original response chunks](response_chunks.jsonl)", "",
                      "Streaming was enabled. api_response.json reconstructs the final message and usage; response_chunks.jsonl preserves every SDK chunk, including additional provider fields."])
    lines.extend([
        "", f"Result: {summary['requirements']} requirements and {summary['sections']} sections.", "",
        f"Provider-reported usage: {api_usage.get('prompt_tokens', 'unavailable')} input tokens and {api_usage.get('completion_tokens', 'unavailable')} completion tokens.", "",
        f"Finish reason: `{finish_reason}`. Reasoning tokens within completion usage: {reasoning_tokens if reasoning_tokens is not None else 'unavailable'}.", "",
        "The original run directory retains workspace/ for inspection or recovery. This is a Designer integration test; the game has not been implemented, played, or officially evaluated. On failure, inspect the saved result and call status; missing response files mean no response was received.", "",
    ])
    for index, line in enumerate(lines):
        link = re.fullmatch(r"- \[(.+)\]\(([^)]+)\)", line)
        if link and not (output / link[2]).is_file():
            lines[index] = f"- {link[1]}: unavailable for this call."
    (output / "review.md").write_bytes("\n".join(lines).encode("utf-8"))
    return summary


def run_demo(output: Path, *, env_file: Path = ROOT / ".env", config: GatewayConfig | None = None) -> dict:
    return asyncio.run(_run_live(output, config or GatewayConfig.from_env(env_file)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New run and review directory")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    try:
        summary = run_demo(args.output, env_file=args.env_file)
    except Exception as exc:
        print(f"Live example could not finish ({type(exc).__name__}); inspect saved run records if present.")
        raise SystemExit(1) from None
    print(json.dumps(summary, ensure_ascii=False))
    print(f"Review: {args.output.resolve() / 'review.md'}")
    raise SystemExit(0 if summary["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
