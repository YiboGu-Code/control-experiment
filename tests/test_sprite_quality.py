"""Offline geometry, matte, quality quarantine and public-role integration checks."""
import asyncio
import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo

from adaptive_game_generation.models import Role
from adaptive_game_generation.models.asset_pipeline import AssetLayout
from adaptive_game_generation.providers.openai_image import OpenAICompatibleImageProvider
from adaptive_game_generation.providers.image_prompts import subject_instructions
from adaptive_game_generation.roles.art_producer import ArtProducer, ArtProducerConfig, ImageJob
from adaptive_game_generation.roles.developer import DeveloperConfig
from adaptive_game_generation.roles.developer.assets import asset_package_findings
from adaptive_game_generation.runtime.asset_normalization import generated_layout, normalize_pixels, png
from adaptive_game_generation.runtime.image_processing import (
    PROCESSING_KEY, contained, prepare_generated, remove_matte, sprite_findings,
)
from adaptive_game_generation.runtime.media import ImageServiceInfo

FIXTURES = Path(__file__).parent / "fixtures/sprite_quality"
WHOLE = AssetLayout(kind="sprite", mode="whole")
SETTINGS = SimpleNamespace(transparency="chroma_key", chroma_tolerance=40, resample="nearest")


def silhouette(size=(64, 64), background=(0, 0, 0, 0), color=(20, 180, 220, 255)):
    image = Image.new("RGBA", size, background)
    draw = ImageDraw.Draw(image)
    draw.ellipse((25, 6, 38, 19), fill=color)
    draw.polygon([(27, 19), (36, 19), (45, 35), (37, 32), (37, 43), (43, 56),
                  (35, 56), (31, 44), (27, 56), (19, 56), (25, 42), (25, 30), (18, 35)], fill=color)
    return image


