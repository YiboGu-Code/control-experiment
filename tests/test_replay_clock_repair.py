"""Offline checks for the real-engine replay command; fixtures are never live evidence."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil

from PIL import Image
import pytest

ROOT = Path(__file__).resolve().parents[1]
CODE = runpy.run_path(str(ROOT / "examples/replay_clock_repair.py"))


def test_prepare_uses_registered_real_game_and_traces_and_changes_only_clock_expression(tmp_path):
    run = ROOT / "runs/fixed-loop-developer-opt-001"
    output = tmp_path / "repair"
    repair = CODE["prepare"](run, output, ["near_rewind_reset"])
    before = output / "before/public/game"
    after = output / "after/public/game"
    changed = [p.relative_to(before).as_posix() for p in before.rglob("*") if p.is_file()
               and p.read_bytes() != (after / p.relative_to(before)).read_bytes()]
    assert changed == [CODE["SCRIPT"]]
    assert (after / CODE["SCRIPT"]).read_bytes() == (before / CODE["SCRIPT"]).read_bytes().replace(
        CODE["OLD"].encode(), CODE["NEW"].encode())
    assert len(repair["requests"]) == 1
    assert any(f["code"] == "nondeterministic_render_clock" for f in repair["before_static_findings"])
    assert not any(f["code"] == "nondeterministic_render_clock" for f in repair["after_static_findings"])
    with pytest.raises(ValueError, match="new output"):
        CODE["prepare"](run, output)


def test_missing_docker_cannot_be_promoted_to_success_or_host_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(CODE["shutil"], "which", lambda _: None)
    result = asyncio.run(CODE["execute"](tmp_path, {"requests": []}, "offline-image", 5))
    assert result["status"] == "runtime_unavailable" and result["real_replay_verified"] is False
    assert result["sessions"] == [] and not (tmp_path / "before").exists()


def session_files(tmp_path):
    save = CODE["save"]
    result = tmp_path / "result"
    result.mkdir()
    trace = {"duration_frames": 2, "events": []}
    job = {"project_sha256": "project-digest", "trace": trace, "checkpoints": [0]}
    save(result / "driver.json", {"status": "passed", "project_sha256": "project-digest", "network": "none",
        "fresh_process": True, "fresh_user_data": True, "authored_files_unchanged": True, "original_project_unchanged": True})
    save(result / "receipt.json", {"completed_frames": 2, "event_count": 0, "width": 1280, "height": 720, "fps": 30})
    save(result / "executed_trace.json", trace)
    image = Image.new("RGBA", (1280, 720), "black")
    image.save(result / "frame_0000.png")
    (result / "frame_0000.json").write_bytes(b'{}')
    row = {"state_sha256": hashlib.sha256(b'{}').hexdigest(), "pixels_sha256": hashlib.sha256(image.tobytes()).hexdigest()}
    (result / "frames.jsonl").write_text("\n".join(json.dumps({"frame": f, **row}) for f in range(2)), encoding="utf-8")
    (result / "events.jsonl").write_text("", encoding="utf-8")
    return job


def test_replay_reader_checks_actual_screenshot_and_snapshot_bytes(tmp_path):
    job = session_files(tmp_path)
    record, trace = CODE["read_session"](tmp_path, job, "probe", "capture")
    assert record.status == "passed" and len(record.pixel_digests) == trace.duration_frames == 2
    (tmp_path / "result/frame_0000.json").write_bytes(b'{"changed":true}')
    with pytest.raises(ValueError, match="Scene snapshot"):
        CODE["read_session"](tmp_path, job, "probe", "capture")


def test_reader_reproduces_saved_real_replay_failures(tmp_path):
    """Read prior real engine evidence, without pretending to rerun the engine."""
    run = ROOT / "runs/fixed-loop-developer-opt-001"
    state = json.loads((run / "state_final.json").read_bytes())
    ref = max((r for r in state["artifacts"] if r["artifact_id"] == "player-report"), key=lambda r: r["version"])
    report_path = run / "workspace/public" / ref["path"]
    assert CODE["digest_path"](report_path) == ref["sha256"]
    report = json.loads(report_path.read_bytes())
    comparisons = []
    for path in sorted((report_path.parent / "requests").glob("*-capture.json")):
        trace_id = path.name.removesuffix("-capture.json")
        for mode in ("capture", "replay-1", "replay-2"):
            identity = trace_id + "-" + mode
            target = tmp_path / identity
            shutil.copytree(report_path.parent / "sessions" / identity, target / "result")
            job = json.loads((report_path.parent / "requests" / (identity + ".json")).read_bytes())
            session, _ = CODE["read_session"](target, job, trace_id, identity)
            if mode == "capture":
                capture = session
            else:
                comparisons.append(CODE["compare_sessions"](capture, session).model_dump(mode="json"))
    expected = {c["replay_session_id"]: c for c in report["comparisons"]}
    assert {c["replay_session_id"]: c for c in comparisons} == expected
    assert len(comparisons) == 10 and sum(c["status"] == "passed" for c in comparisons) == 2
    assert all(c["state_match"] for c in comparisons)
    if destination := os.environ.get("CLOCK_REVIEW_ROOT"):
        CODE["save"](Path(destination) / "recorded_replay_recheck.json", {
            "mode": "Offline reading of saved real engine evidence; the repaired game has NOT been replayed",
            "source_report": str(report_path), "source_report_sha256": ref["sha256"],
            "real_replay_verified": False, "comparisons": comparisons})
