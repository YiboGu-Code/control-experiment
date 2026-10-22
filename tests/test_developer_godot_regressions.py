"""Opt-in, real local Godot checks in Docker; no model or image service calls."""

import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from adaptive_game_generation.roles.developer.diagnostics import ENGINE_FAILURE
from adaptive_game_generation.runtime import DockerSandbox
from adaptive_game_generation.runtime.workspace import InvocationWorkspace

pytestmark = pytest.mark.skipif(
    os.environ.get("GAMECRAFT_GODOT_REGRESSION_TESTS") != "1",
    reason="Set GAMECRAFT_GODOT_REGRESSION_TESTS=1 for offline, containerized Godot regressions",
)
PROJECT = '[application]\nrun/main_scene="res://Main.tscn"\n[rendering]\nrenderer/rendering_method="gl_compatibility"\n'
SCENE = '[gd_scene load_steps=2 format=3]\n[ext_resource type="Script" path="res://main.gd" id="1"]\n[node name="Root" type="Node"]\nscript=ExtResource("1")\n'


def run_case(tmp_path, name, files):
    destination = os.environ.get("GAMECRAFT_GODOT_REGRESSION_OUTPUT")
    root = (Path(destination).absolute() if destination else tmp_path) / name
    public, stage = root / "public", root / "output"
    public.mkdir(parents=True)  # Never overwrite previous evidence.
    (stage / "game").mkdir(parents=True)
    for path, content in files.items():
        target = stage / "game" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    workspace = InvocationWorkspace(public, stage, "artifacts/developer/diagnostic")
    sandbox = DockerSandbox(os.environ.get("GAMECRAFT_GODOT_REGRESSION_IMAGE", "gamecraft-godot:4.5.1"))
    async def check():
        steps = []
        for name, argv in (
            ("import", ("godot", "--headless", "--path", "/output/game", "--import")),
            ("launch", ("godot", "--headless", "--path", "/output/game", "--quit-after", "60")),
        ):
            result = await sandbox.run(workspace, argv, timeout=60)
            steps.append({"step": name, **asdict(result)})
        return steps
    steps = asyncio.run(check())
    (root / "logs.json").write_text(json.dumps(steps, indent=2) + "\n", encoding="utf-8")
    return steps[-1]


def test_resource_method_call_silently_loses_later_nodes(tmp_path):
    scene = SCENE + '[node name="Label" type="Label" parent="."]\nMETHOD\n[node name="Later" type="Control" parent="."]\n'
    script = 'extends Node\nfunc _ready() -> void:\n\tassert(has_node("Later"), "Missing later resource nodes")\n\tif has_node("Later"):\n\t\tprint("CHECK_SCENE_OK")\n'
    files = {"project.godot": PROJECT, "Main.tscn": scene.replace("METHOD", 'add_theme_font_size_override("font_size", 32)'), "main.gd": script}
    bad = run_case(tmp_path, "resource-before", files)
    assert "CHECK_SCENE_OK" not in bad["stdout"] and ENGINE_FAILURE.search(bad["stderr"])
    files["Main.tscn"] = scene.replace("METHOD", "theme_override_font_sizes/font_size = 32")
    good = run_case(tmp_path, "resource-after", files)
    assert good["returncode"] == 0 and "CHECK_SCENE_OK" in good["stdout"]
    assert not ENGINE_FAILURE.search(good["stdout"] + "\n" + good["stderr"])


def test_scene_transition_initialization_survives_in_a_persistent_autoload(tmp_path):
    target = SCENE.replace('res://main.gd', 'res://target.gd')
    files = {
        "project.godot": PROJECT, "Main.tscn": SCENE, "Target.tscn": target,
        "target.gd": 'extends Node\nfunc configure(id: String) -> void:\n\tprint("SCENARIO_READY:" + id)\n',
        "main.gd": 'extends Node\nfunc _ready() -> void:\n\tcall_deferred("_start")\nfunc _start() -> void:\n\tget_tree().change_scene_to_file("res://Target.tscn")\n\tawait get_tree().physics_frame\n\tget_tree().current_scene.configure("level24")\n',
    }
    bad = run_case(tmp_path, "lifetime-before", files)
    assert ENGINE_FAILURE.search(bad["stderr"]) and "SCENARIO_READY:" not in bad["stdout"]
    files["project.godot"] += '[autoload]\nRouter="*res://router.gd"\n'
    files["main.gd"] = 'extends Node\nfunc _ready() -> void:\n\tRouter.call_deferred("open", "res://Target.tscn", "level24")\n'
    files["router.gd"] = 'extends Node\nfunc open(path: String, id: String) -> void:\n\tvar tree: SceneTree = get_tree()\n\tvar error: Error = tree.change_scene_to_file(path)\n\tassert(error == OK)\n\tawait tree.scene_changed\n\ttree.current_scene.configure(id)\n'
    good = run_case(tmp_path, "lifetime-after", files)
    assert good["returncode"] == 0 and "SCENARIO_READY:level24" in good["stdout"]
    assert not ENGINE_FAILURE.search(good["stdout"] + "\n" + good["stderr"])


def test_input_map_requires_real_key_event_objects(tmp_path):
    script = '''extends Node
func _ready() -> void:
\tvar matches: int = 0
\tfor event in InputMap.action_get_events("move_right"):
\t\tif event is InputEventKey and event.physical_keycode == 68:
\t\t\tmatches += 1
\tassert(matches == 1, "Keyboard event is missing")
\tif matches == 1:
\t\tprint("KEY_EVENT_OK")
'''
    files = {"project.godot": PROJECT + '[input]\nmove_right={"deadzone":0.5,"events":[{"physical_keycode":68}]}\n',
             "Main.tscn": SCENE, "main.gd": script}
    bad = run_case(tmp_path, "input-before", files)
    assert "KEY_EVENT_OK" not in bad["stdout"]
    files["project.godot"] = PROJECT + '[input]\nmove_right={"deadzone":0.5,"events":[Object(InputEventKey,"physical_keycode":68)]}\n'
    good = run_case(tmp_path, "input-after", files)
    assert good["returncode"] == 0 and "KEY_EVENT_OK" in good["stdout"]
    assert not ENGINE_FAILURE.search(good["stdout"] + "\n" + good["stderr"])
