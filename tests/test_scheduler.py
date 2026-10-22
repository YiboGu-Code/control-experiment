"""Offline protocol and orchestration tests; all model/engine fixtures are labeled."""

import asyncio
import json
import os
from pathlib import Path
import shutil

import pytest

from adaptive_game_generation.controllers import AdaptiveScheduler, FixedLoop, FixedLoopConfig, ModelScheduler, SchedulerConfig
from adaptive_game_generation.controllers.common import role_request
from adaptive_game_generation.controllers.results import export_run, write_json
from adaptive_game_generation.controllers.scheduler_policy import candidates_for
from adaptive_game_generation.models import Blocker, ResourceUsage, Role, RoleCallFinished, RoleResult, SchedulerDecision
from adaptive_game_generation.roles.developer import DeveloperReport
from adaptive_game_generation.roles.player import PlayerReport
from adaptive_game_generation.roles.qa_reviewer import QAReport
from adaptive_game_generation.runtime import Executor, InvalidSubmission, Services, TextReply
from adaptive_game_generation.runtime.commit import digest_path
from adaptive_game_generation.models.base import utc_now

from test_designer import INSTRUCTION
from test_fixed_loop import make_run, report
from test_fixed_loop_images import runner_settings


class ScriptedScheduler:
    def __init__(self, roles=("designer",), mutate=None, failure=None):
        self.roles, self.mutate, self.failure = roles, mutate, failure
        self.prompts, self.responses = [], []

    def input_token_upper_bound(self, prompt):
        return len(prompt.encode()) + 1024

    async def generate(self, prompt, *, max_output_tokens, timeout):
        data = json.loads(prompt)
        self.prompts.append(data)
        if self.failure:
            raise self.failure
        role = self.roles[min(len(self.prompts) - 1, len(self.roles) - 1)]
        candidate = next(c for c in data["candidates"] if c["role"] == role)
        response = {"state_revision": data["state"]["state_revision"], "selected_role": role,
            "work_item_ids": candidate["work_item_ids"],
            "objective": "Address the selected public work and preserve current-version evidence boundaries.",
            "completion_criteria": ["Produce the role's declared outputs or report concrete blockers."],
            "rationale": "Explicit offline fixture verifying adaptive role order and shared accounting."}
        if self.mutate:
            response = self.mutate(response, data)
        raw = response if isinstance(response, str) else json.dumps(response)
        self.responses.append(raw)
        return TextReply(raw, 1000, 500)


def setup(tmp_path, **kwargs):
    provider = kwargs.pop("provider", None) or ScriptedScheduler()
    executor, adapters, workers, images = make_run(tmp_path, controller="adaptive_scheduler", scheduler_provider=provider, **kwargs)
    return executor, adapters, provider, workers, images


def decisions(state):
    return [e.payload for e in state.ledger.events if isinstance(e.payload, SchedulerDecision)]


def results(state):
    return [e.payload.result for e in state.ledger.events if isinstance(e.payload, RoleCallFinished)]


