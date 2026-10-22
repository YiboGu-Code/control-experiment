"""Opt-in real rendering smoke test; uses the recorded Developer game unchanged."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from adaptive_game_generation.roles.player.contracts import InputTrace, normalize_trace
from adaptive_game_generation.runtime import DockerSandbox, Workspace
from adaptive_game_generation.runtime.commit import digest_path
from adaptive_game_generation.runtime.workspace import PublicResource

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("GAMECRAFT_PLAYER_RUNTIME_TESTS") != "1", reason="Opt in to real isolated Player rendering")
def test_actual_input_and_rendered_frames(tmp_path):
    destination = Path(os.environ.get("GAMECRAFT_PLAYER_RUNTIME_OUTPUT", str(tmp_path / "runtime")))
    game = ROOT / "examples/inputs/player_developer_game"
    task = ROOT / "benchmark_resources/FreedomIntelligence-gamecraft-bench-a433475/tasks/platformer-time-loop"
    workspace, _ = Workspace.prepare(destination, task, task_id="platformer-time-loop",
        resources=(PublicResource(name="game", source=game),))
    invocation = workspace.begin("player", "render-smoke")
    trace = normalize_trace(InputTrace.model_validate({"scenario": "near_rewind", "duration_frames": 30,
        "events": [{"frame": 3, "type": "key_down", "keycode": "D"}, {"frame": 15, "type": "key_up", "keycode": "D"},
                   {"frame": 16, "type": "key_press", "keycode": "SPACE"}]}))
    job = {"harness_version": "godot-player.v1", "trace": trace.wire(), "checkpoints": [0, 29],
        "project_sha256": digest_path(game), "seed": 1337}
    (invocation.staging / "job.json").write_text(json.dumps(job), encoding="utf-8")
    backend = DockerSandbox(image=os.environ.get("PLAYER_SANDBOX_IMAGE", "gamecraft-player:4.5.1"),
        cpus=2.0, memory_mb=1536, tmpfs_mb=512)
    result = asyncio.run(backend.run(invocation, ("python3", "/opt/gamecraft/player_driver.py", "--project",
        "/workspace/input/resources/game", "--request", "/output/job.json", "--output", "/output/session"), 180))
    output = invocation.staging / "session"
    diagnostic = (output / "driver.json").read_text() if (output / "driver.json").exists() else result.stderr
    assert result.returncode == 0, diagnostic
    assert json.loads((output / "executed_trace.json").read_text()) == trace.wire()
    assert len((output / "frames.jsonl").read_text().splitlines()) == 30
    assert (output / "frame_0029.png").is_file()
    first, last = [json.loads((output / f"frame_{frame:04}.json").read_text()) for frame in (0, 29)]
    def player_position(snapshot):
        return next(n["properties"]["position"] for n in snapshot["nodes"] if n["class"] == "CharacterBody2D")
    assert player_position(first) != player_position(last), "Actual key input must move the actual character"
    assert player_position(last)[1] < player_position(first)[1], "A one-frame Space tap must reach the real physics jump handler"
    assert digest_path(game) == job["project_sha256"]
