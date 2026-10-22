"""Real QA model reviewing the original Time Loop instruction and recorded real play."""

import argparse
import asyncio
import hashlib
import json
import re
import runpy
import shutil
import traceback
from pathlib import Path

from adaptive_game_generation.models import ArtifactRef, ProposedUpdates, ResourceLimits, RoleResult
from adaptive_game_generation.providers import GatewayConfig, OpenAICompatibleTextProvider
from adaptive_game_generation.roles.qa_reviewer import QAConfig, QAReport, QAReviewer
from adaptive_game_generation.runtime import Executor, RuntimeConfig, Services, Workspace
from adaptive_game_generation.runtime.commit import digest_path

ROOT = Path(__file__).resolve().parents[1]
PLAYER_REVIEW = ROOT / "docs/examples/player_review"
HELPERS = runpy.run_path(str(ROOT / "examples/player_demo.py"))
write_json = HELPERS["write_json"]


async def seed_recorded_inputs(executor, recorded=None):
    await HELPERS["seed_recorded_inputs"](executor, recorded)
    original = RoleResult.model_validate_json((PLAYER_REVIEW / "role_result.json").read_bytes())
    async def import_player(context):
        replacements = {ref.key: ref for ref in context.state.artifacts}
        pending, refs = {}, []
        for ref in original.artifact_refs:
            path = PLAYER_REVIEW / "deliverables" / ref.path.split("/", 3)[3]
            if digest_path(path) != ref.sha256:
                raise ValueError("Recorded Player input checksum mismatch")
            pending[ref.key] = (ref, path.read_bytes())
        def rebase(value):
            if isinstance(value, dict):
                if {"artifact_id", "version", "path", "kind", "created_by"} <= value.keys():
                    ref = ArtifactRef.model_validate(value)
                    return replacements[ref.key].model_dump(mode="json")
                return {k: rebase(v) for k, v in value.items()}
            if isinstance(value, list):
                return [rebase(v) for v in value]
            return value
        while pending:
            progress = False
            for key, (original_ref, raw) in list(pending.items()):
                relative = original_ref.path.split("/", 3)[3]
                if relative.startswith("logs/") or original_ref.artifact_id == "player-report":
                    try:
                        value = rebase(json.loads(raw))
                    except KeyError:
                        continue
                    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
                logical = await context.tools.write_bytes(relative, raw)
                ref = original_ref.revised(path=logical, sha256=hashlib.sha256(raw).hexdigest())
                replacements[key] = ref
                refs.append(ref)
                del pending[key]
                progress = True
            if not progress:
                raise ValueError("Recorded Player references cannot be rebased")
        evidence = tuple(e.revised(artifact_refs=tuple(replacements[r.key] for r in e.artifact_refs)) for e in original.proposed_updates.evidence)
        return RoleResult(role="player", request_id=context.request.request_id, state_revision=context.request.state_revision,
            status="partial", summary="Imported recorded real Player sessions; only logical references rebased, no new plays or model calls",
            artifact_refs=tuple(refs), evidence_refs=tuple(e.evidence_id for e in evidence), proposed_updates=ProposedUpdates(evidence=evidence))
    result = await executor.execute(executor.request("player", "Import recorded real Player evidence for QA",
        ("Preserve executed inputs, state journals and image bytes",), request_id="import-recorded-player"), import_player)
    if result.status != "partial":
        raise RuntimeError(f"Player import failed: {result.error}")


