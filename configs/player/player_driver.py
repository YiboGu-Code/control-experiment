"""Trusted container entry point. One fresh project, process and save directory per run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hash(path):
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*"), key=lambda p: tuple(part.casefold() for part in p.relative_to(path).parts)):
        if child.is_file():
            digest.update(child.relative_to(path).as_posix().encode() + b"\0")
            digest.update(bytes.fromhex(file_hash(child)))
    return digest.hexdigest()


def checked_path(value, parent):
    path = Path(value).resolve(strict=True)
    if not path.is_relative_to(parent) or path == parent:
        raise ValueError("Path is outside the assigned mount")
    return path


def run(project, request, destination):
    project = checked_path(project, Path("/workspace"))
    request = checked_path(request, Path("/output"))
    output = Path(destination).resolve()
    if not output.is_relative_to(Path("/output")) or output == Path("/output") or output.exists():
        raise ValueError("A new output directory inside /output is required")
    output.mkdir(parents=True)
    job = json.loads(request.read_text())
    expected = job["project_sha256"]
    if tree_hash(project) != expected:
        raise ValueError("Source project checksum mismatch")
    logs = []
    result = {"driver_version": "godot-player.v1", "status": "failed", "project_sha256": expected,
              "fresh_process": True, "fresh_user_data": True, "network": "none", "steps": logs}
    with tempfile.TemporaryDirectory(prefix="player-", dir="/tmp") as directory:
        temp = Path(directory)
        game = temp / "game"
        shutil.copytree(project, game, ignore=shutil.ignore_patterns(".godot"))
        # Imported resources are derived; authored code/config/assets must not change.
        authored = {p.relative_to(game).as_posix(): file_hash(p) for p in game.rglob("*")
                    if p.is_file() and p.suffix not in (".import", ".uid")}
        env = {**os.environ, "HOME": str(temp / "home"), "XDG_DATA_HOME": str(temp / "data"),
               "XDG_CONFIG_HOME": str(temp / "config"), "XDG_CACHE_HOME": str(temp / "cache"),
               "LIBGL_ALWAYS_SOFTWARE": "1", "TZ": "UTC", "LC_ALL": "C.UTF-8"}
        for key in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            Path(env[key]).mkdir()
        actual_job = {**job, "output": str(output)}
        actual_path = temp / "job.json"
        actual_path.write_text(json.dumps(actual_job))
        commands = [("import", ["godot", "--headless", "--path", str(game), "--import"]),
                    ("play", ["xvfb-run", "-a", "-s", "-screen 0 1280x720x24 -nolisten tcp", "godot",
                              "--path", str(game), "--rendering-method", "gl_compatibility", "--audio-driver", "Dummy",
                              "--fixed-fps", "30", "--disable-vsync", "--resolution", "1280x720",
                              "--script", "/opt/gamecraft/player_harness.gd", "--", "--player-job", str(actual_path)])]
        if job["trace"].get("scenario"):
            commands[-1][1].extend(["--scenario", job["trace"]["scenario"]])
        for name, argv in commands:
            # Outer SandboxBackend enforces the hard deadline and kills the entire container.
            process = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
            stdout, stderr = process.stdout[-524288:], process.stderr[-524288:]
            error = re.search(r"(?mi)^\s*(?:SCRIPT ERROR:|ERROR:|Parse Error:|Shader compilation failed)", stdout + "\n" + stderr)
            logs.append({"name": name, "argv": argv, "returncode": process.returncode, "stdout": stdout,
                         "stderr": stderr, "passed": process.returncode == 0 and error is None})
            (output / "driver.json").write_text(json.dumps(result, indent=2))
            if not logs[-1]["passed"]:
                break
        changed = [name for name, digest in authored.items() if not (game / name).is_file() or file_hash(game / name) != digest]
        result["authored_files_unchanged"] = not changed
        result["modified_source_paths"] = changed
        result["original_project_unchanged"] = tree_hash(project) == expected
        if all(s["passed"] for s in logs) and len(logs) == 2 and not changed and result["original_project_unchanged"]:
            result["status"] = "passed" if (output / "receipt.json").exists() else "failed"
        (output / "driver.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({"status": result["status"], "steps": len(logs), "authored_files_unchanged": not changed}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.project, args.request, args.output))
