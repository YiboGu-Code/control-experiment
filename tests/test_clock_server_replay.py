"""Recheck user-recorded engine evidence; never call models or start Godot/Docker."""
import json
from pathlib import Path
import shutil

import pytest

from test_replay_clock_repair import CODE, ROOT, session_files

RUN = ROOT / "runs/fixed-loop-developer-opt-001"
SERVER = RUN / "diagnosis/clock-server-replay"


def test_server_replay_proves_clock_fix_for_all_recorded_traces():
    result = CODE["audit_saved_replay"](SERVER, RUN)
    assert result["status"] == "passed" and result["real_replay_verified"] is True
    assert result["original_project_verified_locally"] is True
    assert result["changed_files"] == ["scripts/gameplay_final.gd"]
    assert (result["trace_count"], result["session_count"], result["total_recorded_frames"]) == (5, 30, 6300)
    assert result["after_compared_frame_pairs"] == 2100 and result["verified_checkpoint_images"] == 132
    assert sum(c["status"] == "passed" for c in result["comparisons"]["before"]) == 2
    assert len(result["comparisons"]["after"]) == 10
    assert all(c["state_match"] and c["pixel_match"] for c in result["comparisons"]["after"])
    assert all(t["cross_variant_state_match"] for t in result["trace_checks"])
    assert all(c["different_pixels"] == 0 for t in result["trace_checks"]
               for c in t["checkpoint_pixel_checks"] if c["variant"] == "after")


@pytest.mark.parametrize("fault, message", [
    ("missing_replay", "coverage is incomplete"),
    ("altered_input", "Session input changed"),
    ("forged_summary", "summary does not match"),
    ("altered_project", "project differs"),
])
def test_audit_does_not_trust_success_flag_or_incomplete_evidence(tmp_path, fault, message):
    target = tmp_path / "replay-copy"
    ignore = ["analysis"]
    if fault == "missing_replay":
        ignore.append("four_loop_completion-replay-2")
    shutil.copytree(SERVER, target, ignore=shutil.ignore_patterns(*ignore))
    if fault == "altered_input":
        path = target / "before/sessions/four_loop_completion-replay-1/request.json"
        data = json.loads(path.read_bytes())
        data["seed"] += 1
        CODE["save"](path, data)
    elif fault == "forged_summary":
        path = target / "summary.json"
        data = json.loads(path.read_bytes())
        data["comparisons"]["before"][0]["status"] = "passed"
        CODE["save"](path, data)
    elif fault == "altered_project":
        path = target / "after/public/game/scripts/gameplay_final.gd"
        path.write_bytes(path.read_bytes() + b"\n# A source edit after replay must invalidate the evidence.\n")
    with pytest.raises(ValueError, match=message):
        CODE["audit_saved_replay"](target, RUN)


def test_isolation_flags_require_booleans_not_truthy_text(tmp_path):
    job = session_files(tmp_path)
    path = tmp_path / "result/driver.json"
    data = json.loads(path.read_bytes())
    data["fresh_process"] = "false"
    CODE["save"](path, data)
    with pytest.raises(ValueError, match="isolation"):
        CODE["read_session"](tmp_path, job, "probe", "capture")


def test_every_frame_requires_real_digest_even_without_screenshot(tmp_path):
    job = session_files(tmp_path)
    path = tmp_path / "result/frames.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[1]["pixels_sha256"] = "missing"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    with pytest.raises(ValueError, match="per-frame digest"):
        CODE["read_session"](tmp_path, job, "probe", "capture")
