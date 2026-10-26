"""Offline protocol and failure tests. Synthetic adapters are never used by the live demo."""

import asyncio
import hashlib
import io
import json
import runpy
from pathlib import Path

from PIL import Image
import pytest
from pydantic import ValidationError

from adaptive_game_generation.models import ResourceLimits
from adaptive_game_generation.providers import GatewayConfig
from adaptive_game_generation.roles.player import Player, PlayerConfig, PlayerReport
from adaptive_game_generation.roles.player.contracts import InputTrace, TraceEvent, normalize_trace
from adaptive_game_generation.roles.player.role import compare_sessions
from adaptive_game_generation.runtime import Executor, ProcessResult, RuntimeConfig, Services, TextReply, Workspace
from adaptive_game_generation.runtime.commit import digest_path
from adaptive_game_generation.runtime.errors import SandboxCleanupFailed, SandboxUnavailable

ROOT = Path(__file__).resolve().parents[1]
DEMO = runpy.run_path(str(ROOT / "examples/player_demo.py"))


class OfflineText:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def input_token_upper_bound(self, prompt):
        return len(prompt.encode()) + 64

    async def generate(self, prompt, *, max_output_tokens, timeout):
        self.prompts.append(json.loads(prompt))
        raw = self.replies.pop(0)
        return TextReply(raw if isinstance(raw, str) else json.dumps(raw), 1000, 1000)


