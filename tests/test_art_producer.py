"""Offline failure/contract tests. Live integration never uses these scripted replies."""

import asyncio
import copy
import hashlib
import json
import runpy
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from adaptive_game_generation.models import ResourceLimits, RoleResult
from adaptive_game_generation.providers.mock_image import MockImageProvider
from adaptive_game_generation.roles.art_producer import ArtManifest, ArtProducer, ArtProducerConfig
from adaptive_game_generation.runtime import Executor, RuntimeConfig, Services, TextReply, Workspace
from adaptive_game_generation.runtime.asset_libraries import MountedAssetLibraries

ROOT = Path(__file__).resolve().parents[1]
DEMO = runpy.run_path(str(ROOT / "examples/art_producer_demo.py"))


@pytest.mark.parametrize("path", ["sprites/player.png", "tiles/terrain.png", "res://assets/player.png", "/workspace/game/assets/player.png"])
def test_output_schema_rejects_paths_outside_assets_namespace(path):
    from adaptive_game_generation.roles.art_producer.contracts import AssetDraft
    with pytest.raises(ValidationError):
        AssetDraft.model_validate(asset(target_path=path))
    assert AssetDraft.model_json_schema()["properties"]["target_path"]["pattern"] == "^assets/"
    assert AssetDraft.model_validate(asset(target_path="assets/sprites/player.png")).target_path == "assets/sprites/player.png"


class ScriptedText:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def input_token_upper_bound(self, prompt):
        return len(prompt.encode()) + 64

    async def generate(self, prompt, *, max_output_tokens, timeout):
        self.prompts.append(json.loads(prompt))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return TextReply(reply if isinstance(reply, str) else json.dumps(reply), 1000, 2000)


def asset(name="player", **changes):
    return {"asset_id": name, "source": "generate", "generation_prompt": "A readable test platformer sprite",
        "purpose": "Player and translucent ghost silhouettes", "requirement_ids": ["R001"],
        "target_path": f"assets/{name}.png", "width": 32, "height": 32,
        "transparent_background": True, "frame_names": ["idle"],
        "integration_notes": "Use nearest filtering; tint ghosts with reduced opacity in Developer code.",
        "affected_scenes": ["Gameplay"], "license_handling": "Preserve the host-provided source and license record.", **changes}


def proposal(*assets):
    return {"action": "submit", "status": "completed", "summary": "Create the requested reusable sprites",
            "style_guide": "Readable cool colors, strong silhouettes, consistent pixel scale.", "assets": list(assets or [asset()])}


def make_run(tmp_path, *replies, controller="fixed_loop", libraries=None, image=None, budget=None, seed=True):
    source, _, instruction, prior_state, design = DEMO["load_inputs"]()
    task_root = tmp_path / "task"
    task_root.mkdir(parents=True)
    (task_root / "instruction.md").write_bytes(instruction)
    (task_root / "tests").mkdir()
    (task_root / "tests/rubric.json").write_text("HIDDEN-RUBRIC-SENTINEL")
    (task_root / ".env").write_text("API_KEY=SECRET-SENTINEL")
    workspace, task = Workspace.prepare(tmp_path / "run", task_root, task_id=source["task_id"])
    text, image = ScriptedText(*replies), image or MockImageProvider()
    libraries = libraries or MountedAssetLibraries(kenney=tmp_path / "missing-kenney", opengameart=tmp_path / "missing-oga")
    limits = budget or ResourceLimits(max_model_calls=10, max_input_tokens=900000, max_output_tokens=100000,
                                     max_image_calls=20, max_tool_calls=300)
    executor = Executor.create(workspace, task, run_id="art-test", controller=controller,
        budget=limits, config=RuntimeConfig(role_limits=limits),
        services=Services(text=text, image=image, image_info=MockImageProvider.info, libraries=libraries))
    if seed:
        asyncio.run(DEMO["seed_recorded_designer"](executor, prior_state, design))
    return executor, text, image


def invoke(executor, adapter=None, **kwargs):
    request = executor.request("art_producer", "Produce the requested Time Loop assets", ("Traceable sources and exact frame layout",), **kwargs)
    return asyncio.run(executor.execute(request, adapter or ArtProducer()))


