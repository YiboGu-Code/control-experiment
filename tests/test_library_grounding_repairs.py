"""Offline contract tests using the real Benchmark instruction; no online calls."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest
from PIL import Image

from adaptive_game_generation.models import ResourceLimits
from adaptive_game_generation.roles.art_producer import ArtProducer, ArtProducerConfig
from adaptive_game_generation.roles.designer import Designer, DesignerConfig
from adaptive_game_generation.roles.designer.grounding import instruction_span
from adaptive_game_generation.runtime import Executor, Services
from adaptive_game_generation.runtime.asset_normalization import png
from adaptive_game_generation.runtime.media import ImageServiceInfo
from test_art_producer import asset, proposal, make_run, invoke, manifest, library_pair, search_turn
from test_sprite_quality import silhouette
from test_designer import make_run as design_run, invoke as design, response, read_document

LAYOUT = {"kind": "sprite", "mode": "whole"}


def export_review(name, executor, text, replies, images=()):
    destination = os.environ.get("LIBRARY_REPAIR_REVIEW_ROOT")
    if not destination:
        return
    target = Path(destination) / name
    target.mkdir(parents=True, exist_ok=False)
    (target / "instruction.md").write_text(executor.state.task.instruction, encoding="utf-8")
    for filename, value in {"state_final.json": executor.state.model_dump(mode="json"),
            "model_io.json": [{"input": p, "output": r} for p, r in zip(text.prompts, replies)],
            "image_jobs.json": list(images),
            "mode.json": {"online_calls": 0, "mode": "Offline scripted model/image replies through real adapters and executor"}}.items():
        (target / filename).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    shutil.copytree(executor.workspace.public, target / "public")
    return target


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_available_libraries_prevent_unjustified_generation_then_import_clean_sprite(tmp_path, controller):
    libraries, roots = library_pair(tmp_path)
    for root in roots.values():
        silhouette().save(root / "pack/player.png")
    selected = asset(source="kenney", source_path="pack/player.png", generation_prompt=None,
                     width=64, height=64, layout=LAYOUT)
    replies = [proposal(), search_turn(), proposal(selected)]
    executor, text, images = make_run(tmp_path / "case", *replies, libraries=libraries, controller=controller)
    result = invoke(executor, ArtProducer(ArtProducerConfig(normalize_assets=True, max_quality_repair_rounds=2)))
    assert result.status == "completed", result.error
    assert not images.calls and executor.state.ledger.usage.image_calls == 0
    assert "Library-first" in str(text.prompts[1]["proposal_rejections"])
    doc = manifest(executor)
    assert doc.assets[0].normalization.status == "materialized" and not doc.assets[0].is_mock
    assert executor.workspace.read_public(doc.assets[0].record.artifact_ref.path).read_bytes() == (roots["kenney"] / "pack/player.png").read_bytes()
    assert [s["search_id"] for s in doc.search_observations] == ["search-001", "search-002"]
    export_review("library-first-" + controller, executor, text, replies)


def fallback():
    return {"reason": "Returned objects do not provide the requested clock device",
        "search_ids": ["search-001", "search-002"], "rejected_candidates": [
            {"library": lib, "path": "pack/player.png", "reason": "This is a character, not the required clock device"}
            for lib in ("kenney", "opengameart")]}


@pytest.mark.parametrize("fault", ["unknown_search", "missing_library", "invented_candidate", "no_review"])
def test_fallback_requires_actual_search_and_candidate_evidence(tmp_path, fault):
    libraries, _ = library_pair(tmp_path)
    reason = fallback()
    if fault == "unknown_search": reason["search_ids"] = ["invented"]
    elif fault == "missing_library": reason["search_ids"] = ["search-001"]
    elif fault == "invented_candidate": reason["rejected_candidates"][0]["path"] = "invented.png"
    else: reason["rejected_candidates"] = []
    executor, _, image = make_run(tmp_path / "case", search_turn(), proposal(asset(generation_fallback=reason)), libraries=libraries)
    result = invoke(executor, ArtProducer(ArtProducerConfig(max_model_turns=2)))
    assert result.status == "failed" and result.error.code == "invalid_submission"
    assert not image.calls and not executor.state.assets


def test_documented_generation_fallback_still_works(tmp_path):
    libraries, _ = library_pair(tmp_path)
    executor, _, image = make_run(tmp_path / "case", search_turn(), proposal(asset(generation_fallback=fallback())), libraries=libraries)
    result = invoke(executor)
    assert result.status == "partial", result.error  # Mock generation never becomes final art.
    assert len(image.calls) == 1 and manifest(executor).assets[0].request.generation_fallback.reason


def test_search_specific_name_outranks_generic_sprite_directory_and_atlas(tmp_path):
    libraries, roots = library_pair(tmp_path)
    root = roots["kenney"]
    (root / "pack/Sprites").mkdir()
    (root / "pack/Sprites/unrelated.png").write_bytes(png(silhouette()))
    (root / "pack/player_atlas.png").write_bytes(png(silhouette()))
    found = asyncio.run(libraries.search("kenney", "player character sprite", limit=20))
    assert found.assets[0].path == "pack/player.png"
    assert "pack/Sprites/unrelated.png" not in [a.path for a in found.assets]
    second = asyncio.run(libraries.search("kenney", "player character sprite", offset=1, limit=1))
    assert second.assets[0].path == "pack/player_atlas.png"


def test_repaired_asset_attempt_paths_integrate_without_leaking_rejected_originals(tmp_path):
    from test_developer import invoke as develop, proposal as code, report as developer_report, ScriptedSandbox, ScriptedText
    from adaptive_game_generation.roles.developer import DeveloperConfig
    first = asset(width=64, height=64, layout=LAYOUT)
    second = {**first, "generation_prompt": "One isolated subject without a panel or background"}
    executor, _, _ = quality_run(tmp_path, [proposal(first), proposal(second)],
        [png(Image.new("RGBA", (64, 64), "red")), png(silhouette())])
    assert invoke(executor, ArtProducer(ArtProducerConfig(normalize_assets=True, max_quality_repair_rounds=1))).status == "completed"
    art = manifest(executor)
    text = ScriptedText(code())
    executor = Executor.open(executor.workspace, services=Services(text=text, sandbox=ScriptedSandbox()))
    result = develop(executor, DeveloperConfig(require_normalized_assets=True))
    assert result.status == "completed", result.error
    game = executor.workspace.read_public(developer_report(executor).project_ref.path)
    frame = art.assets[0].normalization.frames[0]
    assert (game / frame.target_path).read_bytes() == executor.workspace.read_public(frame.artifact_ref.path).read_bytes()
    assert not (game / "art_attempts").exists() and not (game / "assets/player.png").exists()


class QualityImages:
    """Synthetic pixels marked non-mock solely to exercise the real quality gate."""
    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), []
    async def generate(self, prompt, *, timeout):
        self.calls.append(json.loads(prompt))
        return self.outputs.pop(0)


def quality_run(tmp_path, replies, pixels, **kwargs):
    image = QualityImages(*pixels)
    executor, text, _ = make_run(tmp_path, *replies, image=image, **kwargs)
    service = ImageServiceInfo(provider_id="offline-quality-fixture", is_mock=False,
        license="Synthetic offline test image; no model or remote service was used")
    executor = Executor.open(executor.workspace, services=Services(text=text, image=image, image_info=service,
        libraries=executor.services.libraries))
    return executor, text, image


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_quality_feedback_repairs_only_failed_asset_and_preserves_attempts(tmp_path, controller):
    first = asset(width=64, height=64, layout=LAYOUT)
    good = asset("accepted", width=64, height=64, layout=LAYOUT)
    repair = {**first, "generation_prompt": "One isolated silhouette, no background panel, no text or ground"}
    replies = [proposal(first, good), proposal(repair)]
    executor, text, image = quality_run(tmp_path, replies,
        [png(Image.new("RGBA", (64, 64), "red")), png(silhouette()), png(silhouette())], controller=controller)
    result = invoke(executor, ArtProducer(ArtProducerConfig(normalize_assets=True, max_quality_repair_rounds=2)))
    assert result.status == "completed", result.error
    doc = manifest(executor)
    assert len(image.calls) == executor.state.ledger.usage.image_calls == 3
    assert len(text.prompts) == executor.state.ledger.usage.model_calls == 2
    feedback = text.prompts[1]["input"]["quality_feedback"]
    assert [f["asset_id"] for f in feedback] == ["player"] and feedback[0]["normalization"]["quality"]["findings"]
    assert image.calls[0]["prompt"] != image.calls[2]["prompt"]
    assert len(doc.quality_attempts) == 3 and doc.quality_attempts[0]["candidate"]["normalization"]["status"] == "rejected"
    assert all(a.normalization.status == "materialized" for a in doc.assets)
    for ref in result.artifact_refs:
        assert hashlib.sha256(executor.workspace.read_public(ref.path).read_bytes()).hexdigest() == ref.sha256
    assert "HIDDEN-" not in json.dumps(text.prompts) and "SECRET-SENTINEL" not in json.dumps(text.prompts)
    export_review("quality-repair-" + controller, executor, text, replies, image.calls)


def test_repeated_bad_images_end_partial_without_relaxed_quality_or_extra_calls(tmp_path):
    first = asset(width=64, height=64, layout=LAYOUT)
    second = {**first, "generation_prompt": "Remove all panel/background content; render one clean isolated subject"}
    bad = png(Image.new("RGBA", (64, 64), "red"))
    executor, text, image = quality_run(tmp_path, [proposal(first), proposal(second)], [bad, bad])
    result = invoke(executor, ArtProducer(ArtProducerConfig(normalize_assets=True, max_quality_repair_rounds=1)))
    assert result.status == "partial", result.error
    doc = manifest(executor)
    assert len(image.calls) == len(text.prompts) == 2 and not doc.assets[0].normalization.frames
    assert any("allowance exhausted" in line for line in doc.limitations)


@pytest.mark.parametrize("dimension", ["image_calls", "model_calls"])
def test_quality_repair_stops_before_exceeding_shared_call_budget(tmp_path, dimension):
    limits = ResourceLimits(max_model_calls=1 if dimension == "model_calls" else 10,
        max_image_calls=1 if dimension == "image_calls" else 10, max_input_tokens=900000,
        max_output_tokens=100000, max_tool_calls=300)
    first = asset(width=64, height=64, layout=LAYOUT)
    executor, text, image = quality_run(tmp_path, [proposal(first)], [png(Image.new("RGBA", (64, 64), "red"))], budget=limits)
    result = invoke(executor, ArtProducer(ArtProducerConfig(normalize_assets=True, max_quality_repair_rounds=2)))
    assert result.status == "partial", result.error
    assert len(text.prompts) == len(image.calls) == 1 and manifest(executor).assets[0].normalization.status == "rejected"
    assert any("allowance exhausted" in s for s in manifest(executor).limitations)


@pytest.mark.parametrize("fault", ["unchanged", "relabel", "metadata_only"])
def test_quality_retry_cannot_repeat_same_request_or_relabel_sprite(tmp_path, fault):
    first = asset(width=64, height=64, layout=LAYOUT)
    second = copy.deepcopy(first)
    if fault == "relabel": second["layout"]["kind"] = "background"
    if fault == "metadata_only":
        second.update(integration_notes="Use the sprite carefully", purpose="Revised metadata only",
                      target_path="assets/renamed.png", frame_names=["renamed"])
    executor, _, image = quality_run(tmp_path, [proposal(first), proposal(second)], [png(Image.new("RGBA", (64, 64), "red"))])
    result = invoke(executor, ArtProducer(ArtProducerConfig(normalize_assets=True, max_quality_repair_rounds=2, max_model_turns=2)))
    assert result.status == "partial", result.error
    assert len(image.calls) == 1 and manifest(executor).assets[0].normalization.status == "rejected"
    assert manifest(executor).proposal_rejections


def test_grounding_normalization_preserves_words_numbers_controls_and_operators():
    original = "Press `R` to **reset**.\nA *ghost* lives for 30 seconds. Calculate x * 2 using loop_tick."
    assert instruction_span("Press R to reset.", original) in original
    assert instruction_span("A ghost lives for 30\n seconds.", original) in original
    for wrong in ["Press E to reset.", "A ghost lives for 20 seconds.", "Calculate x 2 using loop_tick.", "using looptick"]:
        assert instruction_span(wrong, original) is None
    assert instruction_span("path /foo/bar/file", "path `/foo/*bar*/file`") is None
    assert instruction_span("identifier foo", "identifier `_foo_`") is None


def test_actual_saved_kenney_asset_is_imported_and_cropped_without_generation(tmp_path):
    from adaptive_game_generation.runtime.asset_libraries import MountedAssetLibraries
    from adaptive_game_generation.runtime.workspace import safe_path
    root = Path(__file__).resolve().parents[1] / "runs/fixed-loop-developer-opt-001/workspace/public"
    doc = json.loads((root / "artifacts/art_producer/f0308b8eb711b84113fc1c0c/asset_manifest.json").read_bytes())
    original = next(a for a in doc["assets"] if a["request"]["source"] == "kenney")
    source_bytes = safe_path(root, original["record"]["artifact_ref"]["path"]).read_bytes()
    assert hashlib.sha256(source_bytes).hexdigest() == original["source_sha256"]
    mount = tmp_path / "recorded-library-subset"
    selected = safe_path(mount, original["request"]["source_path"])
    selected.parent.mkdir(parents=True)
    selected.write_bytes(source_bytes)
    license_file = safe_path(mount, original["source_license_path"])
    license_bytes = safe_path(root, original["license_ref"]["path"]).read_bytes()
    assert hashlib.sha256(license_bytes).hexdigest() == original["source_license_sha256"]
    license_file.write_bytes(license_bytes)
    libraries = MountedAssetLibraries(kenney=mount, opengameart=tmp_path / "missing-oga")
    # Use the previously verified public region; no reconstructed/fabricated atlas sidecar.
    request = {**original["request"], "requirement_ids": ["R001"], "layout": original["normalization"]["layout"]}
    search = {"action": "search", "summary": "Browse the saved real Kenney subset",
              "searches": [{"library": "kenney", "query": "onlyObjects"}]}
    replies = [search, proposal(request)]
    executor, text, image = make_run(tmp_path / "case", *replies, libraries=libraries)
    result = invoke(executor, ArtProducer(ArtProducerConfig(normalize_assets=True, max_quality_repair_rounds=2)))
    assert result.status == "completed", result.error
    new = manifest(executor).assets[0]
    assert not image.calls and new.normalization.status == "materialized"
    assert new.source_license_sha256 == original["source_license_sha256"]
    expected = safe_path(root, original["normalization"]["frames"][0]["artifact_ref"]["path"]).read_bytes()
    assert executor.workspace.read_public(new.normalization.frames[0].artifact_ref.path).read_bytes() == expected
    target = export_review("recorded-kenney", executor, text, replies)
    if target:
        (target / "source_provenance.json").write_text(json.dumps({
            "recorded_manifest": str(root / "artifacts/art_producer/f0308b8eb711b84113fc1c0c/asset_manifest.json"),
            "source_sha256": original["source_sha256"], "source_license_sha256": original["source_license_sha256"],
            "expected_frame_sha256": hashlib.sha256(expected).hexdigest(), "image_calls": 0,
            "scope": "Actual saved Kenney atlas, license and region; subset mount only, not the full live library"
        }, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_one_bad_grounding_is_target_repaired_without_losing_valid_requirements(tmp_path, controller):
    original = response()
    invalid = copy.deepcopy(original)
    invalid["requirements"][3]["source_excerpt"] = "This invented phrase is not in the Benchmark instruction."
    correction = {"corrections": [{"requirement_id": original["requirements"][3]["requirement_id"],
                                  "source_excerpt": original["requirements"][3]["source_excerpt"]}]}
    executor, text = design_run(tmp_path, invalid, correction, controller=controller)
    result = design(executor, adapter=Designer(DesignerConfig(max_grounding_repairs=1)))
    assert result.status == "completed", result.error
    assert len(text.prompts) == executor.state.ledger.usage.model_calls == 2
    assert [e["requirement_id"] for e in text.prompts[1]["grounding_errors"]] == [original["requirements"][3]["requirement_id"]]
    assert [r.model_dump(mode="json") for r in read_document(executor).requirements] == [
        type(read_document(executor).requirements[0]).model_validate(r).model_dump(mode="json") for r in original["requirements"]]
    assert any(r.artifact_id == "designer-grounding-audit" for r in result.artifact_refs)
    export_review("grounding-repair-" + controller, executor, text, [invalid, correction])


def test_grounding_repair_obeys_common_model_budget(tmp_path):
    invalid = response()
    invalid["requirements"][0]["source_excerpt"] = "Not in the instruction"
    executor, text = design_run(tmp_path, invalid, budget=ResourceLimits(max_model_calls=1, max_input_tokens=400000, max_output_tokens=100000))
    result = design(executor, adapter=Designer(DesignerConfig(max_grounding_repairs=1)))
    assert result.status == "failed" and result.error.code == "budget_exceeded"
    assert len(text.prompts) == 1 and not executor.state.requirements
