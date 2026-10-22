"""Opt-in diagnosis of the immutable run 003 delivery, not a new generated game."""

import asyncio
import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest

from adaptive_game_generation.runtime import DockerSandbox
from adaptive_game_generation.runtime.workspace import InvocationWorkspace

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "runs/fixed-loop-real-images-003/delivery/game"
pytestmark = pytest.mark.skipif(
    os.environ.get("GAMECRAFT_RUN003_DIAGNOSTICS") != "1" or not SOURCE.is_dir(),
    reason="Opt-in local Docker diagnosis requires the recorded run 003 delivery",
)


def probe(tmp_path, name, script):
    destination = os.environ.get("GAMECRAFT_RUN003_OUTPUT")
    root = (Path(destination).absolute() if destination else tmp_path) / name
    public, stage = root / "public", root / "output"
    public.mkdir(parents=True)  # Deliberately fail rather than overwrite evidence.
    game = stage / "game"
    shutil.copytree(SOURCE, game)
    # Only the isolated diagnostic copy gains a monitoring autoload. Original
    # implementation bytes are retained; these are not shipping input traces.
    project = (game / "project.godot").read_text(encoding="utf-8")
    assert project.count("[autoload]") == 1
    project = project.replace("[autoload]", '[autoload]\nDiagnosticProbe="*res://diagnostic_probe.gd"')
    (game / "project.godot").write_text(project, encoding="utf-8")
    (game / "diagnostic_probe.gd").write_text(script, encoding="utf-8")
    workspace = InvocationWorkspace(public, stage, "artifacts/developer/diagnostic")
    sandbox = DockerSandbox("gamecraft-godot:4.5.1")
    async def run():
        logs = []
        for step, argv in [
            ("import", ("godot", "--headless", "--path", "/output/game", "--import")),
            ("probe", ("godot", "--headless", "--fixed-fps", "30", "--path", "/output/game",
                       "--quit-after", "1100", "--", "--scenario", "chapter1_level1")),
        ]:
            result = await sandbox.run(workspace, argv, timeout=90)
            logs.append({"step": step, **asdict(result)})
        return logs
    logs = asyncio.run(run())
    (root / "logs.json").write_text(json.dumps(logs, indent=2), encoding="utf-8")
    assert logs[0]["returncode"] == 0
    return logs[-1]


def test_recorded_natural_rewind_stores_recording_without_spawning_ghost(tmp_path):
    result = probe(tmp_path, "natural-rewind", """extends Node
var elapsed: float = 0.0
func _physics_process(delta: float) -> void:
    elapsed += delta
    if elapsed < 31.5:
        return
    var manager = get_node_or_null("/root/LoopManager")
    var level = get_tree().current_scene
    if manager == null or level == null or not level.has_method("get_spawn_position"):
        return
    var state = manager.get_game_state()
    print("RUN003_PROBE:" + JSON.stringify({"loops": state.loop_count,
        "recordings": state.recordings.size(), "ghost_nodes": level.ghost_nodes.size(),
        "timer": state.timer}))
    get_tree().quit()
""")
    line = next(line for line in result["stdout"].splitlines() if line.startswith("RUN003_PROBE:"))
    observed = json.loads(line.split(":", 1)[1])
    assert observed["loops"] == 2 and observed["recordings"] == 1
    assert observed["ghost_nodes"] == 0  # Confirms the historical defect, not a fix.
    assert 27 < observed["timer"] < 30


def test_recorded_ghost_script_uses_an_invalid_sprite_property(tmp_path):
    result = probe(tmp_path, "ghost-animation-property", """extends Node
var elapsed: float = 0.0
var injected: bool = false
func _physics_process(delta: float) -> void:
    elapsed += delta
    if elapsed > 1.0 and not injected:
        injected = true
        var scene = load("res://scenes/Ghost.tscn") as PackedScene
        var ghost = scene.instantiate()
        ghost.setup([{"position": Vector2(100, 600), "animation": "idle", "facing": 1}])
        get_tree().current_scene.add_child(ghost)
        print("RUN003_GHOST_EXERCISED")
    if elapsed > 1.1:
        get_tree().quit()
""")
    assert "RUN003_GHOST_EXERCISED" in result["stdout"]
    assert "animation" in result["stderr"] and "Sprite2D" in result["stderr"]
    assert "SCRIPT ERROR:" in result["stderr"]
