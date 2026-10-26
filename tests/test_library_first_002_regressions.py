"""Saved real-run inputs plus offline transport/contract tests; no paid requests."""
import asyncio
import base64
import copy
import hashlib
import json
import os
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from adaptive_game_generation.models import ArtifactRef, RoleResult
from adaptive_game_generation.providers.openai_compatible import ProviderFailure
from adaptive_game_generation.providers.mock_image import MockImageProvider
from adaptive_game_generation.roles.art_producer import ArtProducer, ArtProducerConfig
from adaptive_game_generation.roles.art_producer.contracts import AssetDraft, ActorMotion
from adaptive_game_generation.roles.art_producer.motion import validate_motion, motion_quality
from adaptive_game_generation.roles.art_producer.prompt import build_prompt as art_prompt
from adaptive_game_generation.roles.developer.animation import animation_resources, animation_integration_findings
from adaptive_game_generation.roles.developer.diagnostics import source_findings, repair_brief
from adaptive_game_generation.roles.qa_reviewer.contracts import QAProposal, QAConfig
from adaptive_game_generation.roles.qa_reviewer.prompt import build_prompt
from adaptive_game_generation.roles.qa_reviewer.repair import repair_prompt, merge_patch
from adaptive_game_generation.runtime import Executor, Services, InvalidSubmission
from adaptive_game_generation.runtime.tools import ModelImage
from adaptive_game_generation.runtime.asset_libraries import MountedAssetLibraries
from test_art_producer import asset, proposal, make_run, invoke, manifest
from test_dashscope_image import provider, config, sdk_response, image_bytes
from test_image_provider import job

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures/fixed_loop_library_first_002"


def saved(name):
    return json.loads((FIXTURE / name).read_bytes())


def test_real_qa_repairs_only_two_rows_and_preserves_all_other_decisions():
    inputs, original, notes = saved("qa_input.json"), QAProposal.model_validate(saved("qa_original_proposal.json")), saved("qa_errors.json")
    before = build_prompt(inputs, QAConfig(), [], original.model_dump_json())
    prompt = repair_prompt(inputs, original, notes)
    assert len(prompt.encode()) < len(before.encode()) * 0.4
    assert len(json.loads(prompt)["criteria"]) == 2
    keys = {(n["requirement_id"], n["criterion_index"]) for n in notes}
    patch = {"criteria": [c.revised(verdict="insufficient", rationale="Collect correctly mapped evidence before acceptance.",
        evidence_ids=(), source_ids=()).model_dump(mode="json") for c in original.criteria if (c.requirement_id, c.criterion_index) in keys]}
    result = merge_patch(original, patch, notes)
    assert [c for c in result.criteria if (c.requirement_id, c.criterion_index) not in keys] == [c for c in original.criteria if (c.requirement_id, c.criterion_index) not in keys]
    assert result.issues == original.issues and result.ready == original.ready
    invalid = copy.deepcopy(patch)
    invalid["criteria"].append(next(c.model_dump(mode="json") for c in original.criteria if (c.requirement_id, c.criterion_index) not in keys))
    with pytest.raises(InvalidSubmission, match="out-of-scope"):
        merge_patch(original, invalid, notes)
    with pytest.raises(InvalidSubmission, match="duplicate"):
        merge_patch(original, {"criteria": patch["criteria"] * 2}, notes)
    for malformed in (None, [], "bad"):
        with pytest.raises(InvalidSubmission, match="JSON object"):
            merge_patch(original, malformed, notes)
    unavailable = copy.deepcopy(patch)
    unavailable["criteria"][0]["source_ids"] = ["not-supplied"]
    with pytest.raises(InvalidSubmission, match="not supplied"):
        merge_patch(original, unavailable, notes, allowed_sources={"instruction"})
    if destination := os.environ.get("ART_REVIEW_OUTPUT"):
        out = Path(destination) / "qa-scoped-repair"
        out.mkdir(parents=True, exist_ok=True)
        (out / "input.json").write_text(prompt, encoding="utf-8")
        (out / "offline_patch.json").write_text(json.dumps(patch, indent=2), encoding="utf-8")
        (out / "merged_proposal.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        (out / "comparison.json").write_text(json.dumps({"old_text_bytes": len(before.encode()),
            "new_text_bytes": len(prompt.encode()), "changed_criteria": len(keys),
            "unchanged_criteria": len(original.criteria) - len(keys), "online_model_called": False}, indent=2), encoding="utf-8")


