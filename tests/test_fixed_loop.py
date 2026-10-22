"""Offline orchestration tests. Fixtures never replace models/engines in the live runner."""

import asyncio
import hashlib
import json
import runpy
from pathlib import Path

import pytest

from adaptive_game_generation.controllers import FIXED_ORDER, FixedLoop, FixedLoopConfig
from adaptive_game_generation.controllers.results import export_run
from adaptive_game_generation.models import Blocker, ResourceLimits, Role, RoleCallFinished, RoleResult
from adaptive_game_generation.providers.mock_image import MockImageProvider
from adaptive_game_generation.roles.art_producer import ArtProducer
from adaptive_game_generation.roles.designer import Designer
from adaptive_game_generation.roles.developer import Developer, DeveloperConfig, DeveloperReport
from adaptive_game_generation.roles.player import Player, PlayerConfig, PlayerReport
from adaptive_game_generation.roles.qa_reviewer import QAConfig, QAReport, QAReviewer
from adaptive_game_generation.runtime import Executor, InvalidSubmission, MountedAssetLibraries, RuntimeConfig, Services, Workspace
from adaptive_game_generation.runtime.commit import digest_path
from adaptive_game_generation.runtime.store import atomic_text

from test_art_producer import ScriptedText, asset, proposal as art_proposal
from test_designer import INSTRUCTION, response as designer_proposal
from test_developer import ScriptedSandbox, proposal as developer_proposal
from test_player import FINISH, OfflineRenderer, plan as player_plan
from test_qa_reviewer import OfflineText

ROOT = Path(__file__).resolve().parents[1]


class Renderer(OfflineRenderer):
    """Add isolation receipt fields required by the real QA evidence reader."""
    async def run(self, workspace, argv, timeout):
        result = await super().run(workspace, argv, timeout)
        folder = workspace.output_path(argv[argv.index("--output") + 1].removeprefix("/output/"))
        path = folder / "driver.json"
        data = json.loads(path.read_bytes())
        data.update(fresh_process=True, fresh_user_data=True, network="none")
        path.write_text(json.dumps(data), encoding="utf-8")
        return result


def make_run(tmp_path, *, max_model_calls=30, fail_designer=False, renderer=None, image_service=None,
             controller="fixed_loop", scheduler_provider=None):
    source = tmp_path / "task"
    source.mkdir(parents=True)
    (source / "instruction.md").write_bytes(INSTRUCTION.read_bytes())
    (source / "tests").mkdir()
    (source / "tests/rubric.json").write_text("HIDDEN-LOOP-SENTINEL")
    (source / ".env").write_text("SECRET-LOOP-SENTINEL")
    workspace, task = Workspace.prepare(tmp_path / "workspace", source, task_id="platformer-time-loop")
    requirements = [r["requirement_id"] for r in designer_proposal()["requirements"]]
    patch = developer_proposal(affected_requirement_ids=requirements, implemented_requirement_ids=requirements)
    providers = {
        Role.DESIGNER: ScriptedText(*([ValueError("offline failure")] * 3 if fail_designer else [designer_proposal()])),
        Role.ART_PRODUCER: ScriptedText(art_proposal(asset())),
        Role.DEVELOPER: ScriptedText(patch, patch, patch),
        Role.PLAYER: ScriptedText(player_plan(), FINISH, FINISH, FINISH),
        Role.QA_REVIEWER: OfflineText(lambda proposal, count: {
            **proposal, "issues": [{**i, "issue": {**i["issue"],
                "suggested_role": "developer" if count == 1 else "player"}} for i in proposal["issues"]]}),
    }
    mock = image_service or MockImageProvider()
    services = {r: Services(text=p) for r, p in providers.items()}
    services[Role.ART_PRODUCER] = Services(text=providers[Role.ART_PRODUCER], image=mock, image_info=mock.info,
        libraries=MountedAssetLibraries(kenney=tmp_path / "absent-kenney", opengameart=tmp_path / "absent-oga"))
    services[Role.DEVELOPER] = Services(text=providers[Role.DEVELOPER], sandbox=ScriptedSandbox())
    services[Role.PLAYER] = Services(text=providers[Role.PLAYER], sandbox=renderer or Renderer())
    limits = ResourceLimits(max_model_calls=6, max_input_tokens=6000000, max_output_tokens=200000,
                            max_tool_calls=3000, max_image_calls=16)
    executor = Executor.create(workspace, task, run_id="loop-test", controller=controller,
        budget=limits.revised(max_model_calls=max_model_calls, max_tool_calls=15000, max_input_tokens=15000000, max_output_tokens=600000),
        config=RuntimeConfig(role_limits_by_role={r: limits for r in Role}, service_configuration_id="offline-loop-fixtures",
            scheduler_limits=ResourceLimits(max_model_calls=1, max_input_tokens=2000000, max_output_tokens=4096,
                                           max_tool_calls=10, max_wall_seconds=90.0)),
        services_by_role=services, services=Services(text=scheduler_provider))
    adapters = {Role.DESIGNER: Designer(), Role.ART_PRODUCER: ArtProducer(),
        Role.DEVELOPER: Developer(DeveloperConfig(prompt_version="developer.v3", max_model_turns=5)),
        Role.PLAYER: Player(PlayerConfig(prompt_version="player.v2")),
        Role.QA_REVIEWER: QAReviewer(QAConfig(max_model_turns=1))}
    return executor, adapters, providers, mock


