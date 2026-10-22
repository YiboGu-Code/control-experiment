"""Paid, opt-in end-to-end Player test: real model, real Godot, only art is mock."""

import json
import os
import runpy
from pathlib import Path

import pytest

from adaptive_game_generation.roles.player.contracts import InputTrace
from adaptive_game_generation.runtime.commit import digest_path

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("GAMECRAFT_PLAYER_LIVE_TESTS") != "1", reason="Opt in to real Player model and rendering")
def test_real_player_model_play_and_replay(tmp_path):
    demo = runpy.run_path(str(ROOT / "examples/player_demo.py"))
    output = Path(os.environ.get("GAMECRAFT_PLAYER_LIVE_OUTPUT", str(tmp_path / "player-review")))
    summary = demo["run_demo"](output)
    print(json.dumps({"review": str(output / "review.md"), **summary}))
    assert summary["status"] == "completed", summary
    assert all(summary["checks"].values())
    result = json.loads((output / "role_result.json").read_bytes())
    for ref in result["artifact_refs"]:
        path = output / "deliverables" / ref["path"].split("/", 3)[3]
        assert digest_path(path) == ref["sha256"]
        if ref["kind"] == "input_trace":
            InputTrace.model_validate_json(path.read_bytes())
    for directory in (output / "model_calls").iterdir():
        response = json.loads((directory / "response.json").read_bytes())
        assert response["choices"][0]["message"]["content"].encode() == (directory / "response.txt").read_bytes()
        prompt = json.loads((directory / "prompt.json").read_bytes())
        assert prompt["input"]["task"]["instruction"].encode() == (output / "instruction.md").read_bytes()
        assert "controller" not in prompt["input"] and "ledger" not in prompt["input"]
    report = json.loads((output / "deliverables/player_report.json").read_bytes())
    assert any(s["scenario"] is None and s["mode"] == "capture" for s in report["sessions"])
    assert any(s["scenario"] is not None and s["mode"] == "capture" for s in report["sessions"])
    for comparison in report["comparisons"]:
        passed = comparison["state_match"] and comparison["pixel_match"]
        assert comparison["status"] == ("passed" if passed else "failed")
