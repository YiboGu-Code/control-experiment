"""Offline regressions from run 003; no online model/image calls."""

import asyncio
import copy
import hashlib
import json
import runpy
from pathlib import Path

import pytest

from adaptive_game_generation.models import Role
from adaptive_game_generation.providers import GatewayConfig
from adaptive_game_generation.roles.art_producer import ArtProducer, ArtProducerConfig
from adaptive_game_generation.roles.developer import DeveloperConfig
from adaptive_game_generation.roles.qa_reviewer import QAConfig, QAReport, QAReviewer
from adaptive_game_generation.roles.player.contracts import InputTrace
from test_art_producer import invoke as invoke_art, make_run as art_run, manifest, search_turn, proposal, asset
from test_developer import invoke as invoke_dev, make_run as dev_run, proposal as dev_proposal, ScriptedSandbox
from test_fixed_loop import make_run, report, FixedLoop, FixedLoopConfig
from test_player import plan, FINISH
from test_qa_reviewer import OfflineText, setup_executor, request

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures/fixed_loop_real_images_003"


def test_actual_art_omission_can_be_repaired_after_three_search_turns(tmp_path):
    raw = (FIXTURE / "missing_affected_scenes.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == json.loads((FIXTURE / "provenance.json").read_bytes())["sha256"]
    corrected = json.loads(raw)
    for draft in corrected["assets"]:
        draft["affected_scenes"] = ["Gameplay"]  # Synthetic schema correction only.
    executor, provider, image = art_run(tmp_path, search_turn(), search_turn(), search_turn(),
                                       raw.decode(), corrected)
    result = invoke_art(executor, ArtProducer(ArtProducerConfig(max_model_turns=6, max_search_turns=3)))
    assert result.status == "partial", result.error
    assert len(provider.prompts) == 5
    assert len(image.calls) == len(corrected["assets"])
    assert provider.prompts[3]["limits"]["search_turns_left"] == 0
    assert provider.prompts[3]["output_schema"]["properties"]["action"]["enum"] == ["submit"]
    errors = manifest(executor).proposal_rejections
    assert len(errors) == 1 and errors[0].model_turn == 4
    assert len(errors[0].errors) == 5 and all("affected_scenes" in e for e in errors[0].errors)
    assert provider.prompts[4]["search_observations"] == provider.prompts[3]["search_observations"]
    assert executor.state.ledger.usage.model_calls == 5


def test_art_search_cap_is_enforced_before_search_tools(tmp_path):
    executor, provider, image = art_run(tmp_path, search_turn(), proposal(asset()))
    result = invoke_art(executor, ArtProducer(ArtProducerConfig(max_model_turns=3, max_search_turns=0)))
    assert result.status == "partial", result.error
    assert not provider.prompts[1]["search_observations"]
    assert "Search turn allowance exhausted" in str(provider.prompts[1]["proposal_rejections"])
    assert len(image.calls) == 1


def test_small_patch_keeps_whole_project_static_provenance_without_runtime_acceptance(tmp_path):
    executor, _ = dev_run(tmp_path, dev_proposal(), dev_proposal(), backend=ScriptedSandbox())
    config = DeveloperConfig(prompt_version="developer.v3")
    assert invoke_dev(executor, config, request_id="first").status == "partial"
    assert invoke_dev(executor, config, request_id="second").status == "partial"
    game = executor.state.game_revisions[-1]
    evidence = next(e for e in executor.state.evidence if e.evidence_id in game.build_evidence_ids)
    assert game.affected_requirement_ids == ("R002",)
    assert set(evidence.requirement_ids) == {r.requirement_id for r in executor.state.requirements}
    assert evidence.kind == "build_check" and "not acceptance" in evidence.observation
    assert all(r.verification is None for r in executor.state.requirements)
    assert executor.state.readiness is None


@pytest.mark.parametrize("repair", [True, False])
def test_qa_repairs_bad_citations_once_then_conservatively_abstains(tmp_path, repair):
    def transform(raw, call):
        if call == 1 or not repair:
            raw["criteria"][0].update(verdict="pass", basis="static",
                evidence_ids=["invented-evidence"], source_ids=["project-inventory"])
        return raw
    provider = OfflineText(transform)
    async def run():
        executor, workspace = await setup_executor(tmp_path / "workspace", provider)
        result = await executor.execute(request(executor), QAReviewer(QAConfig(max_grounding_repairs=1)))
        assert result.status == "completed", result.error
        ref = next(r for r in result.artifact_refs if r.artifact_id == "qa-report")
        return QAReport.model_validate_json(workspace.read_public(ref.path).read_bytes())
    document = asyncio.run(run())
    assert len(provider.prompts) == 2
    assert provider.prompts[1]["previous_response"]
    assert provider.prompts[1]["validation_errors"][0]["kind"] == "grounding"
    assert bool(document.grounding_notes) is not repair
    assert document.effective_proposal.criteria[0].verdict == "insufficient"
    assert not document.readiness.ready
    assert "invented-evidence" not in document.readiness.evidence_ids


def test_delivery_retains_a_named_gameplay_capture_alongside_ordinary_entry(tmp_path):
    executor, adapters, providers, _ = make_run(tmp_path)
    first = plan()
    first["trials"][0]["trace"]["scenario"] = None
    second, third = copy.deepcopy(first), copy.deepcopy(first)
    second["trials"][0]["trace_id"] = "another_menu"
    second["trials"][0]["trace"]["events"][0]["frame"] += 1
    third["trials"][0]["trace_id"] = "boundary_scenario"
    third["trials"][0]["trace"]["scenario"] = "near_rewind"
    first["trials"].extend([second["trials"][0], third["trials"][0]])
    providers[Role.PLAYER].replies = [first, FINISH, FINISH]
    asyncio.run(FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=2)).run())
    from adaptive_game_generation.roles.developer import DeveloperReport
    dev = report(executor, "developer-report", DeveloperReport)
    traces = [InputTrace.model_validate_json(executor.workspace.read_public(p.trace_ref.path).read_bytes())
              for p in dev.packaged_traces]
    assert [t.scenario for t in traces] == [None, "near_rewind"]


def test_runner_reserves_repair_and_exploration_capacity():
    runner = runpy.run_path(str(ROOT / "examples/fixed_loop_demo.py"))
    gateways = {role: GatewayConfig(model="offline", api_key="unused", base_url="https://example.test")
                for role in runner["PREFIXES"]}
    configs, _, limits = runner["role_setup"](gateways)
    assert configs[Role.ART_PRODUCER].max_search_turns == 3
    assert configs[Role.ART_PRODUCER].max_model_turns == 6
    assert configs[Role.PLAYER].max_traces == 5
    assert configs[Role.PLAYER].max_model_turns == 5
    assert configs[Role.QA_REVIEWER].max_grounding_repairs == 1
    for role, config in configs.items():
        assert limits[role].max_model_calls == getattr(config, "max_model_turns", 1)
    with pytest.raises(ValueError):
        InputTrace(duration_frames=601)  # The public Benchmark cap remains unchanged.