def report(executor, identity, cls):
    ref = max((r for r in executor.state.artifacts if r.artifact_id == identity), key=lambda r: r.version)
    return cls.model_validate_json(executor.workspace.read_public(ref.path).read_bytes())


def slots(executor):
    return json.loads((executor.workspace.private / "controller/checkpoint.json").read_bytes())["slots"]


def test_fresh_chain_two_cycles_packages_and_replays_exact_new_revision(tmp_path):
    executor, adapters, providers, mock = make_run(tmp_path)
    state = asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=2)).run())
    assert state.ledger.termination.reason == "iteration_limit"
    assert all(s.get("status") != "failed" for s in slots(executor)), slots(executor)
    assert [s["role"] for s in slots(executor)[:5]] == [r.value for r in FIXED_ORDER]
    assert [(s["role"], s["status"]) for s in slots(executor)[5:7]] == [("designer", "skipped"), ("art_producer", "skipped")]
    assert state.current_game_revision_id == "game-0002"
    dev = report(executor, "developer-report", DeveloperReport)
    player = report(executor, "player-report", PlayerReport)
    qa = report(executor, "qa-report", QAReport)
    assert len(dev.packaged_traces) == 1
    assert dev.packaged_traces[0].source_game_revision_id == "game-0001"
    packaged = executor.workspace.read_public(dev.project_ref.path + "/" + dev.packaged_traces[0].target_path).read_bytes()
    assert packaged == executor.workspace.read_public(dev.packaged_traces[0].trace_ref.path).read_bytes()
    assert player.game_revision_id == qa.game_revision_id == dev.game_revision_id == "game-0002"
    assert len(player.comparisons) == 2 and all(c.status == "passed" for c in player.comparisons)
    assert player.sessions[1].session_id == "packaged_01-capture"
    assert qa.gates["packaged_traces_replayed"] and not qa.gates["final_art"]
    assert not state.readiness.ready and len(mock.calls) == 1
    assert len(providers[Role.DESIGNER].prompts) == len(providers[Role.ART_PRODUCER].prompts) == 1
    assert all(p["input"]["task"]["instruction"] == INSTRUCTION.read_text(encoding="utf-8")
               for provider in providers.values() for p in provider.prompts)
    assert "SECRET-LOOP" not in str([p.prompts for p in providers.values()])
    assert "HIDDEN-LOOP" not in str([p.prompts for p in providers.values()])
    assert not any(e.payload.kind == "scheduler_decision" for e in state.ledger.events)
    before = state.ledger.usage
    assert asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=2)).run()) == state
    review = export_run(executor, tmp_path / "review")
    assert review["delivery_exported"] and not review["delivery_ready"]
    assert review["final_build_status"] == "passed"
    assert review["roles_not_invoked"] == []
    assert [g["revision_id"] for g in review["game_builds"]] == ["game-0001", "game-0002"]
    assert digest_path(tmp_path / "review/delivery/game") == dev.project_ref.sha256
    assert executor.state.ledger.usage == before  # Export is a byte copy, no extra execution.