def test_real_sources_reveal_frozen_flash_static_actor_and_resource_kill():
    sources = saved("sources.json")
    found = source_findings(sources)
    assert "effect_timer_without_decay" in {f["code"] for f in found}
    assert "actor_motion_unproven" in {f["code"] for f in found}
    brief = repair_brief(sources, (), {"build_attempts": [saved("killed_build.json")]})
    assert any(d.get("code") == "engine_process_killed" for d in brief["diagnostics"])
    assert "not proven" in next(d["message"] for d in brief["diagnostics"] if d.get("code") == "engine_process_killed")


def test_native_reference_payload_contains_exact_pixels_and_preserves_sdk(tmp_path):
    seen = []
    async def call(**kwargs):
        seen.append(kwargs)
        return sdk_response()
    selected = config().revised(model="qwen-image-3.0-pro")
    service = provider(tmp_path, call, settings=selected)
    raw = image_bytes()
    async def run():
        async with service:
            return await service.generate(job().model_dump_json(), timeout=20,
                images=(ModelImage("anchor", raw, "image/png"),))
    assert asyncio.run(run())
    assert service.info.supports_reference_images
    content = seen[0]["messages"][0]["content"]
    assert base64.b64decode(content[0]["image"].split(",")[1]) == raw
    assert "text" in content[1] and seen[0]["model"] == "qwen-image-3.0-pro"
    assert service.calls[0]["references"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert not selected.revised(reference_images=False).info.supports_reference_images
    assert not selected.revised(model="wan2.7-image").info.supports_reference_images


class ReferenceImage(MockImageProvider):
    def __init__(self):
        super().__init__()
        self.references = []

    async def generate(self, prompt, *, timeout, images=()):
        self.references.append(images)
        return await super().generate(prompt, timeout=timeout)


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_shared_executor_passes_only_verified_public_anchor_bytes(tmp_path, controller):
    image = ReferenceImage()
    executor, text, _ = make_run(tmp_path, proposal(asset("anchor"), asset("running", reference_asset_ids=["anchor"])),
                               image=image, controller=controller)
    executor = Executor.open(executor.workspace, services=Services(text=text, image=image,
        image_info=executor.services.image_info.revised(supports_reference_images=True), libraries=executor.services.libraries))
    outcome = invoke(executor)
    assert outcome.status == "partial", outcome
    assert len(image.references) == 2 and not image.references[0] and len(image.references[1]) == 1
    anchor = manifest(executor).assets[0].record.artifact_ref
    assert hashlib.sha256(image.references[1][0].data).hexdigest() == anchor.sha256
    assert executor.state.ledger.usage.image_calls == 2 and executor.state.ledger.usage.token_usage_complete
    if destination := os.environ.get("ART_REVIEW_OUTPUT"):
        out = Path(destination) / (controller + "-reference-protocol")
        out.mkdir(parents=True, exist_ok=True)
        (out / "input.json").write_text(json.dumps(text.prompts, indent=2), encoding="utf-8")
        (out / "model_output.json").write_text(json.dumps(proposal(asset("anchor"), asset("running", reference_asset_ids=["anchor"])), indent=2), encoding="utf-8")
        (out / "manifest.json").write_text(manifest(executor).model_dump_json(indent=2), encoding="utf-8")
        (out / "result.json").write_text(outcome.model_dump_json(indent=2), encoding="utf-8")
        (out / "fixture.json").write_text(json.dumps({"mode": "offline scripted protocol; image output is mock", "live_model_called": False,
            "usage": executor.state.ledger.usage.model_dump(mode="json")}, indent=2), encoding="utf-8")


def test_bad_reference_hash_is_denied_before_a_paid_image_request(tmp_path):
    image = ReferenceImage()
    executor, text, _ = make_run(tmp_path, image=image)
    executor = Executor.open(executor.workspace, services=Services(text=text, image=image,
        image_info=executor.services.image_info.revised(supports_reference_images=True), libraries=executor.services.libraries))
    async def run(context):
        path = await context.tools.write_bytes("anchor.png", image_bytes())
        ref = ArtifactRef(artifact_id="anchor", kind="asset", path=path, created_by="art_producer", sha256="0" * 64)
        await context.tools.generate_image(job().model_dump_json(), "result.png", image_refs=(ref,))
    result = invoke(executor, run)
    assert result.status == "failed" and not image.references
    assert executor.state.ledger.usage.image_calls == 0


def test_image_transport_failure_retains_completed_assets_and_lists_deferred_work(tmp_path):
    class FailingImage(MockImageProvider):
        async def generate(self, prompt, *, timeout):
            if self.calls:
                raise ProviderFailure("Image request timed out; no automatic retry was made")
            return await super().generate(prompt, timeout=timeout)
    executor, text, image = make_run(tmp_path, proposal(asset("a"), asset("b"), asset("c")), image=FailingImage())
    result = invoke(executor)
    report = manifest(executor)
    assert result.status == "partial" and [a.record.asset_id for a in report.assets] == ["a"]
    assert [(f["asset_id"], f["status"]) for f in report.production_failures] == [("b", "provider_failed"), ("c", "not_attempted")]
    assert executor.state.ledger.usage.image_calls == 2 and executor.state.ledger.usage.token_usage_complete


def test_actor_contract_requires_motion_and_rejects_unknown_frames():
    actor = AssetDraft.model_validate(asset(layout={"kind": "sprite", "mode": "whole"}))
    with pytest.raises(InvalidSubmission, match="motion contract"):
        validate_motion((actor,), (), required=True)
    motion = ActorMotion(mode="frames", display_height=68,
        clips=({"name": "run", "frames": ("player/idle", "run/pose")},), implementation="Use fixed simulation ticks.")
    actor = actor.revised(motion=motion)
    with pytest.raises(InvalidSubmission, match="unknown animation"):
        validate_motion((actor,), (), required=True)
    other = AssetDraft.model_validate(asset("run", frame_names=["pose"], layout={"kind": "sprite", "mode": "whole"}))
    validate_motion((actor, other), (), required=True)


def animation_fixture(duplicate=False):
    motion = ActorMotion(mode="frames", display_height=68,
        clips=({"name": "run", "frames": ["player/a", "player/b"]},), implementation="Use simulation tick / physics rate.")
    frames = [SimpleNamespace(region=SimpleNamespace(name=n, width=64, height=64, pivot_x=0.5, pivot_y=1.0),
        target_path=f"assets/normalized/player/{n}.png", artifact_ref=SimpleNamespace(sha256="a" if duplicate else n)) for n in ("a", "b")]
    return SimpleNamespace(assets=[SimpleNamespace(request=SimpleNamespace(asset_id="player", motion=motion),
                                                  normalization=SimpleNamespace(frames=frames))])


def test_host_exports_animation_resource_and_rejects_idle_only_integration():
    files, contracts = animation_resources(animation_fixture())
    assert b'SpriteFrames' in files["assets/animations/player.tres"]
    assert b'assets/normalized/player/b.png' in files["assets/animations/player.tres"]
    assert animation_integration_findings({"player.gd": 'load("res://assets/normalized/player/a.png")'}, contracts)
    assert animation_integration_findings({"player.gd": '# preload("res://assets/animations/player.tres")'}, contracts)
    assert not animation_integration_findings({"player.gd": 'preload("res://assets/animations/player.tres")'}, contracts)
    assert motion_quality(animation_fixture(True).assets)[0]["code"] == "duplicate_animation_frames"
    files, contracts = animation_resources(animation_fixture(True))
    assert "assets/animations/player.tres" not in files and contracts[0]["status"] == "pending"


def test_on_demand_art_skills_are_bounded_and_not_loaded_by_default(tmp_path):
    config = ArtProducerConfig(progressive_skills=True, normalize_assets=True)
    first = json.loads(art_prompt({}, config, [], 4))
    loaded = json.loads(art_prompt({"loaded_skills": ["actor_motion"]}, config, [], 3))
    assert first["loaded_skill_bodies"] == {} and "asset_layout" in first["skill_catalog"]
    assert set(loaded["loaded_skill_bodies"]) == {"actor_motion"}
    executor, text, _ = make_run(tmp_path, {"action": "load_skills", "skills": ["actor_motion", "reference_edit"],
        "summary": "Load motion and reference guidance before planning."}, proposal())
    result = invoke(executor, ArtProducer(ArtProducerConfig(progressive_skills=True)))
    assert result.status == "partial" and executor.state.ledger.usage.model_calls == 2
    assert set(text.prompts[1]["loaded_skill_bodies"]) == {"actor_motion", "reference_edit"}


def test_library_preflight_reports_paths_and_requires_explicit_generation_only_choice(tmp_path):
    runner = runpy.run_path(str(ROOT / "examples/fixed_loop_demo.py"))
    mounts = MountedAssetLibraries(kenney=tmp_path / "kenney", opengameart=tmp_path / "oga")
    check = asyncio.run(runner["library_preflight"](mounts, {}))
    assert not check["passed"] and check["warnings"] and check["mounts"][0]["root"]
    assert asyncio.run(runner["library_preflight"](mounts, {"ART_LIBRARY_POLICY": "optional"}))["passed"]
    (tmp_path / "kenney").mkdir()
    assert asyncio.run(runner["library_preflight"](mounts, {}))["passed"]
    assert not asyncio.run(runner["library_preflight"](mounts, {"ART_LIBRARY_POLICY": "both"}))["passed"]


def test_reviewed_presentation_patch_preserves_source_and_uses_physics_delta(monkeypatch):
    import sys
    monkeypatch.syspath_prepend(str(ROOT / "examples"))
    module = runpy.run_path(str(ROOT / "examples/replay_presentation_repair.py"))
    before = saved("sources.json")
    after = module["patch_sources"](before)
    assert "flash_timer - delta" in after["scripts/level.gd"]
    assert not any(f["code"] == "effect_timer_without_decay" for f in source_findings(after))
    assert '"Chapter %d" % chapter' in after["scripts/main.gd"]
    assert "Vector2(0.25, 0.25)" not in after["scripts/player_actor.gd"]
    assert "g.visible = false" in after["scripts/level.gd"]
    assert "flash_timer - delta" not in before["scripts/level.gd"]
    with pytest.raises(ValueError, match="no longer matches"):
        module["patch_sources"](after)


def test_actor_frames_flow_through_art_and_developer_host_assembly(tmp_path):
    from PIL import Image, ImageDraw
    import io
    from adaptive_game_generation.roles.developer import Developer, DeveloperConfig
    from adaptive_game_generation.roles.player import Player, PlayerConfig
    from test_developer import proposal as code_proposal, ScriptedSandbox, report as dev_report
    class PoseFixture(MockImageProvider):
        async def generate(self, prompt, *, timeout):
            canvas = Image.new("RGBA", (64, 32))
            draw = ImageDraw.Draw(canvas)
            draw.rectangle((8, 4, 23, 29), fill=(30, 90, 160, 255))
            draw.rectangle((40, 5, 55, 27), fill=(30, 120, 160, 255))
            output = io.BytesIO()
            canvas.save(output, format="PNG")
            return output.getvalue()
    animation = {"mode": "frames", "display_height": 68, "clips": [{"name": "run", "frames": ["player/a", "player/b"]}],
                 "implementation": "Select frame from fixed simulation ticks; inspect motion during actual play."}
    draft = asset(width=64, height=32, columns=2, rows=1, frame_names=["a", "b"], motion=animation,
                  layout={"kind": "sprite_sheet", "mode": "grid", "cell_width": 32, "cell_height": 32})
    code = code_proposal(script='extends Node2D\nvar poses = preload("res://assets/animations/player.tres")\n')
    executor, text, image = make_run(tmp_path, proposal(draft), code, image=PoseFixture())
    result = invoke(executor, ArtProducer(ArtProducerConfig(normalize_assets=True, require_actor_motion=True)))
    assert result.status == "partial", result
    executor = Executor.open(executor.workspace, services=Services(text=text, image=image,
        image_info=executor.services.image_info, libraries=executor.services.libraries, sandbox=ScriptedSandbox()))
    request = executor.request("developer", "Integrate the Art motion package", ("Keep verified asset bytes",))
    result = asyncio.run(executor.execute(request, Developer(DeveloperConfig())))
    assert result.status == "partial", result
    document = dev_report(executor)
    assert document.motion_contracts[0]["resource_path"] == "assets/animations/player.tres"
    game = executor.state.game_revisions[-1]
    root = executor.workspace.read_public(game.project_ref.path)
    assert (root / "assets/animations/player.tres").exists()
    assert not (root / "assets/player.png").exists()
    assert (root / "assets/normalized/player/a.png").exists()
    observed = {}
    async def inspect(context):
        data, *_ = await Player(PlayerConfig())._inputs(context, game)
        observed.update(data)
        return RoleResult(role="player", request_id=context.request.request_id, state_revision=context.request.state_revision,
                          status="no_op", summary="Offline motion handoff inspection")
    result = asyncio.run(executor.execute(executor.request("player", "Inspect motion handoff", ("Read contracts",)), inspect))
    assert result.status == "no_op", result
    assert observed["developer"]["motion_contracts"] == document.motion_contracts
    if destination := os.environ.get("ART_REVIEW_OUTPUT"):
        out = Path(destination) / "actor-motion-protocol"
        out.mkdir(parents=True, exist_ok=True)
        for name, value in (("model_inputs.json", text.prompts), ("scripted_model_outputs.json", [proposal(draft), code]),
                            ("player_input.json", observed), ("developer_report.json", document.model_dump(mode="json")),
                            ("art_manifest.json", manifest(executor).model_dump(mode="json"))):
            (out / name).write_text(json.dumps(value, indent=2), encoding="utf-8")
        (out / "player.tres").write_bytes((root / "assets/animations/player.tres").read_bytes())
        (out / "fixture.json").write_text(json.dumps({"mode": "offline contract fixture", "model_outputs": "scripted",
            "images": "mock", "engine": "scripted; no Godot execution", "live_game_quality_verified": False}, indent=2), encoding="utf-8")
