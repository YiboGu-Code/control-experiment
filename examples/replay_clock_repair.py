"""Recheck the opt-001 clock fix with saved real input traces and the isolated Player.

No models or image generation. Historical artifacts remain immutable. This is a
diagnostic replay, not a replacement QA verdict or a new benchmark score.
"""
import argparse
import asyncio
from dataclasses import asdict
import difflib
import hashlib
import json
import re
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

from PIL import Image, ImageChops

from adaptive_game_generation.runtime import DockerSandbox
from adaptive_game_generation.runtime.commit import digest_path
from adaptive_game_generation.runtime.workspace import inspect_tree, safe_path
from adaptive_game_generation.roles.developer.diagnostics import source_findings
from adaptive_game_generation.roles.player.contracts import InputTrace
from adaptive_game_generation.roles.player.role import compare_sessions

OLD = "sin(Time.get_ticks_msec() * 0.005)"
NEW = "sin(float(tick_index) / float(PHYSICS_HZ) * 5.0)"
SCRIPT = "scripts/gameplay_final.gd"


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def prepare(run, output, selected=()):
    run, output = run.resolve(), output.resolve()
    if output.exists():
        raise ValueError("Use a new output directory; never overwrite real run evidence")
    state = json.loads((run / "state_final.json").read_bytes())
    game = state["game_revisions"][-1]
    public = run / "workspace/public"
    source = safe_path(public, game["project_ref"]["path"])
    inspect_tree(source)
    original_hash = digest_path(source)
    if original_hash != game["project_ref"]["sha256"]:
        raise ValueError("Saved game differs from its registered project digest")
    reports = [r for r in state["artifacts"] if r["artifact_id"] == "player-report"]
    ref = max(reports, key=lambda r: r["version"])
    report_file = safe_path(public, ref["path"])
    if digest_path(report_file) != ref["sha256"]:
        raise ValueError("Player report digest mismatch")
    report = json.loads(report_file.read_bytes())
    if report["game_revision_id"] != game["revision_id"] or report["project_ref"] != game["project_ref"]:
        raise ValueError("Player traces do not describe the latest saved project")
    requests = []
    for path in sorted((report_file.parent / "requests").glob("*-capture.json")):
        trace_id = path.name.removesuffix("-capture.json")
        if selected and trace_id not in selected:
            continue
        job = json.loads(path.read_bytes())
        InputTrace.model_validate(job["trace"])
        if job["project_sha256"] != original_hash:
            raise ValueError("A saved trace references a different project")
        requests.append({"trace_id": trace_id, "source_request": str(path), "request_sha256": digest_path(path), "job": job})
    if not requests or len(requests) > 10 or set(selected) - {r["trace_id"] for r in requests}:
        raise ValueError("Select 1..10 existing captured traces")
    before = (source / SCRIPT).read_bytes().decode("utf-8")
    if before.count(OLD) != 1 or "tick_index += 1" not in before or "tick_index = 0" not in before:
        raise ValueError("This reviewed opt-001 patch does not match the saved code; inspect it before adapting")
    after = before.replace(OLD, NEW)
    output.mkdir(parents=True)
    for variant in ("before", "after"):
        shutil.copytree(source, output / variant / "public/game")
    patched = output / "after/public/game" / SCRIPT
    patched.write_bytes(after.encode("utf-8"))
    repair = {"source_run": str(run), "original_project_path": str(source), "game_revision": game["revision_id"], "script": SCRIPT,
        "original_project_sha256": original_hash,
        "patched_project_sha256": digest_path(patched.parents[1]),
        "before_source_sha256": hashlib.sha256(before.encode()).hexdigest(),
        "after_source_sha256": digest_path(patched),
        "patch": "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile=SCRIPT, tofile=SCRIPT)),
        "before_static_findings": source_findings({SCRIPT: before}),
        "after_static_findings": source_findings({SCRIPT: after}),
        "meaning": "Use existing resettable simulation ticks at the same 5 radians/second; no gameplay or replay threshold changes",
        "requests": requests}
    save(output / "repair.json", repair)
    (output / "instruction.md").write_text(state["task"]["instruction"], encoding="utf-8")
    save(output / "summary.json", {"status": "prepared_not_run", "real_replay_verified": False,
        "online_model_calls": 0, "image_calls": 0, "traces": [r["trace_id"] for r in requests]})
    return repair


