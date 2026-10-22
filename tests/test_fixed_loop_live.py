"""Explicitly opt in to paid, fresh end-to-end generation and real engine sessions."""

import asyncio
import json
import os
import runpy
from pathlib import Path

import pytest

from adaptive_game_generation.models import RoleCallFinished, SharedState
from adaptive_game_generation.runtime.commit import digest_path

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("GAMECRAFT_FIXED_LOOP_LIVE_TESTS") != "1",
                    reason="Set GAMECRAFT_FIXED_LOOP_LIVE_TESTS=1 to call real text/image models and Docker")
def test_real_fixed_loop_from_instruction_to_auditable_delivery(tmp_path):
    runner = runpy.run_path(str(ROOT / "examples/fixed_loop_demo.py"))
    output = Path(os.environ.get("GAMECRAFT_FIXED_LOOP_LIVE_OUTPUT", tmp_path / "fixed-loop-live"))
    summary = asyncio.run(runner["run_live"](output))
    print(json.dumps({"review": str(output / "review.md"), **summary}))
    state = SharedState.model_validate_json((output / "state_final.json").read_bytes())
    assert (output / "instruction.md").read_bytes() == (runner["DEFAULT_TASK"] / "instruction.md").read_bytes()
    finished = [e.payload for e in state.ledger.events if isinstance(e.payload, RoleCallFinished)]
    assert {f.result.role.value for f in finished} == {"designer", "art_producer", "developer", "player", "qa_reviewer"}
    assert summary["delivery_exported"]
    manifest = json.loads((output / "delivery/manifest.json").read_bytes())
    assert digest_path(output / "delivery/game") == manifest["sha256"]
    assert manifest["traces"], "Inspect preserved results: this run did not reach trace assembly"
    assert all(not f.result.request_id.startswith("import-") for f in finished)
    calls = list((output / "model_calls").glob("*/*/request.json"))
    assert len(calls) == state.ledger.usage.model_calls
    assert state.ledger.usage.token_usage_complete
    for path in calls:
        request = json.loads(path.read_bytes())
        inputs = json.loads(request["messages"][1]["content"])["input"]
        assert inputs["task"]["instruction"] == state.task.instruction
        assert "ledger" not in inputs and "controller" not in inputs
    developer_ref = max((r for r in state.artifacts if r.artifact_id == "developer-report"), key=lambda r: r.version)
    developer = json.loads((output / "workspace/public" / developer_ref.path).read_bytes())
    if any(asset["is_mock"] for asset in developer["integrated_assets"]):
        assert not summary["delivery_ready"], "Current mock assets cannot satisfy final-art acceptance"
