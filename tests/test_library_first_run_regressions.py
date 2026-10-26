"""Real saved rasters/traces, scripted decisions; no online model or engine calls."""
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from adaptive_game_generation.models import ArtifactRef
from adaptive_game_generation.models.asset_pipeline import AssetLayout
from adaptive_game_generation.providers.openai_image import OpenAICompatibleImageProvider
from adaptive_game_generation.roles.art_producer import ArtProducer, ArtProducerConfig, ImageJob
from adaptive_game_generation.roles.art_producer.contracts import AssetDraft
from adaptive_game_generation.roles.art_producer.sourcing import validate_fallback
from adaptive_game_generation.roles.developer.delivery import rank_captures
from adaptive_game_generation.roles.player.contracts import InputTrace
from adaptive_game_generation.runtime import Executor, InvalidSubmission, Services
from adaptive_game_generation.runtime.asset_normalization import generated_layout, normalize_pixels, png
from adaptive_game_generation.runtime.media import ImageServiceInfo
from test_art_producer import asset, proposal, invoke, manifest, make_run, library_pair
from test_library_grounding_repairs import quality_run, export_review
from test_sprite_quality import SETTINGS, silhouette

FIXTURES = Path(__file__).parent / "fixtures/fixed_loop_library_first_001"


@pytest.mark.parametrize("name,kind", [("terrain", "tile"), ("world_switch", "prop")])
def test_real_terrain_and_switch_are_not_actors_and_keep_exact_pixels(name, kind):
    raw = (FIXTURES / (name + ".png")).read_bytes()
    provenance = json.loads((FIXTURES / "provenance.json").read_bytes())
    original = next(a["asset"] for a in provenance["assets"] if a["name"] == name)
    assert hashlib.sha256(raw).hexdigest() == original["source_sha256"]
    old, bad_frames, _ = normalize_pixels(raw, AssetLayout(kind="sprite", mode="whole"), 1, 1, [name], check_quality=True)
    new, frames, preview = normalize_pixels(raw, AssetLayout(kind=kind, mode="whole"), 1, 1, [name], check_quality=True)
    assert old["quality"]["status"] == "rejected" and not bad_frames
    assert new["quality"]["status"] == "checks_passed" and len(frames) == 1
    with Image.open(io.BytesIO(raw)) as before, Image.open(io.BytesIO(frames[0]["data"])) as after:
        assert before.size == after.size and before.convert("RGBA").tobytes() == after.tobytes()
    if root := os.environ.get("PIPELINE_REVIEW_ROOT"):
        destination = Path(root) / "asset-profiles"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / f"{name}-after.png").write_bytes(frames[0]["data"])
        (destination / f"{name}-preview.png").write_bytes(preview)
        (destination / f"{name}-before.png").write_bytes(raw)
        (destination / f"{name}-checks.json").write_text(json.dumps({"before": old, "after": new}, indent=2), encoding="utf-8")


def test_saved_opaque_ui_is_quality_feedback_not_provider_crash():
    raw = (FIXTURES / "opaque_ui.png").read_bytes()
    meta = json.loads((FIXTURES / "provenance.json").read_bytes())
    assert hashlib.sha256(raw).hexdigest() == meta["failed_image"]["sha256"]
    job = ImageJob.model_validate_json((FIXTURES / "opaque_ui.job.json").read_bytes())
    data, report = OpenAICompatibleImageProvider._normalize(SimpleNamespace(config=SETTINGS), raw, job)
    layout, _ = generated_layout(job, *Image.open(io.BytesIO(raw)).size)
    info, frames, _ = normalize_pixels(data, layout, 1, 1, job.frame_names, check_quality=True)
    assert "missing_transparency" in {f["code"] for f in report["findings"]}
    assert info["quality"]["status"] == "rejected" and not frames
    assert not report["resampled"] and report["encoding"] == "lossless_png"


@pytest.mark.parametrize("controller", ["fixed_loop", "adaptive_scheduler"])
def test_opaque_image_feedback_repairs_only_failed_candidate_in_executor(tmp_path, controller):
    raw = (FIXTURES / "opaque_ui.png").read_bytes()
    job = ImageJob.model_validate_json((FIXTURES / "opaque_ui.job.json").read_bytes())
    data, _ = OpenAICompatibleImageProvider._normalize(SimpleNamespace(config=SETTINGS), raw, job)
    first = asset("reset_button", width=job.width, height=job.height, layout=job.layout.model_dump(),
                  generation_prompt=job.prompt, frame_names=list(job.frame_names), purpose="One HUD button")
    good = asset("player", width=64, height=64, layout={"kind": "sprite", "mode": "whole"})
    repair = {**first, "generation_prompt": "One isolated HUD button on a pure magenta matte, with no text or surrounding panel"}
    replies = [proposal(first, good), proposal(repair)]
    executor, text, image = quality_run(tmp_path, replies, [data, png(silhouette()), png(silhouette())], controller=controller)
    info = executor.services.image_info.revised(output_geometry="source_native")
    executor = Executor.open(executor.workspace, services=Services(text=text, image=image, image_info=info,
        libraries=executor.services.libraries))
    result = invoke(executor, ArtProducer(ArtProducerConfig(normalize_assets=True, max_quality_repair_rounds=2)))
    assert result.status == "completed", result.error
    doc = manifest(executor)
    assert len(image.calls) == 3 and len(text.prompts) == 2
    assert [f["asset_id"] for f in text.prompts[1]["input"]["quality_feedback"]] == ["reset_button"]
    assert doc.quality_attempts[0]["candidate"]["normalization"]["status"] == "rejected"
    assert all(a.normalization.status == "materialized" for a in doc.assets)
    assert executor.state.ledger.usage.image_calls == 3
    export_review("opaque-feedback-" + controller, executor, text, replies, image.calls)