def recorded(name):
    job = ImageJob.model_validate_json((FIXTURES / (name + ".job.json")).read_text(encoding="utf-8"))
    provider = SimpleNamespace(config=SETTINGS)
    data, report = OpenAICompatibleImageProvider._normalize(provider, (FIXTURES / (name + ".png")).read_bytes(), job)
    layout = AssetLayout(kind="sprite_sheet" if job.columns * job.rows > 1 else "sprite", mode="grid",
                         cell_width=job.width // job.columns, cell_height=job.height // job.rows)
    return data, report, job, layout


@pytest.mark.parametrize("size", [(32, 32), (128, 32), (32, 128)])
@pytest.mark.parametrize("resample", ["nearest", "lanczos"])
def test_contain_preserves_geometry_and_never_squares_alpha(size, resample):
    original = Image.new("RGBA", (40, 80), (10, 80, 200, 128))
    output, transform = contained(original, size, resample=resample)
    bounds = output.getchannel("A").getbbox()
    assert output.size == size
    assert abs((bounds[2] - bounds[0]) / (bounds[3] - bounds[1]) - .5) < .04
    assert output.getchannel("A").getextrema()[1] == 128
    assert transform["scale"] == min(size[0] / 40, size[1] / 80)


def test_square_generated_sheet_fits_each_pose_without_squashing_or_animation_jitter():
    image = Image.new("RGBA", (256, 256), "magenta")
    draw = ImageDraw.Draw(image)
    for x in (0, 128):
        draw.ellipse((x + 44, 70, x + 83, 189), fill="cyan")
    job = ImageJob(prompt="Two poses of one actor", width=64, height=32, transparent_background=True,
                   columns=2, rows=1, frame_names=("idle", "run"))
    result, report = prepare_generated(png(image), job)
    assert result.size == image.size
    frames = [result.crop((x, 0, x + 128, 256)) for x in (0, 128)]
    assert frames[0].tobytes() == frames[1].tobytes()
    bbox = frames[0].getchannel("A").getbbox()
    assert abs((bbox[2] - bbox[0]) / (bbox[3] - bbox[1]) - 1 / 3) < .05
    transforms = [op for op in report["operations"] if op["operation"] == "frame_native"]
    assert transforms[0]["scale"] == transforms[1]["scale"]
    assert transforms[0]["scale"] == 1 and not report["resampled"]


def test_letterbox_honors_bottom_pivot_instead_of_centering_feet_in_the_air():
    image = Image.new("RGBA", (80, 20), (20, 180, 220, 255))
    output, transform = contained(image, (32, 32), pivot=(.5, 1))
    assert output.getchannel("A").getbbox() == (0, 24, 32, 32)
    assert transform["padding_offset"] == (0, 24)


@pytest.mark.parametrize("mode", ["auto", "chroma_key"])
@pytest.mark.parametrize("color", [(255, 0, 255, 255), (219, 75, 224, 254)])
def test_key_removes_realistic_magenta_and_preserves_subject(mode, color):
    source = silhouette(background=color)
    result, report = remove_matte(source, mode=mode)
    assert result.getpixel((0, 0))[3] == 0
    assert result.getpixel((31, 11)) == source.getpixel((31, 11))
    assert report["removed_pixels"] > 2000
    assert not sprite_findings(result, "idle", check_matte=True)


def test_auto_key_protects_enclosed_purple_detail_and_native_transparency():
    source = silhouette(background=(255, 0, 255, 255))
    source.putpixel((31, 11), (255, 0, 255, 255))
    result, _ = remove_matte(source)
    assert result.getpixel((0, 0))[3] == 0 and result.getpixel((31, 11))[3] == 255
    native = silhouette(color=(255, 0, 255, 128))
    preserved, report = remove_matte(native)
    assert preserved.tobytes() == native.tobytes() and report["operation"] == "preserve_alpha"
    assert not sprite_findings(native, "purple_character")


def test_soft_key_is_confined_to_neighboring_matte_not_subject_interior():
    source = Image.new("RGBA", (12, 12), "magenta")
    draw = ImageDraw.Draw(source)
    draw.rectangle((3, 3, 8, 8), fill=(195, 60, 195, 255))
    draw.rectangle((4, 4, 7, 7), fill=(20, 180, 220, 128))
    result, report = remove_matte(source, mode="chroma_key")
    assert 0 < result.getpixel((3, 5))[3] < 255
    assert result.getpixel((5, 5)) == (20, 180, 220, 128)
    assert report["softened_edge_pixels"] > 0


@pytest.mark.parametrize("background", [(255, 255, 255, 255), (20, 100, 180, 255)])
def test_auto_does_not_invent_transparency_for_non_magenta_backgrounds(background):
    source = silhouette(background=background)
    result, _ = remove_matte(source)
    assert result.tobytes() == source.tobytes()
    assert any(f["code"] == "opaque_sprite" for f in sprite_findings(result, "actor"))


@pytest.mark.parametrize("defect", ["ground", "caption", "card", "empty"])
def test_suspect_actor_is_quarantined_with_diagnostic_preview(defect):
    source = silhouette()
    draw = ImageDraw.Draw(source)
    if defect == "ground": draw.rectangle((2, 45, 61, 60), fill="green")
    if defect == "caption":
        for x in range(2, 62, 8): draw.rectangle((x, 60, x + 2, 62), fill="white")
    if defect == "card": draw.rectangle((3, 3, 60, 60), fill="navy")
    if defect == "empty": source = Image.new("RGBA", source.size)
    info, frames, preview = normalize_pixels(png(source), WHOLE, 1, 1, ["actor"], check_quality=True)
    assert info["quality"]["status"] == "rejected" and not frames and preview
    assert info["quality"]["findings"]


@pytest.mark.parametrize("name", ["player_scene", "ghost_card"])
def test_recorded_real_scenic_sprites_fail_before_and_after_resize(name):
    data, report, job, layout = recorded(name)
    assert report["findings"]  # Original-resolution evidence is retained.
    layout, _ = generated_layout(job, report["output"]["width"], report["output"]["height"])
    info, frames, _ = normalize_pixels(data, layout, job.columns, job.rows, job.frame_names, check_quality=True)
    assert info["quality"]["status"] == "rejected" and not frames
    assert Image.open(io.BytesIO(data)).getchannel("A").getextrema()[0] == 0
    provenance = next(p for p in json.loads((FIXTURES / "provenance.json").read_text()) if p["name"] == name)
    assert hashlib.sha256((FIXTURES / (name + ".png")).read_bytes()).hexdigest() == provenance["sha256"]


def test_embedded_clean_report_cannot_override_pixel_checks():
    image = Image.new("RGBA", (32, 32), "blue")
    metadata = PngInfo(); metadata.add_text(PROCESSING_KEY, '{"findings":[]}')
    stream = io.BytesIO(); image.save(stream, format="PNG", pnginfo=metadata)
    info, frames, _ = normalize_pixels(stream.getvalue(), WHOLE, 1, 1, ["actor"], check_quality=True)
    assert info["quality"]["status"] == "rejected" and not frames


def test_raw_source_crop_budget_is_enforced_before_materialization(monkeypatch):
    from adaptive_game_generation.runtime import asset_normalization
    monkeypatch.setattr(asset_normalization, "MAX_FRAME_PIXELS", 1000)
    job = ImageJob(prompt="Small final sprite from large source", width=16, height=16,
        transparent_background=True, columns=1, rows=1, frame_names=("idle",))
    with pytest.raises(ValueError, match="pixel budget"):
        prepare_generated(png(silhouette(background=(255, 0, 255, 255))), job)


def test_incorrect_embedded_report_quarantines_instead_of_crashing():
    image = silhouette()
    for raw in ('[]', '{"findings": [1]}', 'x' * 65537):
        metadata = PngInfo(); metadata.add_text(PROCESSING_KEY, raw)
        stream = io.BytesIO(); image.save(stream, format="PNG", pnginfo=metadata)
        info, frames, _ = normalize_pixels(stream.getvalue(), WHOLE, 1, 1, ["idle"], check_quality=True)
        assert not frames and info["quality"]["findings"][0]["code"] == "invalid_processing_report"


def test_legitimate_library_font_and_scene_are_preserved_for_their_actual_use():
    root = Path(__file__).parent / "fixtures/asset_normalization"
    for filename, kind in [("oga_font.png", "ui"), ("oga_scene_layer.png", "background")]:
        original = (root / filename).read_bytes()
        info, frames, _ = normalize_pixels(original, AssetLayout(kind=kind, mode="whole"), 1, 1, ["asset"], check_quality=True)
        assert info["quality"]["status"] == "not_applicable"
        assert Image.open(io.BytesIO(frames[0]["data"])).tobytes() == Image.open(io.BytesIO(original)).convert("RGBA").tobytes()


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_real_generated_rejection_reaches_art_developer_and_shared_work_policy(tmp_path, controller):
    from test_art_producer import make_run, invoke, manifest, asset, proposal, ScriptedText
    from test_developer import invoke as develop, proposal as patch, ScriptedSandbox, report as dev_report
    from adaptive_game_generation.controllers.work import WorkPolicy
    data, _, job, layout = recorded("player_scene")
    class SavedImage:
        async def generate(self, prompt, *, timeout): return data
    draft = asset(width=job.width, height=job.height, columns=job.columns, rows=job.rows,
                  frame_names=list(job.frame_names), layout=layout.model_dump(mode="json"))
    executor, text, _ = make_run(tmp_path, proposal(draft), controller=controller, image=SavedImage())
    executor.services = replace(executor.services, image_info=ImageServiceInfo(provider_id="saved-real-image", is_mock=False, license="Saved fixture terms", output_geometry="source_native"))
    assert invoke(executor, ArtProducer(ArtProducerConfig(normalize_assets=True))).status == "partial"
    package = manifest(executor).assets[0].normalization
    assert package.status == "rejected" and package.quality.findings and not package.frames
    assert WorkPolicy(executor.workspace).plan(executor.state, Role.ART_PRODUCER)[0]
    executor.services = replace(executor.services, text=ScriptedText(patch()), sandbox=ScriptedSandbox())
    outcome = develop(executor, DeveloperConfig(require_normalized_assets=True))
    assert outcome.status == "partial", outcome.error
    result = dev_report(executor)
    assert not result.integrated_assets
    assert not (executor.workspace.read_public(result.project_ref.path) / "assets/player.png").exists()
    assert asset_package_findings({"assets/renamed.png": package.source_ref.sha256}, [package])


@pytest.mark.parametrize("library", ["kenney", "opengameart"])
def test_library_rgb_keyed_sprite_uses_same_checks_and_keeps_license(tmp_path, library):
    from test_art_producer import make_run, invoke, manifest, asset, proposal, library_pair, search_turn
    libraries, roots = library_pair(tmp_path / "libraries")
    source = silhouette(background=(219, 75, 224, 255)).convert("RGB")
    source.save(roots[library] / "pack/player.png")
    draft = asset(source=library, source_path="pack/player.png", generation_prompt=None, width=64, height=64, layout=WHOLE.model_dump())
    executor, _, images = make_run(tmp_path / "run", search_turn(), proposal(draft), libraries=libraries)
    assert invoke(executor, ArtProducer(ArtProducerConfig(normalize_assets=True))).status == "completed"
    item = manifest(executor).assets[0]
    assert item.license_ref and item.normalization.quality.status == "checks_passed" and not images.calls
    frame = Image.open(executor.workspace.read_public(item.normalization.frames[0].artifact_ref.path))
    assert frame.getpixel((0, 0))[3] == 0 and frame.getpixel((31, 11))[3] == 255


def test_image_capabilities_are_selected_by_asset_type_without_extra_model_calls():
    base = ImageJob(prompt="Asset", width=64, height=64, transparent_background=True, columns=1, rows=1, frame_names=("asset",))
    actor = subject_instructions(base.revised(layout=WHOLE))
    background = subject_instructions(base.revised(layout=AssetLayout(kind="background", mode="whole")))
    ui = subject_instructions(base.revised(layout=AssetLayout(kind="ui", mode="whole")))
    assert "isolated actor" in actor and "No ground" in actor
    assert "Scenery is allowed" in background and "isolated actor" not in background
    assert "UI artwork" in ui and "isolated actor" not in ui


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_full_controllers_quarantine_real_art_and_qa_vetoes_delivery(tmp_path, controller):
    import os
    import shutil
    from adaptive_game_generation.controllers import FixedLoop, FixedLoopConfig, AdaptiveScheduler, SchedulerConfig
    from adaptive_game_generation.controllers.results import export_run, write_json
    from adaptive_game_generation.roles.developer import Developer, DeveloperReport
    from adaptive_game_generation.roles.player import PlayerReport
    from adaptive_game_generation.roles.qa_reviewer import QAReport
    from test_fixed_loop import make_run, report as saved_report
    from test_art_producer import asset, proposal
    from test_scheduler import ScriptedScheduler
    data, _, job, layout = recorded("player_scene")
    class SavedImage:
        info = ImageServiceInfo(provider_id="offline-saved-real-image", is_mock=False, license="Recorded generated fixture terms", output_geometry="source_native")
        def __init__(self): self.calls = []
        async def generate(self, prompt, *, timeout):
            self.calls.append({"input": json.loads(prompt), "output_sha256": hashlib.sha256(data).hexdigest(),
                               "mode": "offline saved real image; no generation request"})
            return data
    provider = ScriptedScheduler(("designer", "art_producer", "developer", "player", "qa_reviewer"))
    executor, adapters, workers, images = make_run(tmp_path, controller=controller, scheduler_provider=provider, image_service=SavedImage())
    workers[Role.ART_PRODUCER].replies = [proposal(asset(width=job.width, height=job.height, columns=job.columns,
        rows=job.rows, frame_names=list(job.frame_names), layout=layout.model_dump(mode="json")))]
    adapters[Role.ART_PRODUCER] = ArtProducer(ArtProducerConfig(normalize_assets=True))
    adapters[Role.DEVELOPER] = Developer(DeveloperConfig(prompt_version="developer.v3", require_normalized_assets=True))
    responses = {r: [] for r in workers}
    for role, worker in workers.items():
        original = worker.generate
        async def capture(*args, _role=role, _generate=original, **kwargs):
            reply = await _generate(*args, **kwargs)
            responses[_role].append(json.loads(reply.text))
            return reply
        worker.generate = capture
    runner = (FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=1)) if controller == "fixed_loop" else
              AdaptiveScheduler(executor, adapters, SchedulerConfig(max_role_calls=5)))
    state = asyncio.run(runner.run())
    dev = saved_report(executor, "developer-report", DeveloperReport)
    player = saved_report(executor, "player-report", PlayerReport)
    qa = saved_report(executor, "qa-report", QAReport)
    assert dev.asset_packages[0].status == "rejected" and not dev.integrated_assets
    assert player.comparisons and all(c.status == "passed" for c in player.comparisons)
    assert not qa.gates["final_art"] and not state.readiness.ready
    assert not (executor.workspace.read_public(dev.project_ref.path) / "assets/player.png").exists()
    assert "HIDDEN-LOOP" not in str([p.prompts for p in workers.values()])
    destination = os.environ.get("SPRITE_QUALITY_REVIEW_ROOT")
    if destination:
        output = Path(destination) / controller
        output.mkdir(parents=True, exist_ok=False)
        export_run(executor, output, mode="offline-scripted-models-and-engine/saved-real-art-quality-quarantine")
        shutil.copytree(executor.workspace.root, output / "workspace")
        write_json(output / "offline_model_io.json", {r.value: {"inputs": w.prompts, "outputs": responses[r]} for r, w in workers.items()})
        write_json(output / "offline_scheduler_io.json", {"inputs": provider.prompts, "outputs": provider.responses})
        write_json(output / "offline_image_io.json", images.calls)