def test_adaptive_order_reuses_real_roles_and_exports_full_offline_review(tmp_path):
    order = ("designer", "art_producer", "developer", "qa_reviewer", "player", "developer", "player", "qa_reviewer")
    provider = ScriptedScheduler(order)
    executor, adapters, provider, workers, images = setup(tmp_path, provider=provider, max_model_calls=40)
    worker_responses = {role: [] for role in workers}
    for role, worker in workers.items():
        original = worker.generate
        async def recorded(*args, _role=role, _generate=original, **kwargs):
            reply = await _generate(*args, **kwargs)
            worker_responses[_role].append(reply.text)
            return reply
        worker.generate = recorded
    config = SchedulerConfig(max_role_calls=8)
    state = asyncio.run(AdaptiveScheduler(executor, adapters, config).run())
    assert state.ledger.termination.reason == "iteration_limit"
    assert [r.role.value for r in results(state)] == list(order)
    assert all(r.status != "failed" for r in results(state)), [(r.role, r.error) for r in results(state)]
    assert all(d.error is None for d in decisions(state)) and len(decisions(state)) == 8
    assert state.current_game_revision_id == "game-0002"
    dev = report(executor, "developer-report", DeveloperReport)
    player = report(executor, "player-report", PlayerReport)
    qa = report(executor, "qa-report", QAReport)
    assert dev.packaged_traces and player.game_revision_id == qa.game_revision_id == dev.game_revision_id
    assert qa.gates["packaged_traces_replayed"] and not qa.gates["final_art"]
    assert not state.readiness.ready and len(images.calls) == 1
    assert all(p["state"]["task"]["instruction"] == INSTRUCTION.read_text(encoding="utf-8") for p in provider.prompts)
    assert "HIDDEN-LOOP-SENTINEL" not in json.dumps(provider.prompts)
    assert "SECRET-LOOP-SENTINEL" not in json.dumps(provider.prompts)
    assert state.ledger.usage.model_calls == len(provider.prompts) + sum(len(p.prompts) for p in workers.values())
    call = json.loads((executor.workspace.private / "controller/calls/schedule-001-designer/request.json").read_bytes())
    assert decisions(state)[0].objective in call["objective"]
    assert all(c in call["completion_criteria"] for c in decisions(state)[0].completion_criteria)
    assert asyncio.run(AdaptiveScheduler(executor, adapters, config).run()) == state
    output = tmp_path
    (output / "instruction.md").write_bytes(INSTRUCTION.read_bytes())
    write_json(output / "experiment.json", {"mode": "OFFLINE SCRIPTED MODELS AND ENGINE; MOCK ART",
        "controller": config.model_dump(mode="json"), "task_id": state.task.task_id,
        "total_budget": state.ledger.budget.model_dump(mode="json")})
    write_json(output / "image_calls.json", images.calls)
    # Preserve complete synthetic provider I/O without implying an online request.
    for name, prompts, responses in [("scheduler", provider.prompts, provider.responses),
                                    *((r.value, p.prompts, worker_responses[r]) for r, p in workers.items())]:
        for index, prompt in enumerate(prompts, 1):
            write_json(output / "offline_model_inputs" / name / f"{index:03}.json", prompt)
        for index, response in enumerate(responses, 1):
            write_json(output / "offline_model_outputs" / name / f"{index:03}.json", json.loads(response))
    offsets = {role: 0 for role in workers}
    for event in state.ledger.events:
        item = event.payload
        if isinstance(item, RoleCallFinished):
            role = item.result.role
            indices = range(offsets[role] + 1, offsets[role] + item.usage.model_calls + 1)
            write_json(output / "model_call_index" / (item.result.request_id + ".json"), {
                "mode": "offline-scripted-fixtures", "request_id": item.result.request_id,
                "inputs": [f"offline_model_inputs/{role.value}/{i:03}.json" for i in indices],
                "outputs": [f"offline_model_outputs/{role.value}/{i:03}.json" for i in indices]})
            offsets[role] += item.usage.model_calls
    for index, decision in enumerate(decisions(state), 1):
        write_json(output / "model_call_index" / (decision.decision_id + ".json"), {
            "mode": "offline-scripted-fixtures", "decision_id": decision.decision_id,
            "inputs": [f"offline_model_inputs/scheduler/{index:03}.json"],
            "outputs": [f"offline_model_outputs/scheduler/{index:03}.json"]})
    summary = export_run(executor, output, mode="adaptive-scheduler/offline-scripted-models/offline-scripted-engine/mock-image-generation/")
    assert summary["scheduler_usage"]["model_calls"] == 8
    assert summary["scheduler_usage"]["model_calls"] + summary["worker_usage"]["model_calls"] == summary["usage"]["model_calls"]
    assert digest_path(output / "delivery/game") == dev.project_ref.sha256
    review = output / "review.md"
    review.write_text("# Offline integration evidence\n\nAll text model replies and engine observations are scripted test fixtures. "
        "The instruction is the real Benchmark task. This verifies orchestration, not generated game quality.\n\n" +
        review.read_text(encoding="utf-8"), encoding="utf-8")
    if destination := os.environ.get("GAMECRAFT_SCHEDULER_REVIEW_OUTPUT"):
        target = Path(destination)
        assert not target.exists(), "Never overwrite an existing review"
        shutil.copytree(output, target, ignore=shutil.ignore_patterns("task"))


@pytest.mark.parametrize("mutate", [
    lambda r, p: "not JSON",
    lambda r, p: {**r, "selected_role": "developer"},
    lambda r, p: {**r, "state_revision": 999},
    lambda r, p: {**r, "work_item_ids": ["UNKNOWN"]},
    lambda r, p: {**r, "selected_role": None},
    lambda r, p: {**r, "completion_criteria": []},
    lambda r, p: {**r, "limits": {"max_model_calls": 999}},
    lambda r, p: {**r, "ready": True},
])
def test_invalid_decisions_are_charged_and_never_dispatch_workers(tmp_path, mutate):
    executor, adapters, provider, workers, _ = setup(tmp_path, provider=ScriptedScheduler(mutate=mutate))
    state = asyncio.run(AdaptiveScheduler(executor, adapters).run())
    assert state.ledger.termination.reason == "failed" and not results(state)
    assert state.ledger.usage.model_calls == 2 and len(provider.prompts) == 2
    assert all(d.error for d in decisions(state)) and all(not p.prompts for p in workers.values())
    assert (executor.workspace.private / "controller/decisions/schedule-001/response.txt").exists()