def read_session(directory, job, trace_id, session_id):
    """Validate harness output before reusing the production digest comparison."""
    result = directory / "result"
    driver = json.loads((result / "driver.json").read_bytes())
    receipt = json.loads((result / "receipt.json").read_bytes())
    rows = [json.loads(line) for line in (result / "frames.jsonl").read_text().splitlines()]
    trace = InputTrace.model_validate_json((result / "executed_trace.json").read_bytes())
    expected = InputTrace.model_validate(job["trace"])
    if (driver["status"] != "passed" or driver["project_sha256"] != job["project_sha256"]
            or driver["network"] != "none" or not all(driver[k] is True for k in
                ("fresh_process", "fresh_user_data", "authored_files_unchanged", "original_project_unchanged"))):
        raise ValueError("Player isolation, source integrity or engine checks failed")
    count = expected.duration_frames
    if (receipt["completed_frames"] != count or receipt["event_count"] != len(expected.events)
            or (receipt["width"], receipt["height"], receipt["fps"]) != (1280, 720, 30)
            or [r["frame"] for r in rows] != list(range(count)) or trace != expected):
        raise ValueError("Incomplete frames, receipt or real input trace mismatch")
    events = [json.loads(line) for line in (result / "events.jsonl").read_text().splitlines()]
    if events != job["trace"]["events"]:
        raise ValueError("Injected event journal does not match the trace")
    if any(not isinstance(row.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", row[key])
           for row in rows for key in ("state_sha256", "pixels_sha256")):
        raise ValueError("Missing or malformed per-frame digest")
    for frame in job["checkpoints"]:
        image = Image.open(result / f"frame_{frame:04}.png").convert("RGBA")
        if image.size != (1280, 720) or hashlib.sha256(image.tobytes()).hexdigest() != rows[frame]["pixels_sha256"]:
            raise ValueError("Screenshot does not match the recorded pixels")
        if digest_path(result / f"frame_{frame:04}.json") != rows[frame]["state_sha256"]:
            raise ValueError("Scene snapshot does not match the recorded state")
    return SimpleNamespace(status="passed", session_id=session_id, trace_id=trace_id,
        state_digests=tuple(r["state_sha256"] for r in rows), pixel_digests=tuple(r["pixels_sha256"] for r in rows)), trace


def audit_saved_replay(output, run=None):
    """Recompute the saved result without Docker or following server absolute paths.

    This verifies the returned files, not an independent attestation of the server.
    Only the declared traces and the recorded engine/environment are covered.
    """
    output = output.resolve()
    repair = json.loads((output / "repair.json").read_bytes())
    reported = json.loads((output / "summary.json").read_bytes())
    preflight = json.loads((output / "docker_preflight.json").read_bytes())
    if (len(preflight) != 2 or any(p["returncode"] != 0 for p in preflight)
            or json.loads(preflight[-1]["stdout"]) != reported.get("engine_image_id")
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", reported.get("engine_image_id", ""))):
        raise ValueError("Missing or inconsistent frozen engine image provenance")
    projects = {v: safe_path(output, f"{v}/public/game") for v in ("before", "after")}
    digests = {"before": repair["original_project_sha256"], "after": repair["patched_project_sha256"]}
    inventories = {}
    for variant, project in projects.items():
        inspect_tree(project)
        if digest_path(project) != digests[variant]:
            raise ValueError(f"{variant} project differs from its recorded digest")
        inventories[variant] = {p.relative_to(project).as_posix(): digest_path(p)
                                for p in project.rglob("*") if p.is_file()}
    if inventories["before"].keys() != inventories["after"].keys():
        raise ValueError("Clock repair unexpectedly added or removed source files")
    changed = [p for p in inventories["before"] if inventories["before"][p] != inventories["after"][p]]
    before_code, after_code = ((projects[v] / SCRIPT).read_bytes() for v in ("before", "after"))
    if (changed != [SCRIPT] or before_code.count(OLD.encode()) != 1
            or before_code.replace(OLD.encode(), NEW.encode()) != after_code
            or digest_path(projects["before"] / SCRIPT) != repair["before_source_sha256"]
            or digest_path(projects["after"] / SCRIPT) != repair["after_source_sha256"]):
        raise ValueError("Source change does not match the reviewed simulation-clock patch")
    requests = repair["requests"]
    trace_ids = [r["trace_id"] for r in requests]
    if not 1 <= len(requests) <= 10 or len(set(trace_ids)) != len(trace_ids):
        raise ValueError("Expected 1..10 distinct recorded traces")
    if any(not isinstance(t, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", t) for t in trace_ids):
        raise ValueError("Invalid trace identifier")
    original_verified = None
    if run is not None:
        run = run.resolve()
        state = json.loads((run / "state_final.json").read_bytes())
        game = state["game_revisions"][-1]
        original_verified = (game["revision_id"] == repair["game_revision"] and
            game["project_ref"]["sha256"] == digest_path(safe_path(run / "workspace/public", game["project_ref"]["path"])) == digests["before"])
        if not original_verified or (output / "instruction.md").read_text(encoding="utf-8") != state["task"]["instruction"]:
            raise ValueError("Replay source does not match the local registered game/instruction")
        ref = max((r for r in state["artifacts"] if r["artifact_id"] == "player-report"), key=lambda r: r["version"])
        report_path = safe_path(run / "workspace/public", ref["path"])
        if digest_path(report_path) != ref["sha256"]:
            raise ValueError("Original Player report digest mismatch")
        source_report = json.loads(report_path.read_bytes())
        if source_report["project_ref"] != game["project_ref"]:
            raise ValueError("Original Player report is for a different game")
        for request in requests:
            source_job = safe_path(report_path.parent, f"requests/{request['trace_id']}-capture.json")
            if digest_path(source_job) != request["request_sha256"] or json.loads(source_job.read_bytes()) != request["job"]:
                raise ValueError("Recorded trace differs from the original Player input")
    comparisons = {v: [] for v in projects}
    sessions, captures, trace_checks, warnings = [], {}, [], []
    screenshots = 0
    for variant in projects:
        expected_ids = {t + "-" + m for t in trace_ids for m in ("capture", "replay-1", "replay-2")}
        if {p.name for p in (output / variant / "sessions").iterdir() if p.is_dir()} != expected_ids:
            raise ValueError(f"{variant} session coverage is incomplete or unexpected")
        for request in requests:
            trace_id = request["trace_id"]
            capture = None
            for mode in ("capture", "replay-1", "replay-2"):
                identity = trace_id + "-" + mode
                directory = safe_path(output, f"{variant}/sessions/{identity}")
                inspect_tree(directory)
                job = json.loads((directory / "request.json").read_bytes())
                if job != {**request["job"], "project_sha256": digests[variant]}:
                    raise ValueError("Session input changed between capture/replay or before/after")
                if json.loads((directory / "process.json").read_bytes())["returncode"] != 0:
                    raise ValueError("Recorded engine process failed")
                session, trace = read_session(directory, job, trace_id, identity)
                sessions.append({"variant": variant, "session_id": identity, "frames": trace.duration_frames})
                screenshots += len(job["checkpoints"])
                driver = json.loads((directory / "result/driver.json").read_bytes())
                for step in driver.get("steps", []):
                    log = step.get("stderr", "")
                    if log.strip():
                        warnings.append({"variant": variant, "session_id": identity, "step": step.get("name"), "stderr": log})
                if mode == "capture":
                    capture = session
                    captures[variant, trace_id] = session
                else:
                    comparisons[variant].append(compare_sessions(capture, session).model_dump(mode="json"))
    if comparisons != reported["comparisons"] or sessions != reported["sessions"]:
        raise ValueError("Saved summary does not match recomputed complete session evidence")
    for request in requests:
        trace_id = request["trace_id"]
        screenshot_pairs = []
        for variant in projects:
            for frame in request["job"]["checkpoints"]:
                paths = [output / variant / "sessions" / (trace_id + "-" + mode) / "result" / f"frame_{frame:04}.png"
                         for mode in ("capture", "replay-1")]
                with Image.open(paths[0]) as a, Image.open(paths[1]) as b:
                    bands = ImageChops.difference(a.convert("RGBA"), b.convert("RGBA")).split()
                    mask = bands[0]
                    for band in bands[1:]:
                        mask = ImageChops.lighter(mask, band)
                    screenshot_pairs.append({"variant": variant, "frame": frame,
                        "different_pixels": sum(mask.histogram()[1:]), "difference_bbox": mask.getbbox()})
        trace_checks.append({"trace_id": trace_id, "duration_frames": request["job"]["trace"]["duration_frames"],
            "before_passed": sum(c["status"] == "passed" for c in comparisons["before"] if c["trace_id"] == trace_id),
            "after_passed": sum(c["status"] == "passed" for c in comparisons["after"] if c["trace_id"] == trace_id),
            "cross_variant_state_match": captures["before", trace_id].state_digests == captures["after", trace_id].state_digests,
            "checkpoint_pixel_checks": screenshot_pairs})
    verified = len(comparisons["after"]) == 2 * len(requests) and all(c["status"] == "passed" for c in comparisons["after"])
    if reported["real_replay_verified"] is not verified or reported["status"] != ("passed" if verified else "failed"):
        raise ValueError("Saved overall verdict disagrees with complete replay comparisons")
    return {"status": "passed" if verified else "failed", "real_replay_verified": verified,
        "mode": "Offline audit of user-supplied real engine execution; no new engine or model calls",
        "engine_image_id": reported["engine_image_id"], "original_project_verified_locally": original_verified,
        "changed_files": changed, "trace_count": len(requests), "session_count": len(sessions),
        "total_recorded_frames": sum(s["frames"] for s in sessions),
        "after_compared_frame_pairs": 2 * sum(t["duration_frames"] for t in trace_checks),
        "verified_checkpoint_images": screenshots, "trace_checks": trace_checks,
        "comparisons": comparisons, "engine_stderr": warnings,
        "scope": "These input traces in the recorded image/render environment; not full gameplay/QA acceptance or cross-platform proof"}


async def execute(output, repair, image, timeout):
    summary = {"status": "running", "real_replay_verified": False, "online_model_calls": 0, "image_calls": 0,
               "engine_image": image, "comparisons": {"before": [], "after": []}, "sessions": []}
    save(output / "summary.json", summary)
    runtime = shutil.which("docker")
    preflight = []
    if runtime:
        # Read-only checks before any container or session folder is created.
        for args in (("info", "--format", "{{json .ServerVersion}}"),
                     ("image", "inspect", image, "--format", "{{json .Id}}")):
            try:
                process = await asyncio.to_thread(subprocess.run, [runtime, *args],
                    capture_output=True, text=True, timeout=20)
                preflight.append({"args": args, "returncode": process.returncode,
                                  "stdout": process.stdout[-4096:], "stderr": process.stderr[-4096:]})
            except (OSError, subprocess.TimeoutExpired) as exc:
                preflight.append({"args": args, "returncode": None, "error": type(exc).__name__})
            if preflight[-1]["returncode"] != 0:
                break
    save(output / "docker_preflight.json", preflight)
    if not runtime or len(preflight) != 2 or any(p["returncode"] != 0 for p in preflight):
        summary.update(status="runtime_unavailable", error="Docker daemon or preloaded Player image is unavailable; no download or host-execution fallback was used")
        save(output / "summary.json", summary)
        return summary
    try:
        frozen_image = json.loads(preflight[-1]["stdout"])
        if not isinstance(frozen_image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", frozen_image):
            raise ValueError("Docker did not return a verifiable local image ID")
        summary["engine_image_id"] = frozen_image
        backend = DockerSandbox(frozen_image, memory_mb=2048, tmpfs_mb=1024)
        for variant, digest in (("before", repair["original_project_sha256"]), ("after", repair["patched_project_sha256"])):
            public = output / variant / "public"
            for request in repair["requests"]:
                capture, executed_trace = None, None
                for mode in ("capture", "replay-1", "replay-2"):
                    identity = request["trace_id"] + "-" + mode
                    directory = output / variant / "sessions" / identity
                    directory.mkdir(parents=True)
                    job = {**request["job"], "project_sha256": digest}
                    if executed_trace is not None:
                        job["trace"] = executed_trace.model_dump(mode="json", exclude_none=True)
                    save(directory / "request.json", job)
                    process = await backend.run(SimpleNamespace(public=public, staging=directory),
                        ("python3", "/opt/gamecraft/player_driver.py", "--project", "/workspace/game",
                         "--request", "/output/request.json", "--output", "/output/result"), timeout)
                    save(directory / "process.json", asdict(process))
                    if process.returncode:
                        raise ValueError(f"Engine session failed: {variant}/{identity}; inspect process.json and driver.json")
                    session, actual = read_session(directory, job, request["trace_id"], identity)
                    summary["sessions"].append({"variant": variant, "session_id": identity, "frames": actual.duration_frames})
                    if mode == "capture":
                        capture, executed_trace = session, actual
                    else:
                        summary["comparisons"][variant].append(compare_sessions(capture, session).model_dump(mode="json"))
                    save(output / "summary.json", summary)
        if digest_path(Path(repair["original_project_path"])) != repair["original_project_sha256"]:
            raise ValueError("Historical source project changed during replay")
        summary["original_project_unchanged"] = True
        summary["real_replay_verified"] = bool(summary["comparisons"]["after"]) and all(
            c["status"] == "passed" for c in summary["comparisons"]["after"])
        summary["status"] = "passed" if summary["real_replay_verified"] else "failed"
    except Exception as exc:
        summary.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    save(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=Path("runs/fixed-loop-developer-opt-001"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trace", action="append", default=[], help="Saved capture trace ID; default: all captured traces")
    parser.add_argument("--player-image", default="gamecraft-player:4.5.1")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--verify-only", action="store_true", help="Audit an existing --output without an engine/model call")
    parser.add_argument("--audit-output", type=Path, help="New analysis directory for --verify-only (default: --output/analysis)")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.verify_only:
        if args.prepare_only or args.trace:
            parser.error("--verify-only audits the entire saved run; do not combine with --prepare-only/--trace")
        destination = args.audit_output or args.output / "analysis"
        if destination.exists():
            parser.error("Use a new audit output directory; preserve previous analysis")
        try:
            result = audit_saved_replay(args.output, args.run)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            result = {"status": "invalid_evidence", "real_replay_verified": False, "error": f"{type(exc).__name__}: {exc}"}
        save(destination / "verification.json", result)
        print(json.dumps({k: result[k] for k in ("status", "real_replay_verified", "error") if k in result}))
        return 0 if result["status"] == "passed" else 1
    if args.audit_output:
        parser.error("--audit-output requires --verify-only")
    repair = prepare(args.run, args.output, args.trace)
    if args.prepare_only:
        print("Prepared two immutable comparison projects; real replay has NOT run.")
        return 0
    summary = asyncio.run(execute(args.output.resolve(), repair, args.player_image, args.timeout))
    print(json.dumps({k: v for k, v in summary.items() if k not in ("comparisons", "sessions")}, indent=2))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