def test_one_cycle_is_valid_run_but_not_ready_and_does_not_fabricate_packaged_traces(tmp_path):
    executor, adapters, _, _ = make_run(tmp_path)
    state = asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=1)).run())
    assert state.ledger.termination.reason == "iteration_limit"
    assert not state.readiness.ready
    assert not list(executor.workspace.read_public(state.game_revisions[-1].project_ref.path).glob("demo_outputs/*"))
    assert not report(executor, "qa-report", QAReport).gates["packaged_traces_replayed"]


def test_art_plan_correction_reaches_developer_in_the_same_fixed_cycle(tmp_path):
    executor, adapters, providers, image = make_run(tmp_path)
    providers[Role.ART_PRODUCER].replies.insert(0, art_proposal(asset(rows=6)))
    state = asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=1)).run())
    assert [s["status"] for s in slots(executor)[:3]] == ["completed", "partial", "partial"]
    assert state.current_game_revision_id == "game-0001"
    assert len(providers[Role.ART_PRODUCER].prompts) == 2 and len(image.calls) == 1
    assert "evenly divide" in str(providers[Role.ART_PRODUCER].prompts[1]["proposal_rejections"])
    assert providers[Role.DEVELOPER].prompts and providers[Role.QA_REVIEWER].prompts
    assert not state.readiness.ready  # Offline mock artwork is not final-art acceptance.


def test_shared_budget_stops_before_next_paid_role(tmp_path):
    executor, adapters, providers, _ = make_run(tmp_path, max_model_calls=1)
    state = asyncio.run(FixedLoop(executor, adapters).run())
    assert state.ledger.termination.reason == "budget_exhausted"
    assert state.ledger.usage.model_calls == 1
    assert not providers[Role.ART_PRODUCER].prompts and not state.game_revisions


def test_unknown_provider_usage_fails_closed_without_retry(tmp_path):
    executor, adapters, providers, _ = make_run(tmp_path, fail_designer=True)
    state = asyncio.run(FixedLoop(executor, adapters).run())
    assert state.ledger.termination.reason == "failed"
    assert not state.ledger.usage.token_usage_complete
    assert len(providers[Role.DESIGNER].prompts) == 1


def test_missing_prerequisite_skips_dependents_and_stalls(tmp_path):
    executor, adapters, providers, _ = make_run(tmp_path)
    async def blocked(context):
        return RoleResult(request_id=context.request.request_id, state_revision=context.request.state_revision,
            role="designer", status="blocked", summary="Offline missing dependency",
            blockers=(Blocker(description="Offline public input dependency"),))
    adapters[Role.DESIGNER] = blocked
    state = asyncio.run(FixedLoop(executor, adapters).run())
    assert state.ledger.termination.reason == "stalled"
    assert [s["status"] for s in slots(executor)] == ["blocked", *["skipped"] * 4]
    assert all(not p.prompts for p in providers.values())


def test_accounted_role_failures_retry_only_in_next_fixed_cycle(tmp_path):
    executor, adapters, _, _ = make_run(tmp_path)
    async def invalid(context):
        raise InvalidSubmission("Offline rejected role submission")
    adapters[Role.DESIGNER] = invalid
    state = asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_consecutive_failures=2)).run())
    assert state.ledger.termination.reason == "failed"
    assert [s["id"] for s in slots(executor) if s["status"] == "failed"] == ["loop-001-designer", "loop-002-designer"]


def test_role_services_and_limits_cannot_be_selected_by_request(tmp_path):
    executor, _, providers, _ = make_run(tmp_path)
    async def ask(context):
        await context.tools.generate_text(json.dumps({"input": "routing fixture " * 200}), max_output_tokens=8192)
        return RoleResult(request_id=context.request.request_id, state_revision=context.request.state_revision,
                          role="designer", status="no_op", summary="Routing fixture")
    request = executor.request("designer", "Route fixture", ("Correct provider",))
    assert request.limits == executor.config.limits_for("designer")
    bad = request.revised(limits=request.limits.revised(max_model_calls=100))
    with pytest.raises(Exception, match="limits"):
        asyncio.run(executor.execute(bad, ask))
    assert asyncio.run(executor.execute(request, ask)).status == "no_op"
    assert len(providers[Role.DESIGNER].prompts) == 1 and not providers[Role.ART_PRODUCER].prompts


