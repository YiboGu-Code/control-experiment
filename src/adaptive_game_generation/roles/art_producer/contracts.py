"""Model-authored asset requests, separate from host-authored provenance."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from ...models import ArtifactRef, AssetRecord, Blocker
from ...models.base import Identifier, Model, PositiveCount, Text, require_unique
from ...models.asset_pipeline import AssetLayout, NormalizedAsset
from ...runtime.workspace import logical_path

AssetId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
Dimension = Annotated[int, Field(strict=True, ge=1, le=4096)]


class ArtProducerConfig(Model):
    prompt_version: Literal["art-producer.v2", "art-producer.v3", "art-producer.v4"] = "art-producer.v4"
    max_output_tokens: PositiveCount = 8192
    max_model_turns: Annotated[int, Field(strict=True, ge=1, le=8)] = 4
    max_search_turns: Annotated[int, Field(strict=True, ge=0, le=7)] | None = None
    max_assets: Annotated[int, Field(strict=True, ge=1, le=32)] = 16
    search_results: Annotated[int, Field(strict=True, ge=1, le=20)] = 8
    max_prompt_bytes: PositiveCount = 512 * 1024
    max_response_bytes: PositiveCount = 256 * 1024
    allow_generation: bool = True
    normalize_assets: bool = False
    library_first: bool = True
    max_quality_repair_rounds: Annotated[int, Field(strict=True, ge=0, le=3)] = 0
    require_actor_motion: bool = False
    progressive_skills: bool = False


class SearchRequest(Model):
    library: Literal["kenney", "opengameart"]
    query: Annotated[str, StringConstraints(max_length=256)]
    offset: Annotated[int, Field(strict=True, ge=0, le=20000)] = 0
    asset_ids: Annotated[tuple[AssetId, ...], Field(max_length=4)] = ()


class RejectedLibraryCandidate(Model):
    library: Literal["kenney", "opengameart"]
    path: Text
    reason: Text


class GenerationFallback(Model):
    reason: Text
    search_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=16)]
    rejected_candidates: Annotated[tuple[RejectedLibraryCandidate, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def unique(self):
        require_unique(self.search_ids, "fallback search IDs")
        require_unique(((r.library, r.path) for r in self.rejected_candidates), "rejected candidates")
        return self


class MotionClip(Model):
    name: AssetId
    # Cross-asset references permit clean individual library frames in one clip.
    frames: Annotated[tuple[str, ...], Field(min_length=1, max_length=32)]
    fps: Annotated[int, Field(strict=True, ge=1, le=30)] = 8
    loop: bool = True


class ActorMotion(Model):
    mode: Literal["frames", "procedural", "static"]
    display_height: Annotated[int, Field(strict=True, ge=8, le=512)]
    clips: Annotated[tuple[MotionClip, ...], Field(max_length=8)] = ()
    implementation: Text

    @model_validator(mode="after")
    def coherent(self):
        require_unique((c.name for c in self.clips), "animation names")
        if (self.mode == "frames") != bool(self.clips):
            raise ValueError("Frame animation requires clips; procedural/static modes use implementation notes")
        if self.mode == "frames" and not any(len(set(c.frames)) >= 2 for c in self.clips):
            raise ValueError("An animated actor needs at least one clip with two distinct frame references")
        return self


class AssetDraft(Model):
    asset_id: AssetId
    source: Literal["kenney", "opengameart", "generate"]
    source_path: Text | None = None
    generation_prompt: Text | None = None
    generation_fallback: GenerationFallback | None = None
    purpose: Text
    requirement_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    target_path: Annotated[str, StringConstraints(min_length=1, pattern=r"^assets/"),
        Field(description="Project-relative output path starting with assets/, e.g. assets/sprites/player.png. Never sprites/player.png or res://assets/player.png.")]
    width: Dimension | None = None
    height: Dimension | None = None
    transparent_background: bool = False
    columns: Annotated[int, Field(strict=True, ge=1, le=32)] = 1
    rows: Annotated[int, Field(strict=True, ge=1, le=32)] = 1
    frame_names: Annotated[tuple[Text, ...], Field(min_length=1, max_length=64)]
    filter_mode: Literal["nearest", "linear"] = "nearest"
    integration_notes: Text
    affected_scenes: Annotated[tuple[Text, ...], Field(min_length=1)]
    license_handling: Text
    layout: AssetLayout | None = None
    reference_asset_ids: Annotated[tuple[AssetId, ...], Field(max_length=3)] = ()
    motion: ActorMotion | None = None

    @model_validator(mode="after")
    def consistent_request(self):
        logical_path(self.target_path)
        if not self.target_path.startswith("assets/"):
            raise ValueError("Developer target must be inside assets/")
        if self.target_path.casefold().startswith("assets/normalized/"):
            raise ValueError("assets/normalized/ is reserved for host-derived frames")
        if (self.width is None) != (self.height is None):
            raise ValueError("Both image dimensions must be specified together")
        if self.source == "generate":
            if not self.generation_prompt or self.source_path is not None or self.width is None:
                raise ValueError("Generation requires a prompt and dimensions, without a library path")
            if not self.target_path.endswith(".png"):
                raise ValueError("Generated output must be PNG")
        elif not self.source_path or self.generation_prompt is not None:
            raise ValueError("Library import requires a source path, without a generation prompt")
        if self.source != "generate" and self.generation_fallback is not None:
            raise ValueError("Only generated assets may declare a generation fallback")
        if not self.layout and self.width and (self.width % self.columns or self.height % self.rows):
            raise ValueError(f"Sprite grid {self.columns} x {self.rows} must evenly divide image dimensions {self.width} x {self.height}")
        if not self.layout and self.columns * self.rows > 64:
            raise ValueError(f"Sprite grid has {self.columns * self.rows} cells; at most 64 are allowed. Select explicit regions for the needed frames or use individual library images")
        if not self.layout and len(self.frame_names) != self.columns * self.rows:
            raise ValueError(f"Frame names must describe every grid cell in row-major order: expected {self.columns * self.rows}, received {len(self.frame_names)}")
        if self.width is None and (self.rows != 1 or self.columns != 1 or self.transparent_background):
            raise ValueError("Non-raster assets cannot request a sprite grid or transparency")
        if self.layout and self.width is None:
            raise ValueError("Only raster assets have layouts")
        require_unique(self.requirement_ids, "asset requirements")
        require_unique(self.frame_names, "frame names")
        require_unique(self.reference_asset_ids, "reference assets")
        if self.reference_asset_ids and self.source != "generate":
            raise ValueError("Only generated assets use image references")
        return self


class ArtTurn(Model):
    action: Literal["search", "submit", "load_skills"]
    skills: Annotated[tuple[Literal["asset_layout", "actor_motion", "reference_edit"], ...], Field(max_length=3)] = ()
    searches: Annotated[tuple[SearchRequest, ...], Field(max_length=12)] = ()
    status: Literal["completed", "no_op", "blocked"] | None = None
    summary: Text
    style_guide: Text | None = None
    assets: tuple[AssetDraft, ...] = ()
    blockers: tuple[Blocker, ...] = ()

    @model_validator(mode="after")
    def consistent_turn(self):
        if self.action == "load_skills":
            if not self.skills or self.searches or self.status or self.assets or self.style_guide or self.blockers:
                raise ValueError("Skill loads contain only skills and a summary")
            require_unique(self.skills, "skill names")
            return self
        if self.skills:
            raise ValueError("Only load_skills may request skill bodies")
        if self.action == "search":
            if not self.searches or self.status or self.assets or self.style_guide or self.blockers:
                raise ValueError("Search turns contain only searches and a summary")
        elif self.searches or self.status is None:
            raise ValueError("Submit turns require status and cannot search")
        elif self.status == "completed":
            if not self.assets or not self.style_guide or self.blockers:
                raise ValueError("Completed proposals require assets and a style guide")
        elif self.assets or self.style_guide or (self.status == "blocked") != bool(self.blockers):
            raise ValueError("Only blocked proposals require blockers; no changes for no_op/blocked")
        require_unique((a.asset_id for a in self.assets), "asset IDs")
        require_unique((a.target_path.casefold() for a in self.assets), "asset target paths")
        return self


class ImageJob(Model):
    protocol: Literal["art-image.v1"] = "art-image.v1"
    prompt: Text
    width: Dimension
    height: Dimension
    transparent_background: bool
    columns: PositiveCount
    rows: PositiveCount
    frame_names: tuple[Text, ...]
    layout: AssetLayout | None = None


class ProducedAsset(Model):
    request: AssetDraft
    record: AssetRecord
    is_mock: bool
    width: PositiveCount | None = None
    height: PositiveCount | None = None
    has_alpha: bool = False
    source_sha256: Text | None = None
    source_license_path: Text | None = None
    source_license_sha256: Text | None = None
    license_ref: ArtifactRef | None = None
    image_job: ImageJob | None = None
    visual_review: Literal["not_performed"] = "not_performed"
    normalization: NormalizedAsset | None = None


class ProposalRejection(Model):
    model_turn: PositiveCount
    errors: tuple[Text, ...]


class ArtManifest(Model):
    schema_version: Literal["1.0"] = "1.0"
    task_id: Identifier
    version: PositiveCount
    config: ArtProducerConfig
    proposal_rejections: tuple[ProposalRejection, ...] = ()
    search_observations: tuple[dict, ...] = ()
    quality_attempts: tuple[dict, ...] = ()
    production_failures: tuple[dict, ...] = ()
    library_availability: tuple[dict, ...] = ()
    design_ref: ArtifactRef
    style_guide: Text
    summary: Text
    assets: tuple[ProducedAsset, ...]
    affected_scenes: tuple[Text, ...]
    limitations: tuple[Text, ...]
