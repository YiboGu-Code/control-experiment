"""Replay real failed replies offline; successful corrections are synthetic fixtures."""

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from adaptive_game_generation.models import ResourceLimits
from adaptive_game_generation.providers.mock_image import MockImageProvider
from adaptive_game_generation.roles.art_producer import ArtProducer, ArtProducerConfig
from adaptive_game_generation.roles.art_producer.contracts import AssetDraft
from adaptive_game_generation.runtime.asset_libraries import MountedAssetLibraries

from test_art_producer import asset, invoke, library_pair, library_plan, make_run, manifest, proposal, search_turn

FIXTURES = Path(__file__).parent / "fixtures/art_producer_real_images_001"


@pytest.mark.parametrize("fixture,error", [
    ("invalid_grid.txt", "98 cells"),
    ("truncated_frames.txt", "Invalid or incomplete JSON"),
])
def test_recorded_failure_is_repaired_with_catalog_retained_before_any_image(tmp_path, fixture, error):
    provenance = json.loads((FIXTURES / "provenance.json").read_bytes())
    entry = next(e for e in provenance["responses"] if e["fixture"] == fixture)
    raw = (FIXTURES / fixture).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == entry["sha256"]

    # Same public path/dimensions as the failed plan, explicitly synthetic pixels/license.
    root = tmp_path / "library"
    relative = "2D/simplified-platformer-pack/Tilesheet/platformPack_tilesheet.png"
    source = root / relative
    source.parent.mkdir(parents=True)
    Image.new("RGBA", (896, 448), (0, 100, 150, 255)).save(source)
    (source.parent / "LICENSE.txt").write_text("Synthetic offline library fixture: CC0.", encoding="utf-8")
    libraries = MountedAssetLibraries(kenney=root, opengameart=tmp_path / "absent-oga")
    search = {"action": "search", "summary": "Inspect the available platformer assets",
              "searches": [{"library": "kenney", "query": "platformer"}]}
    corrected = proposal(
        asset("atlas", source="kenney", source_path=relative, generation_prompt=None,
              width=896, height=448, columns=1, rows=1, frame_names=["atlas"],
              integration_notes="Import the whole texture unchanged. Developer must verify regions before selecting tiles."),
        asset("marker", generation_fallback={"reason": "The returned atlas has no verified isolated marker region",
            "search_ids": ["search-001"], "rejected_candidates": [{"library": "kenney", "path": relative,
                "reason": "Only a large unverified tile sheet was returned; a standalone marker is needed"}]}),
    )
    class GuardedImage(MockImageProvider):
        async def generate(self, prompt, *, timeout):
            assert len(text.prompts) == 3
            return await super().generate(prompt, timeout=timeout)

    executor, text, image = make_run(tmp_path / "case", search, raw.decode("utf-8"), corrected,
                                    libraries=libraries, image=GuardedImage())
    outcome = invoke(executor, ArtProducer(ArtProducerConfig(max_model_turns=3)))
    assert outcome.status == "partial", outcome.error
    assert len(text.prompts) == executor.state.ledger.usage.model_calls == 3
    assert executor.state.ledger.usage.image_calls == len(image.calls) == 1
    assert executor.state.ledger.usage.token_usage_complete
    feedback = text.prompts[2]
    assert feedback["search_observations"] == text.prompts[1]["search_observations"]
    assert feedback["limits"]["turns_left"] == 1
    assert error in str(feedback["proposal_rejections"])
    assert feedback["proposal_rejections"][0]["model_turn"] == 2
    excerpt = feedback["latest_rejected_response"]
    assert len(excerpt["excerpt"].encode("utf-8")) <= 8192
    assert excerpt["truncated"] == (len(raw) > 8192)
    assert excerpt["original_bytes"] == len(raw)
    assert all(p["input"]["task"]["instruction"] == executor.state.task.instruction for p in text.prompts)
    assert "HIDDEN-" not in json.dumps(text.prompts) and "SECRET-SENTINEL" not in json.dumps(text.prompts)
    document = manifest(executor)
    assert document.config.prompt_version == "art-producer.v4"
    assert document.proposal_rejections[0].model_turn == 2
    atlas = document.assets[0]
    assert atlas.request.frame_names == ("atlas",)
    assert not atlas.is_mock and atlas.visual_review == "not_performed"
    assert executor.workspace.read_public(atlas.record.artifact_ref.path).read_bytes() == source.read_bytes()