def test_empty_terrain_still_rejected_and_sprite_relabel_still_forbidden():
    from adaptive_game_generation.roles.art_producer.sourcing import validate_repair
    empty = png(Image.new("RGBA", (64, 64)))
    info, frames, _ = normalize_pixels(empty, AssetLayout(kind="tile", mode="whole"), 1, 1, ["tile"], check_quality=True)
    assert info["quality"]["status"] == "rejected" and not frames
    old = AssetDraft.model_validate(asset(layout={"kind": "sprite", "mode": "whole"}))
    prior = SimpleNamespace(request=old, normalization=SimpleNamespace(status="rejected"))
    for kind in ("tile", "prop", "ui", "background"):
        with pytest.raises(InvalidSubmission, match="evade"):
            validate_repair(old.revised(layout=AssetLayout(kind=kind, mode="whole")), prior)


def test_opaque_magenta_terrain_is_not_mistaken_for_an_actor_matte():
    image = Image.new("RGBA", (32, 32), "magenta")
    info, frames, _ = normalize_pixels(png(image), AssetLayout(kind="tile", mode="whole"), 1, 1, ["tile"], check_quality=True)
    assert info["quality"]["status"] == "checks_passed"
    assert Image.open(io.BytesIO(frames[0]["data"])).tobytes() == image.tobytes()


@pytest.mark.parametrize("scope", [[], ["door"], ["player"]])
def test_fallback_searches_are_scoped_to_asset(scope):
    draft = AssetDraft.model_validate(asset(generation_fallback={"reason": "No suitable character returned",
        "search_ids": ["search-001"], "rejected_candidates": []}))
    sources = {"libraries": [{"library": "kenney", "available": True}]}
    observations = [{"search_id": "search-001", "library": "kenney", "asset_ids": scope, "assets": [], "scan_truncated": False}]
    if scope == ["player"]:
        validate_fallback(draft, sources, observations, require_scoped=True)
    else:
        with pytest.raises(InvalidSubmission, match="search scope"):
            validate_fallback(draft, sources, observations, require_scoped=True)


def test_scoped_search_batch_preserves_metering_and_allows_fallback(tmp_path):
    libraries, _ = library_pair(tmp_path)
    searches = {"action": "search", "summary": "Search each missing object independently", "searches": [
        {"library": library, "query": "missing_clock_device", "asset_ids": ["clock"]}
        for library in ("kenney", "opengameart")] + [
        {"library": "kenney", "query": "player", "asset_ids": ["player"]}]}
    draft = asset("clock", layout={"kind": "prop", "mode": "whole"}, generation_fallback={
        "reason": "No clock was returned by either scoped query", "search_ids": ["search-001", "search-002"]})
    executor, text, image = make_run(tmp_path / "case", searches, proposal(draft), libraries=libraries)
    result = invoke(executor, ArtProducer(ArtProducerConfig(normalize_assets=True)))
    assert result.status == "partial", result.error  # Mock art remains a placeholder.
    assert len(image.calls) == 1 and len(manifest(executor).search_observations) == 3
    assert text.prompts[1]["search_observations"][0]["asset_ids"] == ["clock"]


def test_real_delivery_prefers_playing_capture_over_earlier_menu():
    raw = json.loads((FIXTURES / "capture_candidates.json").read_bytes())
    sessions = [SimpleNamespace(**{**s, "trace_ref": ArtifactRef.model_validate(s["trace_ref"]),
        "executed_trace": InputTrace.model_validate(s["executed_trace"])}) for s in raw["sessions"]]
    ranked = rank_captures(sessions, set())
    assert ranked[0].session_id == "ordinary_title_level1_play_probe-capture"
    assert ranked[0].executed_trace.duration_frames == 420
    assert ranked[1].scenario is not None
    assert all(s in sessions for s in ranked)  # Selection does not rewrite traces.
    assert rank_captures(sessions, {s.trace_ref.sha256 for s in sessions}) == ranked


def test_input_prompt_pack_cannot_supply_a_world_switch_or_actor():
    from adaptive_game_generation.roles.art_producer.sourcing import validate_library_usage
    candidate = SimpleNamespace(path="input-prompts/Nintendo Switch 2/Default/switch_button_a.png")
    for kind in ("prop", "sprite", "tile"):
        request = AssetDraft.model_validate(asset(layout={"kind": kind, "mode": "whole"}))
        with pytest.raises(InvalidSubmission, match="UI glyphs"):
            validate_library_usage(request, candidate)
    request = request.revised(layout=AssetLayout(kind="ui", mode="whole"), purpose="Controller button input hint")
    validate_library_usage(request, candidate)


def test_new_scenarios_do_not_displace_the_only_ordinary_entry():
    raw = json.loads((FIXTURES / "capture_candidates.json").read_bytes())
    sessions = [SimpleNamespace(**{**s, "trace_ref": ArtifactRef.model_validate(s["trace_ref"]),
        "executed_trace": InputTrace.model_validate(s["executed_trace"])}) for s in raw["sessions"]]
    reused = {s.trace_ref.sha256 for s in sessions if s.scenario is None}
    assert rank_captures(sessions, reused)[0].scenario is None
    assert rank_captures(sessions, reused)[1].scenario is not None
