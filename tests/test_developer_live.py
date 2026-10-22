"""Actual text model and isolated Godot, using the original task and mock Art."""

import hashlib
import json
import os
import runpy
from pathlib import Path

import pytest

from adaptive_game_generation.runtime.commit import digest_path

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("GAMECRAFT_DEVELOPER_LIVE_TESTS") != "1", reason="Set GAMECRAFT_DEVELOPER_LIVE_TESTS=1 for the real Developer model and Godot")
def test_real_developer_model_and_isolated_engine(tmp_path):
    demo = runpy.run_path(str(ROOT / "examples/developer_demo.py"))
    output = Path(os.environ["GAMECRAFT_DEVELOPER_LIVE_OUTPUT"]) if os.environ.get("GAMECRAFT_DEVELOPER_LIVE_OUTPUT") else tmp_path / "developer-review"
    summary = demo["run_demo"](output)
    print(json.dumps({"review": str(output / "review.md"), **summary}))
    assert summary["status"] == "completed", summary
    assert all(summary["checks"].values())
    result = json.loads((output / "role_result.json").read_text(encoding="utf-8"))
    for ref in result["artifact_refs"]:
        path = output / "deliverables" / ref["path"].split("/", 3)[3]
        assert digest_path(path) == ref["sha256"]
    for call in (output / "model_calls").iterdir():
        response = json.loads((call / "response.json").read_text(encoding="utf-8"))
        assert response["choices"][0]["message"]["content"].encode("utf-8") == (call / "response.txt").read_bytes()
        prompt = json.loads((call / "prompt.json").read_text(encoding="utf-8"))
        assert prompt["input"]["task"]["instruction"].encode("utf-8") == (output / "instruction.md").read_bytes()
        assert "controller" not in prompt["input"] and "ledger" not in prompt["input"]
    report = json.loads((output / "deliverables/developer_report.json").read_text(encoding="utf-8"))
    before = json.loads((output / "state_before.json").read_text(encoding="utf-8"))
    art = {a["asset_id"]: a for a in before["assets"]}
    for item in report["integrated_assets"]:
        assert hashlib.sha256((output / "deliverables/game" / item["target_path"]).read_bytes()).hexdigest() == art[item["asset_id"]]["artifact_ref"]["sha256"]
    assert all(step["status"] == "passed" for step in report["build_attempts"][-1]["steps"])
    assert any(step["name"].startswith("scenario:") for step in report["build_attempts"][-1]["steps"])
