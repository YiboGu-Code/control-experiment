"""Real model integration test. No provider mocks or response fixtures are used."""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
from pathlib import Path

import pytest

from adaptive_game_generation.models import RoleResult, SharedState

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("GAMECRAFT_LIVE_TESTS") != "1", reason="Set GAMECRAFT_LIVE_TESTS=1 to call the configured real model")
def test_real_designer_input_output_and_committed_state(tmp_path):
    demo = runpy.run_path(str(ROOT / "examples/designer_demo.py"))
    output = Path(os.environ["GAMECRAFT_LIVE_OUTPUT"]) if os.environ.get("GAMECRAFT_LIVE_OUTPUT") else tmp_path / "review"
    summary = demo["run_demo"](output)
    print(json.dumps({"review": str(output.resolve() / "review.md"), **summary}, ensure_ascii=False))
    assert summary["mode"] == "live" and summary["status"] == "completed", summary
    assert all(summary["checks"].values())
    assert summary["usage"]["model_calls"] == 1 and summary["usage"]["tool_calls"] == 4
    assert (output / "tool_audit.jsonl").is_file()
    state = SharedState.model_validate_json((output / "state_after.json").read_text(encoding="utf-8"))
    result = RoleResult.model_validate_json((output / "role_result.json").read_text(encoding="utf-8"))
    assert state.ledger.events[1].payload.result == result
    source = json.loads((output / "source.json").read_text(encoding="utf-8"))
    assert (output / "instruction.md").read_bytes() == (ROOT / source["instruction_path"]).read_bytes()
    assert hashlib.sha256((output / "instruction.md").read_bytes()).hexdigest() == source["instruction_sha256"]
    body = json.loads((output / "api_request.json").read_text(encoding="utf-8"))
    prompt = json.loads(body["messages"][-1]["content"])
    assert prompt["input"]["task"]["instruction"] == state.task.instruction
    assert body["model"] == summary["model"]
    response = json.loads((output / "api_response.json").read_text(encoding="utf-8"))
    assert response["choices"][0]["message"]["content"].encode("utf-8") == (output / "model_response.txt").read_bytes()
    assert response["usage"]["prompt_tokens"] == state.ledger.usage.input_tokens
    assert response["usage"]["completion_tokens"] == state.ledger.usage.output_tokens
    assert summary["finish_reason"] == response["choices"][0]["finish_reason"] == "stop"
    assert summary["reasoning_tokens"] == (response["usage"].get("completion_tokens_details") or {}).get("reasoning_tokens")
    if body["stream"]:
        chunks = [json.loads(line) for line in (output / "response_chunks.jsonl").read_text(encoding="utf-8").splitlines()]
        text = "".join(choice["delta"].get("content") or "" for chunk in chunks for choice in chunk["choices"])
        assert text == response["choices"][0]["message"]["content"]
        assert next(chunk["usage"] for chunk in reversed(chunks) if chunk.get("usage")) == response["usage"]
    for path in output.iterdir():
        if path.is_file():
            assert not re.search(r"[\u3400-\u9fff]", path.read_text(encoding="utf-8")), path.name
    for ref in result.artifact_refs:
        assert hashlib.sha256((output / Path(ref.path).name).read_bytes()).hexdigest() == ref.sha256
