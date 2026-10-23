"""Offline pixel preservation, native-layout handoff and provider prompt contracts."""
from dataclasses import replace
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from adaptive_game_generation.models.asset_pipeline import AssetLayout, FrameRegion
from adaptive_game_generation.providers.openai_image import ImageGatewayConfig, OpenAICompatibleImageProvider
from adaptive_game_generation.providers.dashscope_image import DashScopeImageConfig, DashScopeImageProvider
from adaptive_game_generation.roles.art_producer import ArtProducer, ArtProducerConfig, ImageJob
from adaptive_game_generation.runtime.asset_normalization import generated_layout, normalize_pixels, png
from adaptive_game_generation.runtime.image_processing import prepare_generated, remove_matte
from test_sprite_quality import silhouette


def job(**changes):
    return ImageJob(prompt="One isolated runner pose", width=16, height=16, transparent_background=True,
        columns=1, rows=1, frame_names=("idle",), layout=AssetLayout(kind="sprite", mode="whole")).revised(**changes)


@pytest.mark.parametrize("resample", ["nearest", "lanczos"])
@pytest.mark.parametrize("mode", ["native", "chroma_key", "opaque"])
def test_lossless_native_pixels_even_when_logical_size_is_tiny(mode, resample):
    source = silhouette(background=(219, 75, 224, 255) if mode == "chroma_key" else (0, 0, 0, 0))
    # One-pixel high-frequency details and partial alpha must survive exactly.
    for y in range(7, 18):
        for x in range(28, 35):
            source.putpixel((x, y), (x * 3, y * 11, 93, 127 + (x + y) % 128))
    request = job()
    if mode == "opaque":
        source = source.convert("RGB")
        request = request.revised(transparent_background=False, layout=AssetLayout(kind="background", mode="whole"))
    expected = remove_matte(source, mode="chroma_key")[0] if mode == "chroma_key" else source
    result, report = prepare_generated(png(source), request, transparency=mode, resample=resample)
    assert result.size == source.size == (64, 64)
    assert result.tobytes() == expected.tobytes()
    assert Image.open(io.BytesIO(png(result))).tobytes() == expected.tobytes()
    assert report["encoding"] == "lossless_png" and report["resampled"] is False
    assert all(op["scale"] == 1 for op in report["operations"] if "scale" in op)


def test_odd_native_grid_has_shared_boundaries_without_lost_pixels():
    request = job(width=30, height=10, columns=3, frame_names=("a", "b", "c"),
                  layout=AssetLayout(kind="sprite_sheet", mode="grid", cell_width=10, cell_height=10))
    native, logical = generated_layout(request, 257, 131)
    assert [(r.x, r.width) for r in native.regions] == [(0, 86), (86, 85), (171, 86)]
    assert all(r.height == 131 for r in native.regions)
    assert [(r.width, r.height) for r in logical] == [(10, 10)] * 3
    source = Image.new("RGBA", (257, 131))
    source.putdata([(x % 251, y % 127, (x + y) % 256, 128) for y in range(131) for x in range(257)])
    _, frames, _ = normalize_pixels(png(source), native, 3, 1, request.frame_names)
    stitched = b"".join(b"".join(Image.open(io.BytesIO(f["data"])).crop((0, y, f["region"].width, y + 1)).tobytes()
                                for f in frames) for y in range(131))
    assert stitched == source.tobytes()


def test_native_explicit_regions_preserve_gaps_pivots_and_timing():
    logical = FrameRegion(name="idle", x=2, y=3, width=12, height=10, pivot_x=.25, pivot_y=1, duration_ms=170)
    request = job(layout=AssetLayout(kind="sprite", mode="regions", regions=(logical,)))
    native, original = generated_layout(request, 80, 96)
    assert native.regions[0] == logical.revised(x=10, y=18, width=60, height=60)
    assert original == (logical,)


@pytest.mark.parametrize("dimensions", [(4097, 32), (32, 4097), (0, 32)])
def test_oversized_native_canvas_fails_instead_of_silently_reducing_quality(dimensions):
    with pytest.raises(ValueError, match="dimension limit"):
        generated_layout(job(), *dimensions)


def test_high_bit_depth_is_rejected_instead_of_silent_quantization():
    source = Image.new("I;16", (64, 64), 12345)
    with pytest.raises(ValueError, match="High-bit-depth"):
        prepare_generated(png(source), job())


