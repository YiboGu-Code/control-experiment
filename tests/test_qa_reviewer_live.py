"""Opt-in paid QA acceptance integration; recorded engine evidence is real, images are mock."""

import json
import os
import runpy
from pathlib import Path

import pytest

from adaptive_game_generation.runtime.commit import digest_path

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("GAMECRAFT_QA_LIVE_TESTS") != "1", reason="Opt in to the real QA model")
def test_real_qa_model_reviews_real_benchmark_evidence(tmp_path):
    demo = runpy.run_path(str(ROOT / "examples/qa_demo.py"))
    output = Path(os.environ.get("GAMECRAFT_QA_LIVE_OUTPUT", str(tmp_path / "qa-review")))
    summary = demo["run_demo"](output)
    print(json.dumps({"review": str(output / "review.md"), **summary}))
    assert summary["status"] == "completed", summary
    assert all(summary["checks"].values())
    assert summary["criteria"] == 116 and summary["issues"] > 0 and not summary["ready"]
    role_result = json.loads((output / "role_result.json").read_bytes())
    for ref in role_result["artifact_refs"]:
        assert ref["kind"] == "report"
        assert digest_path(output / "deliverables" / ref["path"].split("/", 3)[3]) == ref["sha256"]
    for directory in (output / "model_calls").iterdir():
        response = json.loads((directory / "response.json").read_bytes())
        assert response["choices"][0]["message"]["content"].encode() == (directory / "response.txt").read_bytes()
        prompt = json.loads((directory / "prompt.json").read_bytes())
        assert prompt["input"]["task"]["instruction"].encode() == (output / "instruction.md").read_bytes()
        assert "controller" not in prompt["input"] and "ledger" not in prompt["input"]