def test_checkpoint_reconciliation_does_not_repeat_finished_calls(tmp_path):
    executor, adapters, providers, _ = make_run(tmp_path)
    # Simulate a checkpoint persisted before a role and a crash immediately after
    # that role's durable commit, before the controller can mark the slot finished.
    config = FixedLoopConfig(max_cycles=1)
    directory = executor.workspace.private / "controller"
    entry = {"id": "loop-001-designer", "cycle": 1, "role": "designer", "decision": "run",
             "reason": "Initialize design", "work_item_ids": [], "state_before": 0}
    atomic_text(directory / "checkpoint.json", json.dumps({"config": config.model_dump(mode="json"), "slots": [entry]}))
    request = executor.request("designer", "Initialize design", ("Ground the specification",), request_id=entry["id"])
    assert asyncio.run(executor.execute(request, adapters[Role.DESIGNER])).status == "completed"
    reopened = Executor.open(executor.workspace, services_by_role=dict(executor.services_by_role))
    state = asyncio.run(FixedLoop(reopened, adapters, config).run())
    assert len(providers[Role.DESIGNER].prompts) == 1
    assert len([e for e in state.ledger.events if isinstance(e.payload, RoleCallFinished) and e.payload.result.role == "designer"]) == 1


def test_runner_is_fresh_and_cli_defaults_are_real_models(tmp_path):
    runner = runpy.run_path(str(ROOT / "examples/fixed_loop_demo.py"))
    assert runner["DEFAULT_TASK"] / "instruction.md" == INSTRUCTION
    assert "seed_recorded_inputs" not in runner and "load_recorded_inputs" not in runner
    with pytest.raises(ValueError, match="new output"):
        asyncio.run(runner["run_live"](tmp_path))


def test_packaged_trace_replay_divergence_stays_failed_on_new_revision(tmp_path):
    executor, adapters, _, _ = make_run(tmp_path, renderer=Renderer("diverge"))
    state = asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=2)).run())
    assert state.current_game_revision_id == "game-0002"
    qa = report(executor, "qa-report", QAReport)
    assert not qa.gates["packaged_traces_replayed"] and not state.readiness.ready
    assert all(c.status == "failed" for c in report(executor, "player-report", PlayerReport).comparisons)


def test_generated_code_cannot_rewrite_protected_demo_inputs(tmp_path):
    executor, adapters, providers, _ = make_run(tmp_path)
    class Mutator(ScriptedSandbox):
        async def run(self, workspace, argv, timeout):
            result = await super().run(workspace, argv, timeout)
            for path in workspace.output_path("game").glob("demo_outputs/*.json"):
                path.write_text('{"duration_frames": 1, "events": []}')
            return result
    services = dict(executor.services_by_role)
    services[Role.DEVELOPER] = Services(text=providers[Role.DEVELOPER], sandbox=Mutator())
    executor = Executor.open(executor.workspace, services_by_role=services)
    state = asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=2)).run())
    assert state.current_game_revision_id == "game-0001"
    second_dev = next(s for s in slots(executor) if s["id"] == "loop-002-developer")
    assert second_dev["status"] == "failed" and "protected" in second_dev["error"]["message"]
    assert not list(executor.workspace.read_public(state.game_revisions[-1].project_ref.path).glob("demo_outputs/*"))


def test_cancel_records_invocation_and_run_without_publishing_partial_state(tmp_path):
    executor, adapters, _, _ = make_run(tmp_path)
    async def cancelled(context):
        raise asyncio.CancelledError()
    adapters[Role.DESIGNER] = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(FixedLoop(executor, adapters).run())
    assert executor.state.ledger.termination.reason == "cancelled"
    finished = [e.payload for e in executor.state.ledger.events if isinstance(e.payload, RoleCallFinished)]
    assert len(finished) == 1 and finished[0].result.error.code == "cancelled"
    assert executor.state.design is None
    assert slots(executor)[0]["status"] == "failed"
    assert (executor.workspace.private / "controller/calls/loop-001-designer/result.json").is_file()