def test_unsearched_library_path_gets_actionable_feedback_and_can_be_searched(tmp_path):
    libraries, _ = library_pair(tmp_path)
    executor, text, image = make_run(tmp_path / "case", library_plan(), search_turn(), library_plan(),
                                    libraries=libraries)
    result = invoke(executor, ArtProducer(ArtProducerConfig(max_model_turns=3)))
    assert result.status == "completed", result.error
    assert "assets.0.source_path" in str(text.prompts[1]["proposal_rejections"])
    assert "metered search" in str(text.prompts[1]["proposal_rejections"])
    assert len(text.prompts) == 3 and not image.calls
    assert text.prompts[2]["latest_rejected_response"] is None
    assert len(manifest(executor).proposal_rejections) == 1


def test_wrong_library_dimensions_are_repaired_before_import(tmp_path):
    libraries, _ = library_pair(tmp_path)
    invalid = library_plan()
    invalid["assets"][0]["width"] = 64
    executor, text, _ = make_run(tmp_path / "case", search_turn(), invalid, library_plan(), libraries=libraries)
    result = invoke(executor, ArtProducer(ArtProducerConfig(max_model_turns=3)))
    assert result.status == "completed", result.error
    assert "width=32, height=32" in str(text.prompts[2]["proposal_rejections"])


def test_invalid_replies_exhaust_only_the_existing_model_turn_allowance(tmp_path):
    invalid = proposal(asset(columns=14, rows=7, width=896, height=448, frame_names=["atlas"]))
    executor, text, image = make_run(tmp_path, *([invalid] * 4), proposal())
    result = invoke(executor)
    assert result.status == "failed" and result.error.code == "invalid_submission"
    assert "validation budget exhausted" in result.error.message and "98 cells" in result.error.message
    assert len(text.prompts) == executor.state.ledger.usage.model_calls == 4
    assert len(text.replies) == 1  # No hidden fifth correction.
    assert not image.calls and not executor.state.assets and executor.state.state_revision == 1
    assert not list((executor.workspace.public / "artifacts/art_producer").glob("*"))
    assert [p["limits"]["turns_left"] for p in text.prompts] == [4, 3, 2, 1]


def test_shared_budget_prevents_a_correction_call(tmp_path):
    limits = ResourceLimits(max_model_calls=1, max_input_tokens=900000, max_output_tokens=100000,
                            max_image_calls=10, max_tool_calls=100)
    executor, text, image = make_run(tmp_path, '{"action":', proposal(), budget=limits)
    result = invoke(executor)
    assert result.status == "failed" and result.error.code == "budget_exceeded"
    assert len(text.prompts) == executor.state.ledger.usage.model_calls == 1
    assert executor.state.ledger.usage.token_usage_complete
    assert not image.calls and not executor.state.assets


@pytest.mark.parametrize("version", ["art-producer.v2", "art-producer.v3"])
def test_explicit_legacy_versions_preserve_fail_fast_behavior(tmp_path, version):
    executor, text, image = make_run(tmp_path, '{"action":', proposal())
    result = invoke(executor, ArtProducer(ArtProducerConfig(prompt_version=version)))
    assert result.status == "failed" and "invalid JSON proposal" in result.error.message
    assert len(text.prompts) == 1 and not image.calls
    assert "proposal_rejections" not in text.prompts[0]


def test_grid_contract_retains_strict_limits_and_allows_a_whole_sheet():
    with pytest.raises(ValidationError, match="98 cells"):
        AssetDraft.model_validate(asset(width=896, height=448, columns=14, rows=7, frame_names=["atlas"]))
    with pytest.raises(ValidationError, match="evenly divide"):
        AssetDraft.model_validate(asset(width=128, height=128, rows=6, frame_names=list("abcdef")))
    with pytest.raises(ValidationError, match="expected 4, received 1"):
        AssetDraft.model_validate(asset(columns=2, rows=2, frame_names=["atlas"]))
    assert AssetDraft.model_validate(asset(width=896, height=448, frame_names=["atlas"])).columns == 1


def test_oversized_response_is_not_retried(tmp_path):
    executor, text, image = make_run(tmp_path, "x" * 101, proposal())
    result = invoke(executor, ArtProducer(ArtProducerConfig(max_response_bytes=100)))
    assert result.status == "failed" and "byte limit" in result.error.message
    assert len(text.prompts) == 1 and not image.calls