async def run_live(output, env_file):
    config = GatewayConfig.from_env(env_file, prefix="QA")
    recorded = HELPERS["load_recorded_inputs"]()
    source, source_path, instruction, *_ = recorded[0]
    if output.exists():
        raise ValueError("Review destination must be a new directory")
    output.mkdir(parents=True)
    (output / "instruction.md").write_bytes(instruction)
    write_json(output / "source.json", {**source, "player_review": "docs/examples/player_review",
        "game_source": "examples/inputs/player_developer_game", "project_sha256": recorded[3]["game-project"][0].sha256,
        "preconditions": "Recorded real upstream model outputs and real isolated Godot play/replay. Mock images unchanged. Only QA calls are new; no hidden evaluation inputs."})
    write_json(output / "provider_config.json", config.public_settings())
    workspace, task = Workspace.prepare(output / "workspace", source_path.parent, task_id=source["task_id"], benchmark_snapshot=source["snapshot"])
    task = task.revised(environment_constraints=("Write all model prompts and authored output in English. Preserve the original instruction.",
        "Review the recorded Developer project and actual Player evidence; images remain experimental mocks.",
        "The text model receives raw scene properties and source text, not image pixels. No new play sessions are claimed."))
    qa_config = QAConfig(max_output_tokens=config.max_output_tokens)
    limits = ResourceLimits(max_model_calls=qa_config.max_model_turns, max_input_tokens=4000000,
        max_output_tokens=config.max_output_tokens * qa_config.max_model_turns, max_image_calls=0,
        max_tool_calls=3000, max_tool_seconds=1600.0, max_wall_seconds=1800.0)
    async with OpenAICompatibleTextProvider(config, capture_directory=workspace.private / "model_calls") as provider:
        executor = Executor.create(workspace, task, run_id="qa-live", controller="fixed_loop", budget=limits,
            config=RuntimeConfig(role_limits=limits, tool_timeout_seconds=max(300, config.timeout_seconds),
                invocation_timeout_seconds=1800, service_configuration_id=config.configuration_id + ":qa"), services=Services(text=provider))
        await seed_recorded_inputs(executor, recorded)
        before = executor.state
        write_json(output / "state_before.json", before.model_dump(mode="json"))
        request = executor.request("qa_reviewer", "Review Time Loop against the original Benchmark instruction using actual current evidence",
            ("Cover every acceptance criterion, audit design omissions and conflicts, and cite inspected evidence",
             "Publish actionable issues and a justified delivery readiness decision without modifying the game",
             "Distinguish observed defects, missing tests, exact replay differences and mock-art limitations"), request_id="qa-initial")
        write_json(output / "role_request.json", request.model_dump(mode="json"))
        async def invoke(context):
            try:
                return await QAReviewer(qa_config)(context)
            except Exception as exc:
                write_json(workspace.private / "qa_error.json", {"type": type(exc).__name__,
                    "frames": [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
                               for f in traceback.extract_tb(exc.__traceback__)]})
                raise
        result = await executor.execute(request, invoke)
        state = executor.state
    write_json(output / "role_result.json", result.model_dump(mode="json"))
    write_json(output / "state_after.json", state.model_dump(mode="json"))
    (output / "tool_audit.jsonl").write_bytes((workspace.private / "tools.jsonl").read_bytes())
    shutil.copytree(workspace.private / "model_calls", output / "model_calls")
    responses = []
    for directory in sorted((output / "model_calls").iterdir()):
        if (directory / "request.json").exists():
            body = json.loads((directory / "request.json").read_bytes())
            write_json(directory / "prompt.json", json.loads(body["messages"][-1]["content"]))
        if (directory / "response.json").exists():
            responses.append(json.loads((directory / "response.json").read_bytes()))
    for ref in result.artifact_refs:
        target = output / "deliverables" / ref.path.split("/", 3)[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(workspace.read_public(ref.path).read_bytes())
    # A complete portable public input snapshot; credentials/private ledgers are excluded.
    for ref in before.artifacts:
        path, target = workspace.read_public(ref.path), output / "upstream_artifacts" / ref.path
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(path, target)
        else:
            target.write_bytes(path.read_bytes())
    report_path = output / "deliverables/qa_report.json"
    report = QAReport.model_validate_json(report_path.read_bytes()) if report_path.exists() else None
    checks = {"original_instruction_unchanged": source_path.read_bytes() == instruction == workspace.read_public("input/instruction.md").read_bytes(),
        "real_model_completed": bool(responses) and len(responses) == state.ledger.usage.model_calls and all(r["choices"][0]["finish_reason"] == "stop" for r in responses),
        "review_committed": result.status == "completed" and state.state_revision == 5 and report is not None,
        "all_criteria_reviewed": report is not None and len(report.proposal.criteria) == sum(len(r.acceptance_criteria) for r in before.requirements),
        "raw_play_evidence_checked": report is not None and report.evidence_integrity["frames"] == 5406 and report.evidence_integrity["screenshots"] == 47,
        "honest_not_ready": state.readiness is not None and not state.readiness.ready,
        "game_and_evidence_unchanged": state.game_revisions == before.game_revisions and state.evidence == before.evidence and digest_path(workspace.read_public(before.game_revisions[-1].project_ref.path)) == before.game_revisions[-1].project_ref.sha256,
        "design_and_art_unchanged": state.design == before.design and state.assets == before.assets and state.ledger.usage.image_calls == 0,
        "usage_matches_provider": state.ledger.usage.token_usage_complete and state.ledger.usage.input_tokens == sum(r["usage"]["prompt_tokens"] for r in responses) and state.ledger.usage.output_tokens == sum(r["usage"]["completion_tokens"] for r in responses),
        "english_model_output": bool(responses) and all(not re.search(r"[\u3400-\u9fff]", r["choices"][0]["message"]["content"]) for r in responses)}
    summary = {"mode": "live-QA/recorded-real-play/mock-art", "status": "completed" if all(checks.values()) else "failed",
        "role_status": result.status.value, "model": config.model, "ready": state.readiness.ready if state.readiness else None,
        "criteria": len(report.proposal.criteria) if report else 0, "issues": len(state.issues),
        "requirement_statuses": {s: sum(r.status == s for r in state.requirements) for s in ("verified_pass", "verified_fail", "implemented_unverified", "not_implemented")},
        "usage": state.ledger.usage.model_dump(mode="json"), "checks": checks, "error": result.error.model_dump(mode="json") if result.error else None}
    write_json(output / "summary.json", summary)
    lines = ["# QA Reviewer review: Time Loop", "", f"Integration: **{summary['status']}**. Delivery ready: **{summary['ready']}**.", "",
        "The real QA model reviewed recorded real upstream evidence. No upstream model or engine session was rerun; only image generation remains mock.", "",
        "## Inputs", "", "- [Original instruction](instruction.md)", "- [Provenance](source.json)", "- [Full input state](state_before.json)",
        "- [Role request](role_request.json)", "- [Public provider settings](provider_config.json)",
        "- Every input ArtifactRef.path resolves under `upstream_artifacts/`, including the unchanged game, raw frame journals and real PNGs.",
        "", "## Complete model I/O", ""]
    for directory in sorted((output / "model_calls").iterdir()):
        for name in ("request.json", "prompt.json", "response.json", "response.txt", "metadata.json"):
            if (directory / name).exists():
                lines.append(f"- [Call {directory.name}: {name}](model_calls/{directory.name}/{name})")
    lines += ["", "## Outputs", "", "- [Readable QA handoff](deliverables/qa_handoff.md)", "- [Full QA report](deliverables/qa_report.json)",
        "- [Criterion coverage](deliverables/coverage.json)", "- [Issue list](deliverables/issues.json)", "- [Readiness](deliverables/readiness.json)",
        "- [Inspected source catalog](deliverables/evidence_catalog.json)", "- [Role result](role_result.json)", "- [Committed state](state_after.json)",
        "- [Summary and accounting](summary.json)", "- [Tool audit](tool_audit.jsonl)", "",
        "This is an internal review, not an official score. A completed QA role does not mean the game is ready. Text-only semantic judgments remain reviewable model judgments.", ""]
    (output / "review.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def run_demo(output, *, env_file=ROOT / ".env"):
    return asyncio.run(run_live(output, env_file))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    summary = run_demo(args.output, env_file=args.env_file)
    print(json.dumps(summary))
    raise SystemExit(0 if summary["status"] == "completed" else 1)
