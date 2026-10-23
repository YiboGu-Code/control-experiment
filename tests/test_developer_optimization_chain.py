"""Both real controllers + role adapters + SDK; external replies are OFFLINE fixtures."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil

import pytest

httpx = pytest.importorskip("httpx")
from adaptive_game_generation.controllers import AdaptiveScheduler, FixedLoop, FixedLoopConfig, SchedulerConfig
from adaptive_game_generation.controllers.results import export_run, write_json
from adaptive_game_generation.models import Role, RoleCallFinished
from adaptive_game_generation.providers import OpenAICompatibleTextProvider
from adaptive_game_generation.roles.art_producer import ArtProducer, ArtProducerConfig
from adaptive_game_generation.roles.developer import Developer, DeveloperConfig, DeveloperReport
from adaptive_game_generation.roles.player import PlayerReport
from adaptive_game_generation.roles.qa_reviewer import QAReport
from adaptive_game_generation.runtime import Executor, Services
from test_art_producer import asset, proposal as art_proposal
from test_designer import INSTRUCTION
from test_developer import proposal, ScriptedSandbox
from test_developer_optimization import SCRIPT, FIXED, PARSE, context_read, edit_proposal
from test_fixed_loop import make_run, report
from test_godot_mcp import MCPFixture, VisualFixture, FINISH
from test_provider import settings, completion
from test_scheduler import ScriptedScheduler


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_full_chain_profiles_reads_edits_and_independent_replay(tmp_path, controller):
    order = ("designer", "art_producer", "developer", "player", "qa_reviewer")
    scheduler = ScriptedScheduler(order)
    executor, adapters, workers, images = make_run(tmp_path, controller=controller, scheduler_provider=scheduler)
    adapters[Role.ART_PRODUCER] = ArtProducer(ArtProducerConfig(normalize_assets=True))
    workers[Role.ART_PRODUCER].replies = [art_proposal(asset(layout={"kind": "sprite", "mode": "whole"}))]
    adapters[Role.DEVELOPER] = Developer(DeveloperConfig(prompt_version="developer.v4", optimize_execution=True,
        require_normalized_assets=True, auto_source_bytes=1024, max_model_turns=6))
    answers = [proposal(script=SCRIPT), context_read(), edit_proposal(), FINISH]
    wire = []
    def respond(request):
        wire.append(json.loads(request.content))
        return httpx.Response(200, json=completion(text=json.dumps(answers[len(wire) - 1])))
    class CleanVisual(VisualFixture):
        async def generate(self, *args, **kwargs):
            reply = await super().generate(*args, **kwargs)
            data = json.loads(reply.text)
            data["findings"] = []
            return replace(reply, text=json.dumps(data))
    visual, mcp, backend = CleanVisual(), MCPFixture(), ScriptedSandbox(PARSE)
    responses = {role: [] for role in workers}
    for role, worker in [*workers.items(), ("developer_visual", visual)]:
        responses[role] = []
        original = worker.generate
        async def recorded(*args, _role=role, _generate=original, **kwargs):
            reply = await _generate(*args, **kwargs)
            responses[_role].append(reply.text)
            return reply
        worker.generate = recorded
    async def run():
        nonlocal executor
        async with OpenAICompatibleTextProvider(settings(developer_profiles=True, reasoning_effort="high", enable_thinking=True,
            max_output_tokens=adapters[Role.DEVELOPER].config.max_output_tokens),
            capture_directory=tmp_path / "model_calls/developer",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as provider:
            services = {**executor.services_by_role, Role.DEVELOPER: Services(text=provider, sandbox=backend,
                godot_mcp=mcp, vision=visual)}
            executor = Executor.open(executor.workspace, services_by_role=services, services=Services(text=scheduler))
            if controller == "fixed_loop":
                return await FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=1)).run()
            return await AdaptiveScheduler(executor, adapters, SchedulerConfig(max_role_calls=5)).run()
    state = asyncio.run(run())
    finished = [e.payload for e in state.ledger.events if isinstance(e.payload, RoleCallFinished)]
    assert [r.result.role.value for r in finished] == list(order), [(r.result.role, r.result.error) for r in finished]
    assert all(r.result.status != "failed" for r in finished), [(r.result.role, r.result.error) for r in finished]
    doc = report(executor, "developer-report", DeveloperReport)
    player = report(executor, "player-report", PlayerReport)
    qa = report(executor, "qa-report", QAReport)
    assert [c["stage"] for c in doc.optimization_calls] == ["deep", "local_repair", "local_repair", "finalize"]
    assert [c["reasoning_effort"] for c in wire] == ["high", "medium", "medium", "low"]
    assert all(c["enable_thinking"] is True and c["max_tokens"] == adapters[Role.DEVELOPER].config.max_output_tokens for c in wire)
    assert [a.status for a in doc.build_attempts] == ["failed", "passed"] and doc.self_review_completed
    assert executor.workspace.read_public(doc.project_ref.path + "/scripts/main.gd").read_text(encoding="utf-8") == FIXED
    assert len(mcp.calls) == len(visual.prompts) == 1 and len(backend.commands) == 6
    assert player.comparisons and all(c.status == "passed" for c in player.comparisons)
    assert player.game_revision_id == qa.game_revision_id == doc.game_revision_id
    assert not qa.gates["final_art"] and not state.readiness.ready  # Fixture art must never count as delivery.
    dev_event = next(e for e in finished if e.result.role == Role.DEVELOPER)
    assert dev_event.usage.model_calls == 5  # Four main decisions plus one visual helper.
    for request in wire:
        payload = json.loads(request["messages"][-1]["content"])
        assert payload["input"]["task"]["instruction"] == INSTRUCTION.read_text(encoding="utf-8")
        assert "HIDDEN-LOOP-SENTINEL" not in json.dumps(payload) and "SECRET-LOOP-SENTINEL" not in json.dumps(payload)
    output = tmp_path
    (output / "instruction.md").write_bytes(INSTRUCTION.read_bytes())
    for name, prompts, replies in [("scheduler", scheduler.prompts, scheduler.responses),
            ("developer_visual", visual.prompts, responses["developer_visual"]),
            *((r.value, p.prompts, responses[r]) for r, p in workers.items() if r != Role.DEVELOPER)]:
        for index, (prompt, reply) in enumerate(zip(prompts, replies), 1):
            write_json(output / "offline_model_io" / name / f"{index:03}.json", {"input": prompt, "output": json.loads(reply)})
    write_json(output / "image_calls.json", images.calls)
    write_json(output / "optimization_summary.json", {
        "mode": "OFFLINE HTTP/model/engine/visual/replay fixtures; mock art; NOT a game-quality or latency measurement",
        "controller": controller, "calls": doc.optimization_calls, "model_calls_with_visual": dev_event.usage.model_calls,
        "full_write_repair_bytes": len(json.dumps(proposal(script=FIXED)).encode()),
        "local_edit_repair_bytes": len(json.dumps(edit_proposal()).encode()),
        "replay_comparisons": [c.model_dump(mode="json") for c in player.comparisons]})
    export_run(executor, output, mode=controller + "/offline-models-and-engines/mock-image-generation")
    review = output / "review.md"
    review.write_text("# Offline Developer optimization evidence\n\n"
        "The original Benchmark instruction is unchanged. All model replies and engine observations here are test fixtures. "
        "The real SDK, controllers, roles, accounting, patch application and validation execute locally. "
        "This verifies protocol behavior, not online speed or gameplay quality.\n\n" + review.read_text(encoding="utf-8"), encoding="utf-8")
    if destination := os.environ.get("DEVELOPER_OPTIMIZATION_REVIEW_ROOT"):
        target = Path(destination) / controller
        assert not target.exists(), "Never overwrite prior review evidence"
        shutil.copytree(output, target, ignore=shutil.ignore_patterns("task"))
