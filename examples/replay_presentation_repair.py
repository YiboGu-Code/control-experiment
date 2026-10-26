"""Recheck reviewed library-first-002 presentation fixes without any model calls.

Original projects and traces remain immutable. This diagnostic does not generate
missing animation poses or replace formal Player/QA acceptance.
"""
import argparse
import asyncio
import difflib
import json
from pathlib import Path
import shutil

from replay_clock_repair import execute, save
from adaptive_game_generation.runtime.commit import digest_path
from adaptive_game_generation.runtime.workspace import inspect_tree, safe_path
from adaptive_game_generation.roles.player.contracts import InputTrace


PATCHES = {
    "scripts/player_actor.gd": [("player_sprite.scale = Vector2(0.25, 0.25)",
        "player_sprite.scale = Vector2.ONE * (68.0 / float(tex.get_height()))")],
    "scripts/level.gd": [
        ("func _physics_process(delta: float) -> void:\n", "func _physics_process(delta: float) -> void:\n\tflash_timer = maxf(0.0, flash_timer - delta)\n"),
        ("g.scale = Vector2(0.25, 0.25)", "g.scale = Vector2.ONE * (68.0 / float(tex.get_height()))"),
        ("\t\tg.z_index = 5\n", "\t\tg.z_index = 5\n\t\tg.visible = false # Show only after a recorded position is available.\n")],
    "scripts/main.gd": [('"Chapter %d", 42,', '"Chapter %d" % chapter, 42,')],
}


def patch_sources(sources):
    result = dict(sources)
    for path, edits in PATCHES.items():
        for old, new in edits:
            if result.get(path, "").count(old) != 1:
                raise ValueError(f"Reviewed patch no longer matches {path}; inspect this project before adapting")
            result[path] = result[path].replace(old, new)
    return result


def prepare(run, output, selected=()):
    run, output = run.resolve(), output.resolve()
    if output.exists():
        raise ValueError("Choose a new output directory; preserve original and prior diagnostic evidence")
    state = json.loads((run / "state_final.json").read_bytes())
    game = state["game_revisions"][-1]
    public = run / "workspace/public"
    source = safe_path(public, game["project_ref"]["path"])
    inspect_tree(source)
    original_hash = digest_path(source)
    if original_hash != game["project_ref"]["sha256"]:
        raise ValueError("Saved project digest mismatch")
    ref = max((r for r in state["artifacts"] if r["artifact_id"] == "player-report"), key=lambda r: r["version"])
    report_file = safe_path(public, ref["path"])
    if digest_path(report_file) != ref["sha256"]:
        raise ValueError("Player report digest mismatch")
    report = json.loads(report_file.read_bytes())
    if report["project_ref"] != game["project_ref"]:
        raise ValueError("Player report describes a different game")
    requests = []
    for path in sorted((report_file.parent / "requests").glob("*-capture.json")):
        trace_id = path.name.removesuffix("-capture.json")
        if selected and trace_id not in selected:
            continue
        job = json.loads(path.read_bytes())
        InputTrace.model_validate(job["trace"])
        if job["project_sha256"] != original_hash:
            raise ValueError("Trace refers to another project")
        requests.append({"trace_id": trace_id, "source_request": str(path), "request_sha256": digest_path(path), "job": job})
    if not requests or len(requests) > 10 or set(selected) - {r["trace_id"] for r in requests}:
        raise ValueError("Select 1..10 existing captured traces")
    before = {name: (source / name).read_text(encoding="utf-8") for name in PATCHES}
    after = patch_sources(before)
    for variant in ("before", "after"):
        shutil.copytree(source, output / variant / "public/game")
    for name, text in after.items():
        (output / "after/public/game" / name).write_bytes(text.encode("utf-8"))
    repair = {"source_run": str(run), "original_project_path": str(source),
        "original_project_sha256": original_hash, "patched_project_sha256": digest_path(output / "after/public/game"),
        "game_revision": game["revision_id"], "requests": requests,
        "patches": {name: "".join(difflib.unified_diff(before[name].splitlines(True), text.splitlines(True),
                    fromfile=name, tofile=name)) for name, text in after.items()},
        "meaning": "Decay rewind flash on physics ticks; size live/ghost textures uniformly against the 68-unit body; hide unpositioned ghosts; interpolate the chapter label. No texture resampling, generated poses, relaxed replay thresholds or gameplay changes."}
    save(output / "repair.json", repair)
    save(output / "summary.json", {"status": "prepared_not_run", "real_replay_verified": False,
        "online_model_calls": 0, "image_calls": 0, "traces": [r["trace_id"] for r in requests]})
    (output / "instruction.md").write_text(state["task"]["instruction"], encoding="utf-8")
    return repair


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=Path("runs/fixed-loop-library-first-002"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trace", action="append", default=[])
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--player-image", default="gamecraft-player:4.5.1")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("Timeout must be positive")
    repair = prepare(args.run, args.output, args.trace)
    if args.prepare_only:
        print("Prepared immutable comparison projects; real replay remains unverified.")
        return 0
    result = asyncio.run(execute(args.output.resolve(), repair, args.player_image, args.timeout))
    print(json.dumps({k: result[k] for k in ("status", "real_replay_verified", "error") if k in result}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