def manifest(executor):
    ref = max((r for r in executor.state.artifacts if r.artifact_id == "art-manifest"), key=lambda r: r.version)
    return ArtManifest.model_validate_json(executor.workspace.read_public(ref.path).read_bytes())


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_art_contract_original_task_mock_images_and_isolation(tmp_path, controller):
    executor, text, image = make_run(tmp_path, proposal(), controller=controller)
    before = executor.state
    outcome = invoke(executor)
    assert outcome.status == "partial", outcome.error
    after, document = executor.state, manifest(executor)
    assert after.state_revision == 2 and document.assets[0].is_mock
    assert after.requirements == before.requirements and after.design == before.design
    assert not after.game_revisions and not after.evidence and after.readiness is None
    assert after.ledger.usage.model_calls == 1 and after.ledger.usage.image_calls == 1
    assert len(image.calls) == 1 and document.assets[0].visual_review == "not_performed"
    prompt = text.prompts[0]
    assert prompt["input"]["task"]["instruction"] == before.task.instruction
    assert executor.workspace.read_public("input/instruction.md").read_bytes() == before.task.instruction.encode("utf-8")
    assert "HIDDEN-" not in json.dumps(prompt) and "SECRET-SENTINEL" not in json.dumps(prompt)
    assert "ledger" not in prompt["input"] and "controller" not in prompt["input"]
    assert len(prompt["input"]["sources"]["libraries"]) == 2
    assert not any(s["available"] for s in prompt["input"]["sources"]["libraries"])
    assert Executor.open(executor.workspace, services=executor.services).state == after
    for ref in outcome.artifact_refs:
        assert hashlib.sha256(executor.workspace.read_public(ref.path).read_bytes()).hexdigest() == ref.sha256
    assert (document.assets[0].width, document.assets[0].height, document.assets[0].has_alpha) == (32, 32, True)


def test_identical_prompts_for_both_controllers(tmp_path):
    prompts = []
    for controller in ("fixed_loop", "adaptive_scheduler"):
        executor, text, _ = make_run(tmp_path / controller, proposal(), controller=controller)
        # Stable precondition paths and fixed request objectives produce identical contexts.
        invoke(executor, request_id="art-initial")
        prompts.append(text.prompts)
    assert prompts[0] == prompts[1]


def library_pair(tmp_path):
    roots = {}
    for name in ("kenney", "opengameart"):
        root = tmp_path / name
        (root / "pack").mkdir(parents=True)
        Image.new("RGBA", (32, 32), (255, 0, 0, 128)).save(root / "pack/player.png")
        (root / "pack/LICENSE.txt").write_text("CC0 test fixture" if name == "kenney" else "Synthetic fixture license: retain attribution to Test Artist.", encoding="utf-8")
        roots[name] = root
    return MountedAssetLibraries(**roots), roots


def library_plan():
    return proposal(*[asset(name, source=name, source_path="pack/player.png", generation_prompt=None) for name in ("kenney", "opengameart")])


def search_turn():
    return {"action": "search", "summary": "Browse both permitted mounts", "searches": [
        {"library": "kenney", "query": "player"}, {"library": "opengameart", "query": "player"}]}


def test_both_real_filesystem_library_interfaces_license_copy_and_no_image_calls(tmp_path):
    libraries, roots = library_pair(tmp_path)
    executor, text, image = make_run(tmp_path / "case", search_turn(), library_plan(), libraries=libraries)
    outcome = invoke(executor)
    assert outcome.status == "completed", outcome.error
    assert not image.calls and executor.state.ledger.usage.image_calls == 0
    assert len(text.prompts) == 2
    for entry in manifest(executor).assets:
        source = roots[entry.request.source] / entry.request.source_path
        assert executor.workspace.read_public(entry.record.artifact_ref.path).read_bytes() == source.read_bytes()
        assert entry.record.license == (source.parent / "LICENSE.txt").read_text(encoding="utf-8")
        assert executor.workspace.read_public(entry.license_ref.path).read_text(encoding="utf-8") == entry.record.license
        assert not entry.is_mock and entry.source_sha256 == entry.record.artifact_ref.sha256