class OfflineRenderer:
    """Small synthetic renderer fixture to test trust boundaries, not gameplay."""
    def __init__(self, fault=None):
        self.fault, self.commands = fault, []

    async def run(self, workspace, argv, timeout):
        self.commands.append(argv)
        if self.fault == "unavailable":
            raise SandboxUnavailable("Offline unavailable fixture")
        if self.fault == "cleanup":
            raise SandboxCleanupFailed("Offline cleanup fixture")
        if self.fault == "timeout":
            raise asyncio.TimeoutError()
        job_path = workspace.output_path(argv[argv.index("--request") + 1].removeprefix("/output/"))
        job = json.loads(job_path.read_bytes())
        output = workspace.output_path(argv[argv.index("--output") + 1].removeprefix("/output/"))
        output.mkdir(parents=True)
        replay = "replay" in output.name
        picture = Image.new("RGBA", (1280, 720), (1 if replay and self.fault == "diverge" else 0, 20, 30, 255))
        binary = io.BytesIO()
        picture.save(binary, format="PNG")
        snap = json.dumps({"nodes": [{"path": "Synthetic", "class": "Label", "scripted": False,
            "properties": {"text": "OFFLINE FIXTURE", "position": [0, 0]}}], "truncations": 0}).encode()
        sha = hashlib.sha256
        frames = [{"frame": f, "state_sha256": sha(snap).hexdigest(), "pixels_sha256": sha(picture.tobytes()).hexdigest()}
                  for f in range(job["trace"]["duration_frames"])]
        for frame in job["checkpoints"]:
            (output / f"frame_{frame:04}.json").write_bytes(snap)
            (output / f"frame_{frame:04}.png").write_bytes(binary.getvalue())
        if self.fault == "pixels":
            frames[0]["pixels_sha256"] = "f" * 64
        if self.fault == "missing_frame":
            frames.pop()
        events = job["trace"]["events"]
        if self.fault == "events":
            events = []
        write = lambda name, data: (output / name).write_text(json.dumps(data), encoding="utf-8")
        write("driver.json", {"status": "passed", "project_sha256": job["project_sha256"],
            "authored_files_unchanged": True, "original_project_unchanged": True})
        write("receipt.json", {"harness_version": "godot-player.v1", "width": 1280, "height": 720, "fps": 30,
            "completed_frames": job["trace"]["duration_frames"], "event_count": len(events)})
        write("executed_trace.json", job["trace"])
        (output / "frames.jsonl").write_text("\n".join(json.dumps(f) for f in frames), encoding="utf-8")
        (output / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
        if self.fault == "extra":
            (output / "unexpected.gd").write_text("Unregistered extra file")
        if self.fault == "tamper_previous" and replay:
            job_path.parent.joinpath("probe.json").write_text("tampered")
        return ProcessResult(0, "Offline fixture", "")


def plan(**changes):
    return {"action": "play", "summary": "Exercise real dispatch in the integration layer", "trials": [{
        "trace_id": "movement", "purpose": "Observe movement", "requirement_ids": ["R002"],
        "trace": {"scenario": "near_rewind", "duration_frames": 5,
                  "events": [{"frame": 1, "type": "key_press", "keycode": "D"}]}}], **changes}


FINISH = {"action": "finish", "summary": "Summarize observed sessions with bounded coverage"}


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_post_capture_relevance_mapping_does_not_accept_requirements(tmp_path, controller):
    mapping = {"session_id": "movement-capture", "frame": 0, "requirement_ids": ["R001"],
               "rationale": "The recorded checkpoint exposes the visible scene; relevance annotation only."}
    finish = {**FINISH, "evidence_mappings": [mapping]}
    executor, text, backend = make_run(tmp_path, plan(), finish, controller=controller)
    result = invoke(executor)
    assert result.status == "completed", result.error
    evidence = [e for e in result.proposed_updates.evidence if e.kind in ("playthrough", "replay")]
    assert all(set(e.requirement_ids) == {"R001", "R002"} for e in evidence)
    assert len(backend.commands) == 4  # No extra play is invented for a mapping.
    assert not result.proposed_updates.requirements and executor.state.readiness is None
    assert report(executor).decisions[-1].evidence_mappings[0].rationale == mapping["rationale"]
    import os
    if root := os.environ.get("PIPELINE_REVIEW_ROOT"):
        folder = Path(root) / ("player-mapping-" + controller)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "model_io.json").write_text(json.dumps(list(zip(text.prompts, [plan(), finish])), indent=2), encoding="utf-8")
        (folder / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")


@pytest.mark.parametrize("change", [dict(session_id="probe"), dict(session_id="movement-replay-1"),
                                    dict(frame=2), dict(requirement_ids=["invented"])])
def test_invalid_post_capture_mapping_is_rejected_without_extra_execution(tmp_path, change):
    mapping = {"session_id": "movement-capture", "frame": 0, "requirement_ids": ["R001"],
               "rationale": "Offline invalid mapping fixture", **change}
    executor, _, backend = make_run(tmp_path, plan(), {**FINISH, "evidence_mappings": [mapping]}, FINISH)
    assert invoke(executor).status == "completed"
    assert len(backend.commands) == 4 and len(report(executor).protocol_errors) == 1
    assert all(e.requirement_ids == ("R002",) for e in executor.state.evidence
               if e.evidence_id.startswith("player-") and e.kind in ("playthrough", "replay"))


def test_missing_finding_citation_identifies_exact_item_and_repairs_without_fake_play(tmp_path):
    finding = {"category": "gameplay_concern", "description": "The offline probe contains diagnostic text.",
               "uncertainty": "This is a protocol fixture, not a gameplay claim."}
    rejected = plan(findings=[finding])
    repaired = plan(findings=[{**finding, "session_ids": ["probe"]}])
    executor, text, _ = make_run(tmp_path, rejected, repaired, FINISH)
    result = invoke(executor, PlayerConfig(prompt_version="player.v2"))
    assert result.status == "completed", result.error
    document = report(executor)
    assert len(document.sessions) == 4  # Rejected trials never ran.
    assert len(document.protocol_errors) == 1 and len(text.prompts) == 3
    feedback = text.prompts[1]["protocol_feedback"][0]["error"]
    assert "Finding 0 (gameplay_concern)" in feedback and "empty session_ids" in feedback
    assert "No trial" in feedback and "numeric limits.trials_left" in text.prompts[0]["instructions"]


def make_run(tmp_path, *replies, fault=None, controller="fixed_loop", seed=True, budget=None):
    recorded = DEMO["load_recorded_inputs"]()
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_bytes(recorded[0][2])
    (task_dir / "tests").mkdir()
    (task_dir / "tests/rubric.json").write_text("HIDDEN-EVALUATOR-SENTINEL")
    (task_dir / ".env").write_text("SECRET-ENV-SENTINEL")
    workspace, task = Workspace.prepare(tmp_path / "run", task_dir, task_id=recorded[0][0]["task_id"])
    text, backend = OfflineText(replies), OfflineRenderer(fault)
    limits = budget or ResourceLimits(max_model_calls=10, max_input_tokens=3000000, max_output_tokens=100000,
        max_image_calls=0, max_tool_calls=2000)
    executor = Executor.create(workspace, task, run_id="player-test", controller=controller, budget=limits,
        config=RuntimeConfig(role_limits=limits), services=Services(text=text, sandbox=backend))
    if seed:
        asyncio.run(DEMO["seed_recorded_inputs"](executor, recorded))
    return executor, text, backend


def invoke(executor, config=None, **kwargs):
    request = executor.request("player", "Play and replay the public Time Loop game", ("Capture real input and compare fresh runs",), **kwargs)
    return asyncio.run(executor.execute(request, Player(config)))


def report(executor):
    ref = max((r for r in executor.state.artifacts if r.artifact_id == "player-report"), key=lambda r: r.version)
    return PlayerReport.model_validate_json(executor.workspace.read_public(ref.path).read_bytes())


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_shared_executor_contract_and_unchanged_upstream(tmp_path, controller):
    executor, text, backend = make_run(tmp_path, plan(), FINISH, controller=controller)
    before = executor.state
    result = invoke(executor, request_id="initial")
    assert result.status == "completed", result.error
    doc = report(executor)
    assert len(doc.sessions) == 4 and len(doc.comparisons) == 2
    assert all(c.status == "passed" for c in doc.comparisons)
    assert executor.state.requirements == before.requirements and executor.state.issues == before.issues
    assert executor.state.game_revisions == before.game_revisions and executor.state.assets == before.assets
    assert executor.state.readiness is None
    assert executor.state.ledger.usage.image_calls == 0 and executor.state.ledger.usage.model_calls == 2
    trace = InputTrace.model_validate_json(executor.workspace.read_public(doc.sessions[1].trace_ref.path).read_bytes())
    assert [e.type for e in trace.events] == ["key_down", "key_up"]
    for session in doc.sessions[2:]:
        job = next(r for r in result.artifact_refs if r.artifact_id == "player-job-" + session.session_id)
        assert json.loads(executor.workspace.read_public(job.path).read_bytes())["trace"] == trace.wire()
    assert all("--script" not in command for command in backend.commands)  # Only the fixed trusted driver is dispatched.
    for prompt in text.prompts:
        assert prompt["input"]["task"]["instruction"] == before.task.instruction
        assert "controller" not in prompt["input"] and "ledger" not in prompt["input"]
        assert "HIDDEN-EVALUATOR" not in json.dumps(prompt) and "SECRET-ENV" not in json.dumps(prompt)
        assert not any(s["screenshots_visually_inspected_by_model"] for s in prompt["runtime_observations"])
    assert Executor.open(executor.workspace, services=executor.services).state == executor.state


def test_controller_does_not_change_worker_prompt(tmp_path):
    prompts = []
    for controller in ("fixed_loop", "adaptive_scheduler"):
        executor, text, _ = make_run(tmp_path / controller, FINISH, controller=controller)
        invoke(executor, request_id="same")
        prompts.append(text.prompts[0])
    assert prompts[0] == prompts[1]


@pytest.mark.parametrize("event", [
    {"frame": True, "type": "wait"}, {"frame": 600, "type": "wait"},
    {"frame": 0, "type": "key_press", "keycode": "escape"},
    {"frame": 0, "type": "wait", "keycode": "D"},
    {"frame": 0, "type": "mouse_click", "button": "left", "x": 1280, "y": 0},
    {"frame": 0, "type": "mouse_move", "button": "left", "x": 10, "y": 0},
    {"frame": 0, "type": "key_down", "keycode": "D", "x": 1},
])
def test_event_protocol_rejects_ambiguous_inputs(event):
    with pytest.raises(ValidationError):
        TraceEvent.model_validate(event)


def test_normalization_explicit_taps_and_held_release():
    trace = InputTrace.model_validate({"duration_frames": 10, "events": [
        {"frame": 1, "type": "key_press", "keycode": "SPACE"},
        {"frame": 3, "type": "mouse_down", "button": "left", "x": 4, "y": 5},
        {"frame": 5, "type": "mouse_move", "x": 20, "y": 21},
        {"frame": 6, "type": "key_down", "keycode": "D"}]})
    canonical = normalize_trace(trace)
    assert normalize_trace(canonical) == canonical
    assert [(e.frame, e.type) for e in canonical.events] == [(1, "key_down"), (2, "key_up"), (3, "mouse_down"),
        (5, "mouse_move"), (6, "key_down"), (9, "mouse_up"), (9, "key_up")]
    assert canonical.events[-2].x == 20


@pytest.mark.parametrize("events", [
    [{"frame": 0, "type": "key_up", "keycode": "D"}],
    [{"frame": 4, "type": "key_press", "keycode": "D"}],
    [{"frame": 4, "type": "key_down", "keycode": "D"}],
    [{"frame": 0, "type": "key_down", "keycode": "D"}, {"frame": 1, "type": "key_down", "keycode": "D"}],
])
def test_normalization_rejects_invalid_key_lifetimes(events):
    with pytest.raises(ValueError):
        normalize_trace(InputTrace.model_validate({"duration_frames": 5, "events": events}))


@pytest.mark.parametrize("fault", ["pixels", "missing_frame", "unavailable", "timeout"])
def test_incomplete_runtime_never_becomes_a_demo_trace(tmp_path, fault):
    executor, text, _ = make_run(tmp_path, fault=fault)
    result = invoke(executor)
    assert result.status == "partial", result.error
    doc = report(executor)
    assert not text.prompts and not doc.comparisons
    assert all(s.trace_ref is None for s in doc.sessions)
    assert not any(e.kind in ("playthrough", "replay") for e in result.proposed_updates.evidence)


def test_executed_events_are_checked_not_copied_from_model(tmp_path):
    executor, _, _ = make_run(tmp_path, plan(), FINISH, fault="events")
    result = invoke(executor)
    assert result.status == "partial", result.error
    doc = report(executor)
    assert doc.sessions[0].status == "passed"  # No-input probe is valid.
    assert doc.sessions[1].status == "failed" and doc.sessions[1].trace_ref is None
    assert "events" in doc.sessions[1].reason


def test_actual_mismatch_is_retained_without_changing_requirements(tmp_path):
    executor, _, _ = make_run(tmp_path, plan(), FINISH, fault="diverge")
    before = executor.state.requirements
    result = invoke(executor)
    assert result.status == "partial", result.error
    assert all(c.status == "failed" and c.state_match and not c.pixel_match and c.first_pixel_difference == 0 for c in report(executor).comparisons)
    assert executor.state.requirements == before
    assert len([e for e in result.proposed_updates.evidence if e.kind == "replay" and e.outcome == "failed"]) == 2


@pytest.mark.parametrize("fault", ["extra", "tamper_previous", "cleanup"])
def test_invalid_runtime_outputs_cannot_commit(tmp_path, fault):
    executor, _, _ = make_run(tmp_path, plan(), FINISH, fault=fault)
    before = executor.state
    result = invoke(executor)
    assert result.status == "failed"
    assert executor.state.state_revision == before.state_revision
    assert not result.artifact_refs


def test_protocol_repair_is_bounded_and_metered(tmp_path):
    executor, text, backend = make_run(tmp_path, '{"action":"finish","action":"play"}', plan(), FINISH)
    result = invoke(executor)
    assert result.status == "completed", result.error
    assert len(text.prompts) == 3 and len(backend.commands) == 4
    assert len(report(executor).protocol_errors) == 1
    assert text.prompts[1]["protocol_feedback"]


def test_missing_game_blocks_before_model_or_runtime(tmp_path):
    executor, text, backend = make_run(tmp_path, seed=False)
    result = invoke(executor)
    assert result.status == "blocked" and not text.prompts and not backend.commands


def test_configuration_is_frozen_on_repeat_invocation(tmp_path):
    executor, _, _ = make_run(tmp_path, plan(), FINISH)
    assert invoke(executor, request_id="one").status == "completed"
    result = invoke(executor, PlayerConfig(seed=2), request_id="two")
    assert result.status == "failed" and "frozen" in result.error.message


def test_model_budget_is_enforced_before_next_call(tmp_path):
    executor, text, _ = make_run(tmp_path, plan(), FINISH, budget=ResourceLimits(max_model_calls=1,
        max_input_tokens=1000000, max_output_tokens=8192, max_tool_calls=1000))
    result = invoke(executor)
    assert result.status == "failed" and len(text.prompts) == 1
    assert "budget" in result.error.code


def test_portable_directory_digest_matches_linux_driver(tmp_path):
    for name in ("Main.tscn", "assets/a.png", "Z.gd", "a.gd", "a_dir/B.gd"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    driver = runpy.run_path(str(ROOT / "configs/player/player_driver.py"))
    assert digest_path(tmp_path) == driver["tree_hash"](tmp_path)
    paths = sorted(["Main.tscn", "assets/a.png", "Z.gd", "a.gd", "a_dir/B.gd"], key=lambda n: tuple(p.casefold() for p in n.split("/")))
    expected = hashlib.sha256(b"".join(name.encode() + b"\0" + hashlib.sha256(name.encode()).digest() for name in paths)).hexdigest()
    assert digest_path(tmp_path) == expected


def test_player_gateway_prefix_uses_existing_credentials(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PLAYER_MODEL=example-model\nMODEL_GATEWAY_API_KEY=example-secret\nMODEL_GATEWAY_BASE_URL=https://example.invalid/v1\nPLAYER_THINKING=disabled\n")
    config = GatewayConfig.from_env(env, prefix="PLAYER", environ={})
    assert config.model == "example-model" and config.thinking == "disabled"
    assert "example-secret" not in json.dumps(config.public_settings())


def test_checkpoint_repair_explains_all_invalid_trials_without_executing_them(tmp_path):
    invalid = plan()
    invalid["trials"][0]["checkpoint_frames"] = [0, 1, 2, 3, 4]
    invalid["trials"].append({**invalid["trials"][0], "trace_id": "second"})
    executor, text, backend = make_run(tmp_path, invalid, plan(), FINISH)
    result = invoke(executor)
    assert result.status == "completed", result.error
    assert len(backend.commands) == 4  # Probe, one corrected capture, two replays.
    feedback = text.prompts[1]["protocol_feedback"][0]["error"]
    assert "Trial movement" in feedback and "Trial second" in feedback
    assert "at most 4" in feedback
    assert text.prompts[0]["output_schema"]["$defs"]["TrialPlan"]["properties"]["checkpoint_frames"]["maxItems"] == 4


def test_existing_trace_is_rerecorded_with_new_evidence(tmp_path):
    executor, text, _ = make_run(tmp_path, plan(), FINISH)
    assert invoke(executor, request_id="first").status == "completed"
    previous = report(executor)
    source = previous.sessions[1].trace_ref
    text.replies.extend([{"action": "play", "summary": "Reproduce the requested historical trace", "trials": [{
        "trace_id": "historical", "purpose": "Repeat recorded input", "source_trace_ref": source.model_dump(mode="json")}]}, FINISH])
    result = invoke(executor, request_id="second", artifact_refs=(source,))
    assert result.status == "completed", result.error
    current = report(executor)
    assert current.sessions[1].trace_ref != source
    assert current.sessions[1].executed_trace == previous.sessions[1].executed_trace
    assert len(text.prompts[-2]["input"]["existing_traces"]) == 1
    assert all(e.evidence_id.startswith("player-0002-") for e in result.proposed_updates.evidence)


def test_game_checksum_mismatch_prevents_model_and_play(tmp_path):
    executor, text, backend = make_run(tmp_path)
    game = executor.state.game_revisions[-1]
    (executor.workspace.read_public(game.project_ref.path) / "project.godot").write_text("modified in fixture")
    result = invoke(executor)
    assert result.status == "failed" and "checksum" in result.error.message
    assert not text.prompts and not backend.commands


def test_replay_checks_interior_frames_even_without_screenshots():
    from types import SimpleNamespace
    capture = SimpleNamespace(trace_id="movement", session_id="capture", status="passed",
        state_digests=("a", "b", "c", "d"), pixel_digests=("e", "f", "g", "h"))
    replay = SimpleNamespace(trace_id="movement", session_id="replay", status="passed",
        state_digests=("a", "b", "changed", "d"), pixel_digests=capture.pixel_digests)
    comparison = compare_sessions(capture, replay)
    assert comparison.status == "failed" and comparison.first_state_difference == 2 and comparison.pixel_match
    replay.status = "failed"
    assert compare_sessions(capture, replay).status == "not_run"
