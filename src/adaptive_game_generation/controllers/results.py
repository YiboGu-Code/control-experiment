"""Local review exports. Never repair, repackage or execute code while exporting."""

import json
import shutil
from collections import Counter
from pathlib import Path

from ..models import ResourceUsage, Role, RoleCallFinished, SchedulerDecision
from ..runtime.commit import digest_path
from ..runtime.executor import completion_ready
from ..runtime.store import atomic_text
from ..runtime.workspace import inspect_tree, no_links


def write_json(path, value):
    atomic_text(Path(path), json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def export_run(executor, output: Path, *, mode="fixed-loop"):
    """Keep the authoritative workspace; export an exact copy of its latest game."""
    output = Path(output)
    no_links(output)
    state = executor.state
    write_json(output / "state_final.json", state.model_dump(mode="json"))
    finished = [e.payload for e in state.ledger.events if isinstance(e.payload, RoleCallFinished)]
    decisions = [e.payload for e in state.ledger.events if isinstance(e.payload, SchedulerDecision)]
    game = next((g for g in state.game_revisions if g.revision_id == state.current_game_revision_id), None)
    delivery = {"game_revision_id": state.current_game_revision_id, "exported": False,
                "ready": completion_ready(state), "selection": "latest_committed_revision"}
    if game:
        source = executor.workspace.read_public(game.project_ref.path)
        inspect_tree(source, max_bytes=executor.config.max_output_bytes)
        if not game.project_ref.sha256 or digest_path(source) != game.project_ref.sha256:
            raise ValueError("Final project integrity check failed; no delivery copy was created")
        target = output / "delivery" / "game"
        no_links(target)
        if target.exists():
            raise ValueError("Delivery export already exists; it will not be overwritten")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        exported = digest_path(target)
        if exported != game.project_ref.sha256:
            raise ValueError("Delivery copy checksum mismatch")
        delivery.update(exported=True, project_ref=game.project_ref.model_dump(mode="json"),
            sha256=exported, build_status=game.build_status,
            files={p.relative_to(target).as_posix(): digest_path(p) for p in sorted(target.rglob("*")) if p.is_file()},
            traces=sorted(p.relative_to(target).as_posix() for p in target.glob("demo_outputs/*.json")))
    write_json(output / "delivery" / "manifest.json", delivery)
    termination = state.ledger.termination
    summary = {
        "mode": mode,
        "controller": state.ledger.controller.value,
        "scheduler_decisions": len(decisions),
        "scheduler_failed_decisions": sum(d.error is not None for d in decisions),
        "scheduler_usage": ResourceUsage.aggregate(d.usage for d in decisions).model_dump(mode="json"),
        "worker_usage": ResourceUsage.aggregate(f.usage for f in finished).model_dump(mode="json"),
        "termination_reason": termination.reason.value if termination else None,
        "termination_summary": termination.summary if termination else "Run is still active",
        "delivery_ready": delivery["ready"], "delivery_exported": delivery["exported"],
        "final_game_revision_id": state.current_game_revision_id,
        "final_build_status": game.build_status.value if game else None,
        "game_builds": [{"revision_id": g.revision_id, "build_status": g.build_status.value} for g in state.game_revisions],
        "roles_not_invoked": [role.value for role in Role if not any(f.result.role == role for f in finished)],
        "role_calls": dict(Counter(f.result.role.value for f in finished)),
        "role_statuses": dict(Counter(f.result.status.value for f in finished)),
        "usage": state.ledger.usage.model_dump(mode="json"),
        "requirements": dict(Counter(r.status.value for r in state.requirements)),
        "unresolved_issue_ids": [i.issue_id for i in state.issues if i.status != "closed"],
        "readiness": state.readiness.model_dump(mode="json") if state.readiness else None,
        "last_errors": [f.result.error.model_dump(mode="json") for f in finished if f.result.error],
    }
    write_json(output / "summary.json", summary)
    image_description = ("Image generation is configured to use the real image service. "
                         if "/real-image-generation/" in mode else
                         "Image generation is explicitly mocked. " if "/mock-image-generation/" in mode else
                         "See the experiment configuration for the image service used. ")
    title = "Adaptive Scheduler" if state.ledger.controller == "adaptive_scheduler" else "Fixed Loop"
    lines = [f"# {title} run review", "",
        f"Stop reason: **{summary['termination_reason']}**. Delivery ready: **{summary['delivery_ready']}**.", "",
        "This run starts from the original public instruction and fresh pipeline state. Only invoked roles produce outputs. "
        + image_description + "A bounded run or a completed role is not official Benchmark acceptance.", "",
        "- [Original instruction](instruction.md)", "- [Frozen experiment configuration](experiment.json)",
        "- [Summary](summary.json)", "- [Final state and complete executor ledger](state_final.json)",
        "- [Scheduling and skip audit](workspace/.private/controller/checkpoint.json)",
        "- [Tool audit](workspace/.private/tools.jsonl)", "- [Image call index](image_calls.json)",
        "- [Delivery manifest](delivery/manifest.json)", "",
        "## Role inputs and outputs", "",
        "ArtifactRef.path resolves under `workspace/public/`. Every call below has a full input state, "
        "request, result and output state. Raw provider requests/responses are mapped by model_call_index.json.", ""]
    for item in finished:
        request_id = item.result.request_id
        base = f"workspace/.private/controller/calls/{request_id}"
        lines.append(f"- {request_id}: **{item.result.status.value}** — "
                     f"[input]({base}/state_before.json), [request]({base}/request.json), "
                     f"[result]({base}/result.json), [output]({base}/state_after.json), "
                     f"[model calls](model_call_index/{request_id}.json)")
    if decisions:
        lines += ["", "## Scheduler decisions", "",
            "Scheduler usage is included in total usage and also reported separately. Each decision selects only eligible public work; "
            "the host applies common readiness and budget rules. Invalid decisions are charged and cannot dispatch workers.", ""]
        for decision in decisions:
            base = f"workspace/.private/controller/decisions/{decision.decision_id}"
            selected = decision.selected_role.value if decision.selected_role else "rejected"
            lines.append(f"- {decision.decision_id}: **{selected}** — [input]({base}/input.json), "
                f"[raw output]({base}/response.txt), [recorded decision]({base}/result.json), "
                f"[model calls](model_call_index/{decision.decision_id}.json)")
    if game:
        lines += ["", "## Latest game", "",
            f"Basic build: **{game.build_status.value}**. Exporting a revision does not mean it is playable or ready.", "",
            "The exported `delivery/game/` is byte-identical to the selected committed project. No trace, "
            "code or asset is added during export. Failed or unfinished latest revisions are retained honestly.",
            "", "Open `delivery/game/project.godot` in Godot 4.5.1. Import resources before running from the command line.",
            "Use the latest Developer handoff for controls and named scenarios.", ""]
    lines += ["## Interpretation", "",
        "Review the current QA report, not only role statuses. Mock art blocks final-art acceptance. "
        "Replay results cover recorded inputs, the fixed engine image and observed state/pixels, not all gameplay or other platforms.",
        "Keep this entire directory for later analysis. It contains no copied .env or hidden evaluator files.", ""]
    atomic_text(output / "review.md", "\n".join(lines))
    return summary