@pytest.mark.parametrize("mutation", ["unknown_requirement", "traversal", "duplicate", "path_prefix_collision", "library_not_searched", "grid", "script_target", "forged_mock", "missing_license_handling", "nonfinite", "duplicate_json_key"])
def test_invalid_plan_never_spends_image_calls_or_commits(tmp_path, mutation):
    value = proposal()
    if mutation == "unknown_requirement":
        value["assets"].append(asset("door", requirement_ids=["UNKNOWN"]))
    elif mutation == "traversal":
        value["assets"][0]["target_path"] = "assets/../outside.png"
    elif mutation == "duplicate":
        value["assets"].append(copy.deepcopy(value["assets"][0]))
    elif mutation == "path_prefix_collision":
        value["assets"].append(asset("door", target_path="assets/player.png/door.png"))
    elif mutation == "library_not_searched":
        value["assets"][0].update(source="opengameart", source_path="pack/player.png", generation_prompt=None)
    elif mutation == "grid":
        value["assets"][0].update(columns=3, frame_names=["a", "b", "c"])
    elif mutation == "script_target":
        value["assets"][0]["target_path"] = "assets/player.gd"
    elif mutation == "forged_mock":
        value["assets"][0]["is_mock"] = False
    elif mutation == "missing_license_handling":
        del value["assets"][0]["license_handling"]
    elif mutation == "nonfinite":
        value = json.dumps(value).replace('"width": 32', '"width": NaN')
    else:
        value = '{"action":"submit","action":"search"}'
    executor, _, image = make_run(tmp_path, value)
    before = executor.state
    result = invoke(executor, ArtProducer(ArtProducerConfig(max_model_turns=1)))
    assert result.status == "failed" and not image.calls
    assert executor.state.state_revision == before.state_revision and not executor.state.assets
    assert executor.state.ledger.usage.model_calls == 1
    assert not list((executor.workspace.public / "artifacts/art_producer").glob("*"))


def test_incremental_versions_keep_omitted_assets_and_reject_target_collision(tmp_path):
    adapter = ArtProducer(ArtProducerConfig(max_model_turns=1))
    executor, _, _ = make_run(tmp_path, proposal(asset(), asset("door")),
        proposal(asset(generation_prompt="Revised silhouette")), proposal(asset("other", target_path="assets/door.png")))
    assert invoke(executor, adapter).status == "partial"
    initial = manifest(executor)
    assert invoke(executor, adapter).status == "partial"
    revised = manifest(executor)
    assert revised.version == 2
    assert revised.assets[0].record.artifact_ref.version == 2
    assert revised.assets[1] == initial.assets[1]
    result = invoke(executor, adapter)
    assert result.status == "failed" and "collide" in result.error.message
    assert executor.state.state_revision == 3


@pytest.mark.parametrize("cap", ["image", "model", "tools"])
def test_budget_enforcement_prevents_unaccounted_work(tmp_path, cap):
    limits = ResourceLimits(max_model_calls=0 if cap == "model" else 3, max_input_tokens=900000,
        max_output_tokens=100000, max_image_calls=0 if cap == "image" else 3,
        max_tool_calls=2 if cap == "tools" else 100)
    executor, text, image = make_run(tmp_path, proposal(), budget=limits)
    result = invoke(executor)
    assert result.status == "failed" and result.error.code == "budget_exceeded"
    assert not image.calls and not executor.state.assets and executor.state.state_revision == 1
    if cap in ("model", "tools"):
        assert not text.prompts


def test_no_design_blocks_without_model_or_images(tmp_path):
    executor, text, image = make_run(tmp_path, seed=False)
    assert invoke(executor).status == "blocked"
    assert not text.prompts and not image.calls


def test_failed_real_reply_shape_is_rejected_as_a_regression(tmp_path):
    # Preserve the first real failure as evidence; validate the cause using a minimal fixture.
    value = proposal()
    value["assets"][0]["generation_notes"] = "Unknown extra field must not be accepted"
    executor, _, image = make_run(tmp_path, value)
    assert invoke(executor, ArtProducer(ArtProducerConfig(max_model_turns=1))).status == "failed" and not image.calls


def test_context_and_asset_count_bounds(tmp_path):
    executor, text, _ = make_run(tmp_path / "context", proposal())
    assert invoke(executor, ArtProducer(ArtProducerConfig(max_prompt_bytes=100))).status == "failed"
    assert not text.prompts
    executor, _, image = make_run(tmp_path / "count", proposal(asset(), asset("door")))
    assert invoke(executor, ArtProducer(ArtProducerConfig(max_assets=1, max_model_turns=1))).status == "failed"
    assert not image.calls