@pytest.mark.parametrize("model", ["qwen-image-3.0-pro", "qwen-image-2.1-pro", "wan2.7-image", "wan2.7-image-pro"])
def test_actor_prompt_and_native_parameters_block_embellishments_without_unsupported_switches(model):
    config = DashScopeImageConfig(model=model, api_key="offline", base_url="https://offline.test/api/v1",
                                 prompt_extend=True, watermark=True)
    service = SimpleNamespace(config=config)
    service._image_prompt = lambda request: OpenAICompatibleImageProvider._image_prompt(service, request)
    request = job(prompt="A runner jumping in a grass scene; add a RUN label")
    payload = DashScopeImageProvider._payload(service, request)
    parameters = payload["parameters"]
    text = payload["input"]["messages"][0]["content"][0]["text"]
    assert parameters["watermark"] is False
    assert ("prompt_extend" not in parameters) if model.startswith("wan2.7") else parameters["prompt_extend"] is False
    assert "FINAL ACTOR CHECK" in text and "No text of any language" in text
    assert "not a request to rasterize at that low resolution" in text
    # Actor rules follow even conflicting requests, without changing background jobs.
    assert text.index("FINAL ACTOR CHECK") > text.index(request.prompt)
    background = DashScopeImageProvider._payload(service, request.revised(layout=AssetLayout(kind="background", mode="whole"), transparent_background=False))
    assert "FINAL ACTOR CHECK" not in background["input"]["messages"][0]["content"][0]["text"]


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_native_art_handoff_and_developer_package_keep_pixels_and_logical_size(tmp_path, controller):
    from test_art_producer import make_run, asset, proposal, invoke, manifest, ScriptedText
    from test_developer import invoke as develop, proposal as patch, ScriptedSandbox, report as developer_report
    from adaptive_game_generation.roles.developer import DeveloperConfig
    config = ImageGatewayConfig(model="offline-fixture", api_key="unused", base_url="https://unused.test/v1")
    source = png(silhouette(background=(255, 0, 255, 255)))
    calls = []
    class SavedSource:
        async def generate(self, prompt, *, timeout):
            request = ImageJob.model_validate_json(prompt)
            calls.append(request)
            return OpenAICompatibleImageProvider._normalize(SimpleNamespace(config=config), source, request)[0]
    draft = asset(width=16, height=16, layout={"kind": "sprite", "mode": "whole"}, filter_mode="linear")
    executor, art_worker, _ = make_run(tmp_path, proposal(draft), image=SavedSource(), controller=controller)
    executor.services = replace(executor.services, image_info=config.info)
    outcome = invoke(executor, ArtProducer(ArtProducerConfig(normalize_assets=True)))
    assert outcome.status == "completed", outcome.error
    item = manifest(executor).assets[0]
    assert (item.width, item.height) == (64, 64) and item.request.width == 16
    frame = item.normalization.frames[0]
    assert (frame.region.width, frame.region.height) == (64, 64) and frame.display_size == (16, 16)
    pixels = executor.workspace.read_public(frame.artifact_ref.path).read_bytes()
    expected = remove_matte(Image.open(io.BytesIO(source)), mode="chroma_key")[0]
    assert Image.open(io.BytesIO(pixels)).tobytes() == expected.tobytes()
    worker = ScriptedText(patch())
    executor.services = replace(executor.services, text=worker, sandbox=ScriptedSandbox())
    outcome = develop(executor, DeveloperConfig(require_normalized_assets=True))
    assert outcome.status == "completed", outcome.error
    result = developer_report(executor)
    assert (executor.workspace.read_public(result.project_ref.path) / frame.target_path).read_bytes() == pixels
    assert result.asset_packages[0].frames[0].display_size == (16, 16)
    assert "scale=Vector2(s,s)" in worker.prompts[0]["instructions"]
    assert len(calls) == 1 and executor.state.ledger.usage.image_calls == 1
    assert "HIDDEN-" not in json.dumps(worker.prompts)
    import os
    if destination := os.environ.get("SPRITE_FIDELITY_REVIEW_ROOT"):
        from pathlib import Path
        import shutil
        from adaptive_game_generation.controllers.results import export_run, write_json
        output = Path(destination) / controller
        output.mkdir(parents=True, exist_ok=False)
        export_run(executor, output, mode="offline-scripted-text-and-engine/synthetic-native-pixel-fixture")
        shutil.copytree(executor.workspace.root, output / "workspace")
        write_json(output / "complete_io.json", {
            "art_input": art_worker.prompts, "art_model_output": proposal(draft),
            "art_manifest": manifest(executor).model_dump(mode="json"),
            "image_job": calls[0].model_dump(mode="json"),
            "developer_input": worker.prompts, "developer_model_output": patch(),
            "developer_report": result.model_dump(mode="json"), "online_calls": 0})
