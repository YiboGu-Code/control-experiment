"""Small source checks and focused repair feedback; never execute generated code."""

from __future__ import annotations

import json
import re
from collections import Counter

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ENGINE_FAILURE = re.compile(
    r"(?mi)^\s*(?:SCRIPT ERROR:|ERROR:|Parse Error:|Shader compilation failed|"
    r"WARNING: Parent path .* has vanished when instantiating:)"
)
_LOCATION = re.compile(r"res://([^:\s\"'()]+)(?::(\d+))?")


def _statements(text):
    """Yield top-level resource statements, ignoring strings and nested values.

    This is deliberately not a replacement for the Godot parser. It recognizes
    just enough lexical structure to avoid flagging method names inside data.
    """
    depth, quoted, escaped = 0, False, False
    statement, start = [], 1
    for number, line in enumerate(text.splitlines(), 1):
        if not statement:
            start = number
        content = []
        for char in line:
            if not quoted and char in (";", "#"):
                break
            content.append(char)
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[({":
                depth += 1
            elif char in "])}":
                depth = max(0, depth - 1)
        statement.append("".join(content))
        if not quoted and depth == 0:
            yield start, "\n".join(statement).strip()
            statement = []


def source_findings(files):
    """Return certain serialization errors and advisory lifecycle hazards."""
    findings = []
    def add(path, line, code, message, severity="error"):
        findings.append(dict(path=path, line=line, code=code, severity=severity, message=message))

    for path, text in sorted(files.items()):
        if path.endswith((".tscn", ".tres")):
            for number, statement in _statements(text):
                if re.match(r"^[A-Za-z_]\w*\s*\(", statement):
                    add(path, number, "resource_method_call",
                        "A text resource contains properties, not executable method calls. "
                        "Use theme_override_font_sizes/font_size = N for font overrides, or move code into a .gd script. "
                        "This can stop loading later nodes even if import exits with code 0.")
                if statement.startswith(("[sub_resource ", "[ext_resource ")) and re.search(r"\bid\s*=\s*[A-Za-z_]\w*(?:\s|\])", statement):
                    add(path, number, "unquoted_resource_id",
                        "Quote string resource IDs in the section header and in SubResource/ExtResource references.")
        elif path == "project.godot":
            section = None
            for number, statement in _statements(text):
                if statement.startswith("["):
                    section = statement
                elif section == "[application]" and re.match(
                    r"config/(?:project_data_dir_name|use_hidden_project_data_directory)\s*=", statement
                ):
                    add(path, number, "engine_cache_configuration",
                        "Remove project-data cache location/visibility overrides and keep the default .godot directory. "
                        "The execution contract excludes .godot from authored sources and published artifacts. "
                        "A visible cache would be mistaken for source changes. Host sandbox permissions, not game "
                        "configuration, control whether imports can write.")
                elif section == "[input]" and "=" in statement:
                    try:
                        action = json.loads(statement.split("=", 1)[1])
                    except ValueError:
                        continue  # Godot Object(...) syntax is not JSON; the engine checks it.
                    events = action.get("events") if isinstance(action, dict) else None
                    if isinstance(events, list) and any(isinstance(e, dict) for e in events):
                        add(path, number, "input_event_dictionary",
                            "Input action events must be InputEvent objects, not dictionaries of physical_keycode. "
                            "Serialize Object(InputEventKey, ...) or register real InputEventKey instances through InputMap in code.")
        elif path.endswith(".gd"):
            changed_scene = False
            for number, line in enumerate(text.splitlines(), 1):
                code = line.strip()
                if code.startswith(("func ", "static func ")):
                    changed_scene = False
                if not code or code.startswith("#"):
                    continue
                if changed_scene and "get_tree()" in code:
                    add(path, number, "scene_lifetime",
                        "Potential outgoing-scene lifetime bug: change_scene_to_file/packed removes the current scene immediately. "
                        "If this script belongs to that scene, get_tree() is now null. "
                        "Move the transition and await tree.scene_changed into a persistent autoload, or pass pending state "
                        "to the destination before switching and initialize it in _ready. call_deferred or waiting a frame "
                        "on the outgoing node does not keep it alive.", "advisory")
                    changed_scene = False
                if re.search(r"\.change_scene_to_(?:file|packed)\s*\(", code):
                    changed_scene = True
    return findings[:64]


def _diagnostics(attempt):
    diagnostics = []
    for step in attempt.get("steps", []):
        text = _ANSI.sub("", step.get("stdout", "") + "\n" + step.get("stderr", ""))
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if ENGINE_FAILURE.match(line):
                location_text = "\n".join(lines[index:index + 4])
                locations = [{"path": m[1], "line": int(m[2]) if m[2] else None}
                             for m in _LOCATION.finditer(location_text)]
                diagnostics.append({"step": step["name"], "message": line.strip()[:600], "locations": locations[:4]})
        if step.get("status") != "passed" and step.get("reason"):
            diagnostics.append({"step": step["name"], "message": step["reason"][:600], "locations": []})
    return diagnostics


def repair_brief(files, attempts, previous_report=None):
    history = [*(previous_report or {}).get("build_attempts", []),
               *[a.model_dump(mode="json") for a in attempts]]
    counts = Counter(key for attempt in history
                     for key in {(d["step"], d["message"]) for d in _diagnostics(attempt)})
    latest = _diagnostics(history[-1]) if history else []
    unique = {(d["step"], d["message"]): d for d in latest}
    diagnostics = [{**d, "attempts_with_same_error": counts[key]} for key, d in unique.items()][:24]
    findings = source_findings(files)
    locations = [(d["path"], d["line"]) for d in findings]
    locations += [(p["path"], p["line"]) for d in diagnostics for p in d["locations"]]
    excerpts = {}
    for path, line in locations:
        if path not in files or path in excerpts or len(excerpts) >= 12:
            continue
        lines = files[path].splitlines()
        center = max(0, (line or 1) - 1)
        excerpts[path] = [{"line": i + 1, "text": lines[i][:300]}
                          for i in range(max(0, center - 3), min(len(lines), center + 5))]
    return {
        "latest_build_status": history[-1]["status"] if history else None,
        "diagnostics": diagnostics, "source_findings": findings, "source_excerpts": excerpts,
        "interpretation": "Host observations and narrow static checks, not proof of complete gameplay. "
            "Repeated errors mean earlier changes did not fix them. Match each error to actual changed source; "
            "inspect preceding resource statements when later nodes vanish. Keep all required mechanics.",
    }