def test_paid_selection_exhausts_shared_budget_before_worker(tmp_path):
    executor, adapters, provider, workers, _ = setup(tmp_path, max_model_calls=1)
    state = asyncio.run(AdaptiveScheduler(executor, adapters).run())
    assert state.ledger.termination.reason == "budget_exhausted"
    assert state.ledger.usage.model_calls == 1 and not results(state)
    assert not any(p.prompts for p in workers.values())


def test_unknown_scheduler_usage_fails_closed_without_retry(tmp_path):
    executor, adapters, provider, _, _ = setup(tmp_path, provider=ScriptedScheduler(failure=ValueError("PRIVATE-ENDPOINT")))
    state = asyncio.run(AdaptiveScheduler(executor, adapters).run())
    assert state.ledger.termination.reason == "failed" and len(provider.prompts) == 1
    assert not state.ledger.usage.token_usage_complete and not results(state)
    assert "PRIVATE-ENDPOINT" not in state.model_dump_json()


def test_no_progress_stalls_without_claiming_completion(tmp_path):
    executor, adapters, provider, _, _ = setup(tmp_path)
    async def blocked(context):
        return RoleResult(request_id=context.request.request_id, state_revision=context.request.state_revision,
            role="designer", status="blocked", summary="No applicable design change",
            blockers=(Blocker(description="Missing public dependency"),))
    adapters[Role.DESIGNER] = blocked
    state = asyncio.run(AdaptiveScheduler(executor, adapters).run())
    assert state.ledger.termination.reason == "stalled" and len(provider.prompts) == 1


def test_worker_failure_limit_is_shared_and_scheduler_cannot_write(tmp_path):
    executor, adapters, provider, _, _ = setup(tmp_path)
    async def failed(context):
        raise InvalidSubmission("Rejected worker submission")
    adapters[Role.DESIGNER] = failed
    state = asyncio.run(AdaptiveScheduler(executor, adapters).run())
    assert state.ledger.termination.reason == "failed" and len(results(state)) == 2


def test_scheduler_write_attempt_is_rejected_by_common_executor(tmp_path):
    executor, adapters, _, _, _ = setup(tmp_path)
    async def attack(context, candidates, folder):
        await context.tools.write_text("game/escape.gd", "forbidden")
    state = asyncio.run(AdaptiveScheduler(executor, adapters, decision_adapter=attack).run())
    assert all(d.error.code == "permission_denied" for d in decisions(state))
    assert not results(state) and not state.game_revisions


@pytest.mark.parametrize("worker_finished", [False, True])
def test_durable_decision_and_worker_recovery_never_repeat_paid_calls(tmp_path, worker_finished):
    executor, adapters, provider, workers, _ = setup(tmp_path)
    config = SchedulerConfig(max_role_calls=1)
    controller = AdaptiveScheduler(executor, adapters, config)
    directory = controller.store.directory
    write_json(directory / "checkpoint.json", {"config": config.model_dump(mode="json"), "slots": [
        {"id": "schedule-001", "decision": "select", "state_before": 0, "reason": "Recovery fixture"}]})
    candidates = candidates_for(executor.state, controller.work)
    async def select(context):
        return await ModelScheduler(config)(context, candidates, directory / "decisions/schedule-001")
    decision = asyncio.run(executor.decide("schedule-001", select))
    assert decision.error is None
    if worker_finished:
        request = role_request(executor, Role.DESIGNER, "schedule-001-designer", (), objective=decision.objective,
                               completion_criteria=decision.completion_criteria)
        assert asyncio.run(executor.execute(request, adapters[Role.DESIGNER])).status == "completed"
    reopened = Executor.open(executor.workspace, services=Services(text=provider), services_by_role=dict(executor.services_by_role))
    state = asyncio.run(AdaptiveScheduler(reopened, adapters, config).run())
    assert state.ledger.termination.reason == "iteration_limit"
    assert len(provider.prompts) == len(workers[Role.DESIGNER].prompts) == 1
    assert len(decisions(state)) == len(results(state)) == 1
    assert (directory / "calls/schedule-001-designer/result.json").exists()
    with pytest.raises(InvalidSubmission, match="configuration"):
        asyncio.run(AdaptiveScheduler(reopened, adapters, config.revised(max_role_calls=2)).run())


