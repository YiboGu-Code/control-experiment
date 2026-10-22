"""Actual Art text model, original Benchmark task, mock image output only."""

import hashlib
import json
import os
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("GAMECRAFT_ART_LIVE_TESTS") != "1", reason="Set GAMECRAFT_ART_LIVE_TESTS=1 to call the real Art planner")
def test_real_art_planner_and_mock_image_outputs(tmp_path):
    demo = runpy.run_path(str(ROOT / "examples/art_producer_demo.py"))
    output = Path(os.environ["GAMECRAFT_ART_LIVE_OUTPUT"]) if os.environ.get("GAMECRAFT_ART_LIVE_OUTPUT") else tmp_path / "art-review"
    summary = demo["run_demo"](output)
    print(json.dumps({"review": str(output / "review.md"), **summary}))
    assert summary["status"] == "completed", summary
    assert all(summary["checks"].values())
    source = json.loads((output / "source.json").read_text(encoding="utf-8"))
    assert (output / "instruction.md").read_bytes() == (ROOT / source["instruction_path"]).read_bytes()
    assert hashlib.sha256((output / "designer_input.json").read_bytes()).hexdigest() == source["designer_input"]["sha256"]
    result = json.loads((output / "role_result.json").read_text(encoding="utf-8"))
    for ref in result["artifact_refs"]:
        path = output / "deliverables" / ref["path"].split("/", 3)[3]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == ref["sha256"]
    for directory in (output / "model_calls").iterdir():
        response = json.loads((directory / "response.json").read_text(encoding="utf-8"))
        assert (directory / "response.txt").read_bytes() == response["choices"][0]["message"]["content"].encode("utf-8")
        prompt = json.loads((directory / "prompt.json").read_text(encoding="utf-8"))
        assert prompt["input"]["task"]["instruction"] == (output / "instruction.md").read_text(encoding="utf-8")
        assert "controller" not in prompt["input"] and "ledger" not in prompt["input"]
