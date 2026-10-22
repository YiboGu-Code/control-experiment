"""Opt-in local MCP/Godot integration; all model responses are explicit fixtures."""
import asyncio
import json
import os
from pathlib import Path
import runpy

import pytest

from adaptive_game_generation.runtime.commit import digest_path

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.live
@pytest.mark.skipif(os.getenv("GAMECRAFT_MCP_ENGINE_TESTS") != "1",
                    reason="Opt in to local Docker/MCP execution; no online models are called")
def test_real_mcp_inspect_patch_inspect_finish(tmp_path):
    demo = runpy.run_path(str(ROOT / "examples/developer_mcp_demo.py"))
    output = Path(os.environ.get("GAMECRAFT_MCP_ENGINE_OUTPUT", str(tmp_path / "mcp-demo"))).absolute()
    summary = asyncio.run(demo["run"](output, offline_fixture=True))
    assert summary["online_models_called"] is False
    assert summary["delivery_exported"] and summary["final_build_status"] == "passed"
    assert not summary["delivery_ready"] and summary["usage"]["model_calls"] == 5
    role = json.loads((output / "role_output.json").read_text(encoding="utf-8"))
    public = output / "workspace/public"
    report_ref = next(r for r in role["artifact_refs"] if r["artifact_id"] == "developer-report")
    report = json.loads((public / report_ref["path"]).read_text(encoding="utf-8"))
    assert report["self_review_completed"] and len(report["mcp_inspections"]) == 2
    before, after = report["mcp_inspections"]
    assert before["project_sha256"] != after["project_sha256"]
    assert after["project_sha256"] == report["project_ref"]["sha256"] == digest_path(output / "delivery/game")
    assert before["screenshot_ref"]["sha256"] != after["screenshot_ref"]["sha256"]
    for record in (before, after):
        assert record["result"]["mcp_tool_calls"] == 7
        assert record["result"]["status"] == "passed"
        assert record["result"]["original_project_unchanged"]
        assert not record["result"]["acceptance_evidence"] and not record["result"]["deterministic_replay"]
        for ref in record["artifact_refs"]:
            assert digest_path(public / ref["path"]) == ref["sha256"]
        transcript_ref = next(r for r in record["artifact_refs"] if r["path"].endswith("/transcript.jsonl"))
        transcript = [json.loads(row) for row in (public / transcript_ref["path"]).read_text(encoding="utf-8").splitlines()]
        calls = [row["message"] for row in transcript if row["direction"] == "client"
                 and row["message"].get("method") == "tools/call"]
        assert len(calls) == 7 and {x["params"]["name"] for x in calls} == set(
            ("get_scene_tree", "run_project", "simulate_input", "take_screenshot",
             "get_ui_elements", "get_debug_output", "stop_project"))
    original = demo["RUNNER"]["DEFAULT_TASK"] / "instruction.md"
    assert (output / "instruction.md").read_bytes() == original.read_bytes()
    repaired_input = json.loads((output / "model_calls/developer/002/request.json").read_text(encoding="utf-8"))
    assert "Scripted visual feedback" in json.dumps(repaired_input)
    assert "Vector2(1, 1)" in (output / "delivery/game/main.gd").read_text(encoding="utf-8")