def test_pending_scheduler_reservation_survives_recovery(tmp_path):
    executor, adapters, provider, _, _ = setup(tmp_path)
    with executor.store.locked():
        executor.store.start_journal("schedule-001", "scheduler", 0, utc_now().isoformat())
        executor.store.journal("schedule-001", ResourceUsage(model_calls=1, input_tokens=1000, output_tokens=4096,
                                                            token_usage_complete=False))
    reopened = Executor.open(executor.workspace, services=Services(text=provider), services_by_role=dict(executor.services_by_role))
    state = asyncio.run(AdaptiveScheduler(reopened, adapters).run())
    assert state.ledger.termination.reason == "failed" and state.ledger.usage.model_calls == 1
    assert decisions(state)[0].error.code == "interrupted" and not provider.prompts


def test_scheduler_cancellation_is_saved(tmp_path):
    executor, adapters, provider, _, _ = setup(tmp_path, provider=ScriptedScheduler(failure=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(AdaptiveScheduler(executor, adapters).run())
    state = executor.state
    assert state.ledger.termination.reason == "cancelled"
    assert len(decisions(state)) == 1 and decisions(state)[0].error.code == "cancelled"
    assert (executor.workspace.private / "controller/decisions/schedule-001/result.json").exists()


def test_fixed_loop_obeys_same_explicit_role_call_cap(tmp_path):
    executor, adapters, workers, _ = make_run(tmp_path)
    state = asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_role_calls=1)).run())
    assert state.ledger.termination.reason == "iteration_limit"
    assert len(results(state)) == 1 and not workers[Role.ART_PRODUCER].prompts
    assert not decisions(state)


def test_prompt_cap_rejects_before_paid_dispatch(tmp_path):
    executor, adapters, provider, _, _ = setup(tmp_path)
    state = asyncio.run(AdaptiveScheduler(executor, adapters, SchedulerConfig(max_prompt_bytes=16)).run())
    assert state.ledger.termination.reason == "failed" and not provider.prompts
    assert all(d.error.code == "invalid_submission" for d in decisions(state))
    assert state.ledger.usage.model_calls == 0


@pytest.mark.parametrize("limit,reason", [("max_model_calls", "budget_exhausted"), ("max_wall_seconds", "timeout")])
def test_exhausted_run_does_not_pay_for_another_decision(tmp_path, limit, reason):
    executor, adapters, provider, _, _ = setup(tmp_path)
    with executor.store.locked():
        state = executor.store.load()
        budget = state.ledger.budget.revised(**{limit: 0.0 if limit == "max_wall_seconds" else 0})
        executor.store.save(state.revised(ledger=state.ledger.revised(budget=budget)))
    state = asyncio.run(AdaptiveScheduler(executor, adapters).run())
    assert state.ledger.termination.reason == reason and not provider.prompts
    assert not decisions(state) and not results(state)


def test_decision_cannot_be_retargeted_after_an_external_state_change(tmp_path):
    executor, adapters, provider, workers, _ = setup(tmp_path)
    controller = AdaptiveScheduler(executor, adapters)
    candidates = candidates_for(executor.state, controller.work)
    async def choose(context):
        return await controller.selector(context, candidates, controller.store.directory / "decisions/schedule-001")
    assert asyncio.run(executor.decide("schedule-001", choose)).error is None
    request = role_request(executor, Role.DESIGNER, "external-design", ())
    assert asyncio.run(executor.execute(request, adapters[Role.DESIGNER])).status == "completed"
    with pytest.raises(InvalidSubmission, match="stale"):
        asyncio.run(controller.run())
    assert executor.state.ledger.termination.reason == "failed"
    assert len(provider.prompts) == len(workers[Role.DESIGNER].prompts) == 1
    assert not workers[Role.ART_PRODUCER].prompts


def test_legacy_fixed_checkpoint_without_optional_role_cap_is_readable(tmp_path):
    executor, adapters, _, _ = make_run(tmp_path)
    config = FixedLoopConfig(max_cycles=1)
    write_json(executor.workspace.private / "controller/checkpoint.json", {
        "config": config.model_dump(mode="json", exclude={"max_role_calls"}), "slots": []})
    async def blocked(context):
        return RoleResult(request_id=context.request.request_id, state_revision=context.request.state_revision,
            role="designer", status="blocked", summary="Legacy checkpoint fixture",
            blockers=(Blocker(description="Missing public dependency"),))
    adapters[Role.DESIGNER] = blocked
    assert asyncio.run(FixedLoop(executor, adapters, config).run()).ledger.termination.reason == "stalled"


