"""Read saved real run evidence without model/engine execution or source mutation."""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
from statistics import mean


def analyze(root):
    state = json.loads((root / "state_final.json").read_bytes())
    experiment = json.loads((root / "experiment.json").read_bytes())
    roles = []
    for event in state["ledger"]["events"]:
        p = event["payload"]
        if p["kind"] == "role_call_finished":
            roles.append({"request_id": p["result"]["request_id"], "role": p["result"]["role"],
                "status": p["result"]["status"], "minutes": p["usage"]["wall_seconds"] / 60,
                "usage": p["usage"], "error": p["result"].get("error")})
    calls = []
    for role in ("developer", "developer_visual"):
        for directory in sorted((root / "model_calls" / role).glob("*")):
            meta = json.loads((directory / "metadata.json").read_bytes())
            request = json.loads((directory / "request.json").read_bytes())
            response = json.loads((directory / "response.json").read_bytes()) if (directory / "response.json").exists() else {}
            user = request["messages"][-1]["content"]
            prompt = json.loads(user if isinstance(user, str) else user[0]["text"])
            content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
            try:
                output = json.loads(content)
            except ValueError:
                output = {}
            usage = response.get("usage") or {}
            calls.append({"role": role, "call": directory.name, "status": meta["status"],
                "seconds": (datetime.fromisoformat(meta["finished_at"]) - datetime.fromisoformat(meta["started_at"])).total_seconds(),
                "stage": prompt.get("optimization", {}).get("stage"), "effort": request.get("reasoning_effort"),
                "enable_thinking": request.get("enable_thinking"), "max_tokens": request.get("max_tokens"),
                "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                "prompt_bytes": len(json.dumps(prompt, ensure_ascii=False).encode()),
                "response_bytes": len(content.encode()), "decision": output.get("status"),
                "full_write_files": [f["path"] for f in output.get("files", [])],
                "edit_files": [f["path"] for f in output.get("edits", [])], "reads": output.get("reads", []),
                "proposal_feedback": prompt.get("proposal_feedback", []), "source": directory.as_posix()})
    reports = []
    for ref in state["artifacts"]:
        if ref["artifact_id"] not in ("developer-report", "player-report", "qa-report", "art-manifest"):
            continue
        doc = json.loads((root / "workspace/public" / ref["path"]).read_bytes())
        item = {"id": ref["artifact_id"], "version": ref["version"], "path": ref["path"], "game_revision": doc.get("game_revision_id")}
        if item["id"] == "developer-report":
            item.update(builds=[{"turn": a["model_turn"], "status": a["status"]} for a in doc["build_attempts"]],
                rejections=doc["proposal_rejections"], optimization_calls=doc.get("optimization_calls", []),
                self_review_completed=doc.get("self_review_completed"), packaged_traces=len(doc.get("packaged_traces", [])),
                limitations=doc["limitations"])
        elif item["id"] == "player-report":
            item.update(replay_counts=dict(Counter(c["status"] for c in doc["comparisons"])), comparisons=doc["comparisons"])
        elif item["id"] == "qa-report":
            item.update(gates=doc["gates"], readiness=doc["readiness"])
        else:
            item.update(normalization_counts=dict(Counter((a.get("normalization") or {}).get("status", "not_enabled") for a in doc["assets"])))
        reports.append(item)
    main = [c for c in calls if c["role"] == "developer"]
    rejected = {(d["game_revision"], r["model_turn"]) for d in reports if d["id"] == "developer-report" for r in d["rejections"]}
    return {"run": root.as_posix(), "summary": json.loads((root / "summary.json").read_bytes()),
        "conditions": {k: experiment[k] for k in ("instruction_sha256", "roles", "providers", "total_budget", "engines")},
        "role_calls": roles, "developer_calls": calls, "reports": reports,
        "metrics": {"total_role_minutes": sum(r["minutes"] for r in roles),
            "developer_minutes": sum(r["minutes"] for r in roles if r["role"] == "developer"),
            "main_model_minutes": sum(c["seconds"] for c in main) / 60,
            "visual_model_minutes": sum(c["seconds"] for c in calls if c["role"] == "developer_visual") / 60,
            "main_calls": len(main), "visual_calls": len(calls) - len(main),
            "main_stage_counts": dict(Counter(c["stage"] or "legacy" for c in main)),
            "main_mean_input_tokens_confirmed": mean(c["input_tokens"] for c in main if c["input_tokens"] is not None),
            "main_max_input_tokens_confirmed": max(c["input_tokens"] or 0 for c in main),
            "main_output_tokens_confirmed": sum(c["output_tokens"] or 0 for c in main),
            "main_reasoning_tokens_confirmed": sum(c["reasoning_tokens"] or 0 for c in main),
            "main_decision_counts": dict(Counter(c["decision"] for c in main)),
            "edit_proposals": sum(bool(c["edit_files"]) for c in main), "rejected_proposals": len(rejected)},
        "limits": "Observational comparison, not a controlled ablation. Confirmed tokens exclude interrupted calls; ledger reservations are not measured usage."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Use a new output file")
    data = [analyze(root) for root in args.runs]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{ "run": d["run"], **d["metrics"]} for d in data], indent=2))
