"""Reconstruct compact prompts from saved public Developer requests; no service calls."""
import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from adaptive_game_generation.roles.developer import DeveloperConfig
from adaptive_game_generation.roles.developer.contracts import BuildAttempt, ProposalRejection
from adaptive_game_generation.roles.developer.context import ContextWindow, stage_for
from adaptive_game_generation.roles.developer.diagnostics import repair_brief
from adaptive_game_generation.roles.developer.prompt import build_prompt


def review(source: Path, output: Path, calls=(1, 4, 7, 8, 10)):
    if output.exists():
        raise ValueError("Use a new output directory; existing evidence is never overwritten")
    rows = []
    for index in calls:
        path = source / "model_calls/developer" / f"{index:03}" / "request.json"
        wire = json.loads(path.read_bytes())
        original = wire["messages"][-1]["content"]
        payload = json.loads(original)
        if "optimization" in payload:
            raise ValueError("Select pre-optimization requests for this structural comparison")
        inputs, files = payload["input"], payload["current_project_files"]
        config = DeveloperConfig(prompt_version=payload["prompt_version"], optimize_execution=True,
            max_output_tokens=wire.get("max_tokens", wire.get("max_completion_tokens", 16384)))
        attempts = [BuildAttempt.model_validate(a) for a in payload["build_feedback"]]
        rejections = [ProposalRejection.model_validate(r) for r in payload["proposal_feedback"]]
        brief = repair_brief(files, attempts, inputs.get("previous_report"))
        feedback = payload.get("inspection_feedback")
        inspection = SimpleNamespace(records=[SimpleNamespace(**r) for r in feedback["inspections"]]) if feedback else None
        stage, reason = stage_for(files, attempts, brief, inspection)
        window = ContextWindow(config)
        compact, selected, catalog, compact_feedback = window.prepare(inputs, files, attempts, brief, feedback,
            rejections, payload["rejected_proposal"], stage)
        after = build_prompt(compact, config, selected, attempts, payload["limits"]["turns_left"], rejections,
            payload["rejected_proposal"], inspection_feedback=compact_feedback, prepared_brief=brief,
            optimization={"protocol": "developer-execution.v1", "stage": stage, "reason": reason,
                "profile": {"mode": "offline reconstruction; no provider called"}, "context": catalog})
        assert json.loads(after)["input"]["task"]["instruction"] == inputs["task"]["instruction"]
        before_bytes, after_bytes = len(original.encode("utf-8")), len(after.encode("utf-8"))
        directory = output / f"{index:03}"
        directory.mkdir(parents=True)
        (directory / "before-request.json").write_bytes(path.read_bytes())
        (directory / "after-prompt.json").write_text(after, encoding="utf-8")
        # Full omitted documents remain inspectable; no extra filesystem is read.
        (directory / "available-context.json").write_text(json.dumps(window.resources, ensure_ascii=False, indent=2), encoding="utf-8")
        rows.append({"call": index, "source": path.as_posix(), "stage": stage, "before_bytes": before_bytes,
            "after_bytes": after_bytes, "reduction_percent": round((1 - after_bytes / before_bytes) * 100, 2),
            "instruction_sha256": hashlib.sha256(inputs["task"]["instruction"].encode()).hexdigest(),
            "full_source_files": sorted(selected), "available_resources": len(window.resources)})
    summary = {"mode": "Offline structural comparison of saved real requests; no model or engine execution",
        "limitation": "UTF-8 bytes, not tokens, latency, cache hits or quality. New protocol overhead included; subsequent requested reads not simulated.",
        "calls": rows}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--calls", type=int, nargs="+", default=[1, 4, 7, 8, 10])
    args = parser.parse_args()
    print(json.dumps(review(args.source, args.output, args.calls), indent=2))