def test_provider_failure_keeps_conservative_usage_and_never_retries(tmp_path):
    executor, text, image = make_run(tmp_path, RuntimeError("SECRET-PROVIDER-DETAIL"))
    outcome = invoke(executor)
    assert outcome.status == "failed" and len(text.prompts) == 1 and not image.calls
    assert "SECRET-PROVIDER-DETAIL" not in outcome.model_dump_json()
    assert not executor.state.ledger.usage.token_usage_complete


def test_selected_feedback_is_forwarded_without_closing_issues(tmp_path):
    from adaptive_game_generation.models import IssueRecord, ProposedUpdates
    executor, text, _ = make_run(tmp_path, proposal())
    async def add_issue(context):
        return RoleResult(request_id=context.request.request_id, role="qa_reviewer", state_revision=context.request.state_revision,
            status="completed", summary="Request a visual revision", proposed_updates=ProposedUpdates(issues=(
                IssueRecord(issue_id="I-visual", issue_type="visual", severity="minor", description="Improve ghost contrast",
                            suggested_role="art_producer", requirement_ids=("R002",)),)))
    request = executor.request("qa_reviewer", "Record visual feedback", ("Track the open issue",))
    assert asyncio.run(executor.execute(request, add_issue)).status == "completed"
    assert invoke(executor, work_item_ids=("I-visual",)).status == "partial"
    assert text.prompts[0]["input"]["issues"][0]["issue_id"] == "I-visual"
    assert executor.state.issues[0].status == "open"


def test_no_op_requires_prior_assets_and_preserves_state(tmp_path):
    no_op = {"action": "submit", "status": "no_op", "summary": "Existing assets cover the request"}
    executor, _, _ = make_run(tmp_path, no_op, proposal(), no_op)
    assert invoke(executor, ArtProducer(ArtProducerConfig(max_model_turns=1))).status == "failed"
    assert invoke(executor).status == "partial"
    before = executor.state
    assert invoke(executor).status == "no_op"
    assert executor.state.state_revision == before.state_revision
    assert executor.state.assets == before.assets


def test_search_turn_limit_and_generation_disabled(tmp_path):
    executor, _, image = make_run(tmp_path / "a", search_turn())
    result = invoke(executor, ArtProducer(ArtProducerConfig(max_model_turns=1)))
    assert result.status == "blocked" and not image.calls
    executor, _, image = make_run(tmp_path / "b", proposal())
    result = invoke(executor, ArtProducer(ArtProducerConfig(allow_generation=False, max_model_turns=1)))
    assert result.status == "failed" and not image.calls


@pytest.mark.parametrize("failure", ["invalid", "wrong_size", "provider_error", "timeout"])
def test_failed_image_is_charged_and_artifacts_are_not_published(tmp_path, failure):
    class BrokenImage(MockImageProvider):
        async def generate(self, prompt, *, timeout):
            if failure == "invalid":
                return b"not a PNG"
            if failure == "provider_error":
                raise RuntimeError("SECRET-PROVIDER-DETAIL")
            if failure == "timeout":
                raise asyncio.TimeoutError()
            job = json.loads(prompt)
            job["width"] = 16
            return await super().generate(json.dumps(job), timeout=timeout)
    executor, _, _ = make_run(tmp_path, proposal(), image=BrokenImage())
    outcome = invoke(executor)
    assert outcome.status == "failed"
    assert "SECRET-PROVIDER-DETAIL" not in outcome.model_dump_json()
    assert executor.state.ledger.usage.image_calls == 1 and not executor.state.assets


@pytest.mark.parametrize("role", ["designer", "developer", "player", "qa_reviewer"])
def test_library_capability_is_art_only(tmp_path, role):
    executor, _, _ = make_run(tmp_path)
    async def adapter(context):
        await context.tools.asset_sources()
        return RoleResult(request_id=context.request.request_id, role=role,
            state_revision=context.request.state_revision, status="no_op", summary="No changes")
    request = executor.request(role, "Try asset-library access", ("Access should be denied",))
    outcome = asyncio.run(executor.execute(request, adapter))
    assert outcome.status == "failed" and outcome.error.code == "permission_denied"