def test_controller_config_cannot_change_on_reentry(tmp_path):
    executor, adapters, _, _ = make_run(tmp_path, max_model_calls=0)
    asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=1)).run())
    with pytest.raises(InvalidSubmission, match="configuration cannot change"):
        asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=2)).run())


def test_export_refuses_modified_final_game(tmp_path):
    executor, adapters, _, _ = make_run(tmp_path)
    asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=1)).run())
    project = executor.workspace.read_public(executor.state.game_revisions[-1].project_ref.path)
    (project / "Main.tscn").write_text("Modified after QA")
    with pytest.raises(ValueError, match="integrity"):
        export_run(executor, tmp_path / "review")
    assert not (tmp_path / "review/delivery/game").exists()


def test_design_revision_can_retain_art_without_forcing_regeneration(tmp_path):
    executor, adapters, providers, mock = make_run(tmp_path)
    revision = designer_proposal()
    revision["sections"][0]["content"] += "\nRetain existing artwork while clarifying implementation."
    providers[Role.DESIGNER].replies.append(revision)
    providers[Role.ART_PRODUCER].replies.append({"action": "submit", "status": "no_op",
                                              "summary": "Existing assets also satisfy the revised design."})
    providers[Role.QA_REVIEWER].transform = lambda proposal, count: {
        **proposal, "issues": [{**i, "issue": {**i["issue"],
            "suggested_role": "designer" if count == 1 else "player"}} for i in proposal["issues"]]}
    state = asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=2)).run())
    assert state.design.version == 2 and state.current_game_revision_id == "game-0002"
    assert slots(executor)[6]["status"] == "no_op" and slots(executor)[7]["status"] == "partial"
    assert len(mock.calls) == 1 and not any(s["status"] == "failed" for s in slots(executor))


def test_new_exploration_becomes_next_cycle_assembly_work(tmp_path):
    executor, adapters, providers, _ = make_run(tmp_path)
    extra = player_plan()
    extra["trials"][0]["trace_id"] = "new_exploration"
    extra["trials"][0]["trace"]["events"][0]["keycode"] = "A"
    providers[Role.PLAYER].replies = [player_plan(), FINISH, extra, FINISH, FINISH]
    state = asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=3)).run())
    assert state.current_game_revision_id == "game-0003"
    assert not any(s["status"] == "failed" for s in slots(executor)), slots(executor)
    dev = report(executor, "developer-report", DeveloperReport)
    assert len(dev.packaged_traces) == 2
    raw = executor.workspace.read_public(dev.packaged_traces[0].trace_ref.path).read_bytes()
    assert json.loads(raw)["events"][0]["keycode"] == "A"
    assert len(report(executor, "player-report", PlayerReport).comparisons) == 4


def test_retry_input_contains_safe_previous_rejection(tmp_path):
    executor, adapters, providers, _ = make_run(tmp_path)
    designer = adapters[Role.DESIGNER]
    attempts = []
    async def reject_once(context):
        attempts.append(context.request)
        if len(attempts) == 1:
            raise InvalidSubmission("The initial design must include all eight sections")
        return await designer(context)
    adapters[Role.DESIGNER] = reject_once
    asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=2)).run())
    assert "all eight sections" in providers[Role.DESIGNER].prompts[0]["input"]["request"]["objective"]
    assert len(attempts) == 2


def test_preflight_failure_writes_safe_diagnostics_without_credentials(tmp_path, monkeypatch):
    runner = runpy.run_path(str(ROOT / "examples/fixed_loop_demo.py"))
    def unavailable(*args, **kwargs):
        raise ValueError("Never log SECRET-TEST-KEY or https://private.invalid/token")
    monkeypatch.setattr(runner["GatewayConfig"], "from_env", unavailable)
    output = tmp_path / "preflight-failure"
    with pytest.raises(ValueError):
        asyncio.run(runner["run_live"](output))
    assert json.loads((output / "summary.json").read_bytes())["status"] == "preflight_failed"
    text = (output / "failure.json").read_text()
    assert "SECRET-TEST-KEY" not in text and "private.invalid" not in text
