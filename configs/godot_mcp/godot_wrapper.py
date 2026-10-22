#!/usr/bin/python3
"""Fixed preview options; no arbitrary command line is accepted from a model."""
import os
import sys

args = sys.argv[1:]
if not any(arg in args for arg in ("--headless", "--version", "--import", "--script", "--help")):
    args += ["--rendering-method", "gl_compatibility", "--fixed-fps", "30", "--resolution", "1280x720"]
    scenario = os.environ.get("GAMECRAFT_PREVIEW_SCENARIO")
    if scenario:
        args += ["--", "--scenario", scenario]
os.execv("/usr/local/bin/godot", ["godot", *args])
