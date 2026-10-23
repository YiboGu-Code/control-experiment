"""Public, controller-independent asset layout and derived-frame contracts."""
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .artifacts import ArtifactRef
from .base import Model, Text, require_unique

FrameName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
Coordinate = Annotated[int, Field(strict=True, ge=0, le=16384)]
Extent = Annotated[int, Field(strict=True, ge=1, le=4096)]


class FrameRegion(Model):
    name: FrameName
    x: Coordinate
    y: Coordinate
    width: Extent
    height: Extent
    pivot_x: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    pivot_y: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    animation: FrameName | None = None
    duration_ms: int = Field(default=100, strict=True, ge=1, le=10000)


class AssetLayout(Model):
    kind: Literal["sprite", "sprite_sheet", "tile_set", "background", "ui", "source_atlas"]
    mode: Literal["whole", "grid", "regions", "catalog", "unresolved"]
    cell_width: Extent | None = None
    cell_height: Extent | None = None
    margin_x: Coordinate = 0
    margin_y: Coordinate = 0
    spacing_x: Coordinate = 0
    spacing_y: Coordinate = 0
    regions: Annotated[tuple[FrameRegion, ...], Field(max_length=64)] = ()

    @model_validator(mode="after")
    def consistent(self):
        atlas = self.kind in ("sprite_sheet", "tile_set", "source_atlas")
        if atlas and self.mode == "whole":
            raise ValueError("An atlas cannot be a whole display texture; choose grid, regions, catalog or unresolved")
        if self.mode == "grid":
            if self.cell_width is None or self.cell_height is None or self.regions:
                raise ValueError("Grid layout needs cell_width/cell_height and no explicit regions")
        elif any((self.cell_width, self.cell_height, self.margin_x, self.margin_y, self.spacing_x, self.spacing_y)):
            raise ValueError("Cell dimensions, margins and spacing apply only to grid layouts")
        if bool(self.regions) != (self.mode == "regions"):
            raise ValueError("Only regions mode requires explicit frame rectangles")
        require_unique((r.name.casefold() for r in self.regions), "frame names")
        return self


class AtlasMetadata(Model):
    path: Text
    sha256: Text
    status: Literal["available", "unsupported"]
    regions: tuple[FrameRegion, ...] = ()
    truncated: bool = False
    reason: str | None = None


class NormalizedFrame(Model):
    region: FrameRegion
    artifact_ref: ArtifactRef
    target_path: Text
    transparent_pixels: int = Field(ge=0)
    visible_pixels: int = Field(ge=0)
    # Optional logical display bounds; the texture itself keeps source pixels.
    display_size: tuple[Extent, Extent] | None = None


class AssetQualityFinding(Model):
    frame: Text
    code: Text
    message: Text


class AssetQualityReport(Model):
    pipeline: Literal["asset-pixels.v2", "asset-pixels.v3"] = "asset-pixels.v3"
    status: Literal["checks_passed", "rejected", "not_applicable", "mock_not_reviewed"]
    findings: tuple[AssetQualityFinding, ...] = ()
    operations: tuple[dict, ...] = ()
    semantic_review: Literal["not_performed"] = "not_performed"

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "rejected") != bool(self.findings):
            raise ValueError("Quality findings must quarantine the asset")
        return self


class NormalizedAsset(Model):
    protocol: Literal["asset-normalization.v1"] = "asset-normalization.v1"
    asset_id: FrameName
    source_ref: ArtifactRef
    source_target_path: Text
    source_width: Extent
    source_height: Extent
    layout: AssetLayout
    layout_source: AtlasMetadata | None = None
    source_only: bool
    status: Literal["materialized", "unresolved", "rejected"]
    frames: tuple[NormalizedFrame, ...] = ()
    preview_ref: ArtifactRef
    visual_review: Literal["not_performed"] = "not_performed"
    limitations: tuple[Text, ...]
    quality: AssetQualityReport | None = None

    @model_validator(mode="after")
    def consistent(self):
        if not self.source_only or (self.status == "unresolved") != (self.layout.mode == "unresolved"):
            raise ValueError("Normalized sources must be source-only with a consistent status")
        if bool(self.frames) != (self.status == "materialized") or len(self.frames) > 64:
            raise ValueError("Materialized packages require 1..64 frames; unresolved sources have none")
        if (self.status == "rejected") != bool(self.quality and self.quality.status == "rejected"):
            raise ValueError("Rejected quality must withhold display-ready frames")
        require_unique((f.region.name.casefold() for f in self.frames), "normalized frames")
        for frame in self.frames:
            r = frame.region
            if frame.target_path != f"assets/normalized/{self.asset_id}/{r.name}.png":
                raise ValueError("Derived frames must use the reserved asset namespace")
            if r.x + r.width > self.source_width or r.y + r.height > self.source_height:
                raise ValueError("Derived frame exceeds source bounds")
            if frame.visible_pixels + frame.transparent_pixels != r.width * r.height:
                raise ValueError("Frame pixel counts disagree with its dimensions")
        return self

    @property
    def artifact_refs(self):
        return tuple(f.artifact_ref for f in self.frames) + (self.preview_ref,)