def test_known_requirement_cannot_be_assigned_outside_candidate_work(tmp_path):
    def mutate(reply, prompt):
        if reply["selected_role"] == "art_producer":
            return {**reply, "work_item_ids": [prompt["state"]["requirements"][0]["requirement_id"]]}
        return reply
    executor, adapters, _, _, _ = setup(tmp_path, provider=ScriptedScheduler(("designer", "art_producer"), mutate=mutate))
    state = asyncio.run(AdaptiveScheduler(executor, adapters, SchedulerConfig(max_decisions=2)).run())
    assert len(results(state)) == 1
    assert "outside" in decisions(state)[-1].error.message


def test_live_runner_routes_scheduler_and_worker_through_real_adapter_with_offline_http(tmp_path, monkeypatch):
    import httpx
    from adaptive_game_generation.providers import OpenAICompatibleTextProvider
    from test_designer import response as design_response
    runner, env = runner_settings(tmp_path, monkeypatch)
    for name in list(os.environ):
        if name.startswith("SCHEDULER_"):
            monkeypatch.delenv(name)
    with env.open("a") as stream:
        stream.write("SCHEDULER_MODEL=fixture-scheduler\nSCHEDULER_ENABLE_THINKING=true\n")
    calls = []
    def transport(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] == "fixture-scheduler":
            prompt = json.loads(body["messages"][-1]["content"])
            assert body["enable_thinking"] is True
            reply = {"state_revision": prompt["state"]["state_revision"], "selected_role": "designer", "work_item_ids": [],
                "objective": "Initialize the original public task design", "completion_criteria": ["Ground the public requirements"],
                "rationale": "Designer is the only eligible role in this fresh workspace"}
        else:
            reply = design_response()
        return httpx.Response(200, json={"id": "offline-http", "object": "chat.completion", "created": 0,
            "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(reply)}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 2000, "total_tokens": 3000}})
    def provider(config, *, capture_directory):
        return OpenAICompatibleTextProvider(config, capture_directory=capture_directory,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    monkeypatch.setitem(runner["run_live"].__globals__, "OpenAICompatibleTextProvider", provider)
    output = tmp_path / "routed"
    summary = asyncio.run(runner["run_live"](output, env_file=env, controller="adaptive_scheduler", mock_images=True,
                                            max_role_calls=1, max_model_calls=64))
    assert summary["termination_reason"] == "iteration_limit"
    assert summary["scheduler_usage"]["model_calls"] == summary["worker_usage"]["model_calls"] == 1
    assert [c["model"] for c in calls] == ["fixture-scheduler", "fixture-text-model"]
    assert (output / "model_calls/scheduler/001/request.json").exists()
    assert (output / "model_calls/designer/001/response.json").exists()
    assert json.loads((output / "model_call_index/schedule-001.json").read_bytes())["calls"] == ["model_calls/scheduler/001"]


@pytest.mark.parametrize("godot_mcp", [False, True])
def test_preflight_freezes_identical_worker_conditions_and_no_online_calls(tmp_path, monkeypatch, godot_mcp):
    import httpx
    runner, env = runner_settings(tmp_path, monkeypatch)
    for name in list(os.environ):
        if name.startswith("SCHEDULER_"):
            monkeypatch.delenv(name)
    with env.open("a") as stream:
        stream.write("SCHEDULER_MODEL=fixture-scheduler\nSCHEDULER_ENABLE_THINKING=true\n"
                     "SCHEDULER_STREAM=true\nSCHEDULER_MAX_OUTPUT_TOKENS=16384\nQA_VISION=true\n")
    async def forbidden(*args, **kwargs):
        raise AssertionError("Preflight must not call online models")
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    saved = []
    for controller in ("fixed_loop", "adaptive_scheduler"):
        output = tmp_path / controller
        asyncio.run(runner["run_live"](output, env_file=env, controller=controller, preflight_only=True,
                                      godot_mcp=godot_mcp, max_model_calls=64, max_role_calls=15))
        saved.append(json.loads((output / "experiment.json").read_bytes()))
    fixed, adaptive = saved
    assert fixed["comparison_conditions_sha256"] == adaptive["comparison_conditions_sha256"]
    for key in ("providers", "roles", "role_limits", "total_budget", "godot_mcp"):
        assert fixed[key] == adaptive[key]
    assert not fixed["scheduler"]["enabled"] and adaptive["scheduler"]["enabled"]
    assert adaptive["scheduler"]["limits"]["max_model_calls"] == 1
    assert adaptive["scheduler"]["limits"]["max_output_tokens"] == 16384
    assert adaptive["scheduler"]["provider"]["enable_thinking"] is True
    assert "RUNNER-SECRET" not in json.dumps(adaptive) and "runner.cn-beijing" not in json.dumps(adaptive)
