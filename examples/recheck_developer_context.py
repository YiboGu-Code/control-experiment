"""Replay context decisions over saved verified snapshots; no model or engine runs.

The 'before' view reproduces v1 read replacement/clear-on-patch behavior in memory;
it never imports or executes Python/code from the saved run.
"""
import argparse
import json
from pathlib import Path

from adaptive_game_generation.roles.developer import DeveloperConfig, DeveloperContext, DeveloperProposal
from adaptive_game_generation.roles.developer.context import ContextWindow
from adaptive_game_generation.roles.developer.contracts import BuildAttempt
from adaptive_game_generation.roles.developer.edits import apply_edits
from adaptive_game_generation.runtime import InvalidSubmission


class BeforeWindow(ContextWindow):
    def prepare(self, *args, **kwargs):
        self.auto_paths = ()
        return super().prepare(*args, **kwargs)

    def request(self, decision):
        if self.read_count >= self.config.max_context_reads:
            raise InvalidSubmission("Context request allowance exhausted")
        total = sum(len(self._read_part(r).encode()) for r in decision.reads)
        if total > self.config.max_context_response_bytes:
            raise InvalidSubmission("Context byte budget exhausted")
        self.pending = decision.reads
        self.read_count += 1


def recheck(root):
    state = json.loads((root / "state_final.json").read_bytes())
    rows = []
    for ref in state["artifacts"]:
        if ref["artifact_id"] != "developer-report":
            continue
        folder = root / "workspace/public" / Path(ref["path"]).parent
        report = json.loads((folder / "developer_report.json").read_bytes())
        attempts = [BuildAttempt.model_validate(b) for b in report["build_attempts"]]
        accepted = {b.model_turn for b in attempts}
        for label, cls in (("before", BeforeWindow), ("after", ContextWindow)):
            window = cls(DeveloperConfig.model_validate(report["config"]))
            inputs = json.loads((folder / "optimization/verified_inputs.json").read_bytes())
            files = json.loads((folder / "optimization/initial_sources.json").read_bytes())
            records = []
            for call in sorted((folder / "optimization").glob("[0-9][0-9][0-9]")):
                turn = int(call.name)
                payload = json.loads((call / "input.json").read_bytes())
                output = json.loads((call / "output.json").read_bytes())
                files.update(payload["current_project_files"])  # Includes engine-authored .uid files.
                window.prepare(inputs, files, [b for b in attempts if b.model_turn < turn],
                               payload["repair_brief"], None, [], None, "deep")
                error = None
                try:
                    if output["status"] == "context":
                        window.request(DeveloperContext.model_validate(output))
                    elif output["status"] in ("partial", "completed"):
                        window.validate_scope(DeveloperProposal.model_validate(output), files, "deep")
                except InvalidSubmission as exc:
                    error = str(exc)
                records.append({"turn": turn, "decision": output["status"], "error": error,
                    "full_source_files": sorted(window.visible_full_files), "reads_used": window.read_count})
                # Follow ACTUAL accepted history, not a fabricated alternate game.
                if turn in accepted:
                    plan = DeveloperProposal.model_validate(output)
                    files = apply_edits(files, plan.edits)
                    files.update({f.path: f.content for f in plan.files})
                    for path in plan.delete_paths:
                        files.pop(path, None)
                    if label == "before":
                        window.pending = ()
            rows.append({"revision": report["game_revision_id"], "implementation": label, "records": records})
    return {"scope": "Context eligibility only; actual accepted source history retained. No predicted gameplay or model behavior.", "results": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Use a new output file")
    result = recheck(args.run)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for row in result["results"]:
        print(row["revision"], row["implementation"], [(r["turn"], r["error"]) for r in row["records"] if r["error"]])
