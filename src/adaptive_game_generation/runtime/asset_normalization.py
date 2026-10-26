"""Deterministic pixel crops, bounded atlas metadata, and review contact sheets.

This module does not infer semantic correctness or call an image/model service.
"""
import hashlib
import io
import json
import re
from pathlib import PurePosixPath
from xml.etree import ElementTree

from PIL import Image, ImageDraw

from ..models.asset_pipeline import AssetLayout, AtlasMetadata, FrameRegion
from .media import raster_info

MAX_FRAME_PIXELS = 16_777_216


def generated_layout(job, width, height):
    """Map declared cell boundaries to native pixels; never resample the image.

    Round shared boundaries, not independent widths, so odd dimensions cannot
    introduce seams or overlaps. Pivots/timing survive as normalized metadata.
    """
    if not (1 <= width <= 4096 and 1 <= height <= 4096):
        raise ValueError("Native image exceeds the 4096-pixel dimension limit; request a supported smaller canvas, never silently downsample")
    layout = job.layout or AssetLayout(
        kind="sprite_sheet" if job.columns * job.rows > 1 else "sprite" if job.transparent_background else "background",
        mode="grid", cell_width=job.width // job.columns, cell_height=job.height // job.rows)
    original = regions_for(layout, job.width, job.height, job.columns, job.rows, job.frame_names)
    if not original:
        return layout, original
    mapped = []
    for r in original:
        x0, y0 = round(r.x * width / job.width), round(r.y * height / job.height)
        x1, y1 = round((r.x + r.width) * width / job.width), round((r.y + r.height) * height / job.height)
        if x1 <= x0 or y1 <= y0:
            raise ValueError("Generated canvas is too small for the declared frame layout")
        mapped.append(r.revised(x=x0, y=y0, width=x1 - x0, height=y1 - y0))
    native = AssetLayout(kind=layout.kind, mode="regions", regions=tuple(mapped))
    regions_for(native, width, height, job.columns, job.rows, job.frame_names)
    return native, original


def regions_for(layout, width, height, columns, rows, names):
    layout = AssetLayout.model_validate(layout)
    if layout.mode == "unresolved":
        return ()
    if layout.mode == "catalog":
        raise ValueError("Resolve catalog names against the selected library metadata before normalization")
    if layout.mode == "whole":
        if len(names) != 1 or columns != 1 or rows != 1:
            raise ValueError("Whole textures require one frame and a 1 x 1 declaration")
        regions = (FrameRegion(name=names[0], x=0, y=0, width=width, height=height),)
    elif layout.mode == "regions":
        regions = layout.regions
        if tuple(r.name for r in regions) != tuple(names):
            raise ValueError("Explicit regions must match frame_names in order")
    else:
        if not 1 <= columns * rows <= 64 or len(names) != columns * rows:
            raise ValueError("Grid frame count must match frame_names and remain within 64")
        regions = tuple(FrameRegion(name=name,
            x=layout.margin_x + (i % columns) * (layout.cell_width + layout.spacing_x),
            y=layout.margin_y + (i // columns) * (layout.cell_height + layout.spacing_y),
            width=layout.cell_width, height=layout.cell_height) for i, name in enumerate(names))
    if not 1 <= len(regions) <= 64 or len({r.name.casefold() for r in regions}) != len(regions):
        raise ValueError("Frame names must be unique on case-insensitive filesystems; at most 64 frames")
    for r in regions:
        if r.x + r.width > width or r.y + r.height > height:
            raise ValueError(f"Frame {r.name} lies outside the source image")
        if layout.kind in ("sprite_sheet", "tile_set", "source_atlas") and (r.width, r.height) == (width, height):
            raise ValueError("An atlas frame cannot cover the entire source; declare actual subregions or unresolved")
    if sum(r.width * r.height for r in regions) > MAX_FRAME_PIXELS:
        raise ValueError("Cropped pixel budget exceeded")
    return regions


def png(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def normalize_pixels(data, layout, columns, rows, names, *, check_quality=False, is_mock=False):
    from .image_processing import remove_matte, sprite_findings, upstream_findings, PIPELINE_VERSION
    info = raster_info(data)
    layout = AssetLayout.model_validate(layout)
    regions = regions_for(layout, info["width"], info["height"], columns, rows, names)
    with Image.open(io.BytesIO(data)) as original:
        source = original.convert("RGBA")
        findings = upstream_findings(original) if regions and check_quality and not is_mock else []
    checked = layout.kind in ("sprite", "sprite_sheet", "tile", "tile_set", "prop")
    operations = []
    frames = []
    # Fixed-size cells preserve aspect ratio and show alpha against a checkerboard.
    preview = Image.new("RGB", (4 * 192, max(1, (len(regions) + 3) // 4) * 168 + 28), "#202b35")
    draw = ImageDraw.Draw(preview)
    draw.text((8, 7), "Candidate frames - structural checks only; semantic review pending", fill="white")
    for i, r in enumerate(regions):
        crop = source.crop((r.x, r.y, r.x + r.width, r.y + r.height))
        if check_quality and not is_mock and checked:
            # Opaque terrain fills its cell deliberately. A magenta tile may be
            # legitimate terrain color, not a matte. Explicit generated mattes
            # have already been processed by the provider with audited findings.
            crop, operation = remove_matte(crop, mode="preserve" if layout.kind in ("tile", "tile_set") else "auto")
            operations.append({"frame": r.name, **operation})
            findings.extend(sprite_findings(crop, r.name, check_matte=operation["operation"] == "chroma_key", kind=layout.kind))
        hist = crop.getchannel("A").histogram()
        frames.append({"region": r, "data": png(crop), "transparent_pixels": hist[0],
                       "visible_pixels": r.width * r.height - hist[0]})
        x, y = (i % 4) * 192, (i // 4) * 168 + 28
        for cy in range(0, 128, 16):
            for cx in range(0, 176, 16):
                draw.rectangle((x + 8 + cx, y + 4 + cy, x + 23 + cx, y + 19 + cy),
                               fill="#67717c" if (cx + cy) // 16 % 2 else "#aab2ba")
        factor = min(176 / crop.width, 128 / crop.height)
        thumb = crop.resize((max(1, int(crop.width * factor)), max(1, int(crop.height * factor))), Image.Resampling.NEAREST)
        preview.paste(thumb, (x + 8 + (176 - thumb.width) // 2, y + 4 + (128 - thumb.height) // 2), thumb)
        draw.text((x + 8, y + 136), f"{i + 1}: {r.name[:24]}", fill="white")
        draw.text((x + 8, y + 150), f"{r.x},{r.y} {r.width}x{r.height}", fill="#c7dbe9")
    if not regions:
        draw.text((8, 64), "UNRESOLVED SOURCE ATLAS - no display-ready frames", fill="#ffd080")
    if check_quality:
        unique = {(f["frame"], f["code"]): f for f in findings}
        info["quality"] = {"pipeline": PIPELINE_VERSION,
            "status": "rejected" if unique else "mock_not_reviewed" if is_mock else "checks_passed" if checked else "not_applicable",
            "findings": list(unique.values()), "operations": operations, "semantic_review": "not_performed"}
        if unique:
            draw.rectangle((0, 0, preview.width, 27), fill="#6b1320")
            draw.text((8, 7), "REJECTED - diagnostic preview only; no game textures published", fill="white")
            frames = []
    return info, frames, png(preview)


def parse_atlas_metadata(data, path, image_name):
    """Only non-rotated, untrimmed TexturePacker JSON or Kenney-style XML."""
    identity = {"path": path, "sha256": hashlib.sha256(data).hexdigest()}
    try:
        if len(data) > 256 * 1024 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise ValueError("Oversized metadata or XML entities are unsupported")
        entries = []
        if path.lower().endswith(".xml"):
            root = ElementTree.fromstring(data)
            if root.tag != "TextureAtlas" or PurePosixPath(root.attrib.get("imagePath", "")).name != image_name:
                raise ValueError("Atlas metadata must identify this image")
            for node in root.findall("SubTexture"):
                a = node.attrib
                if any(k in a for k in ("frameX", "frameY", "frameWidth", "frameHeight")) or a.get("rotated", "false").lower() != "false":
                    raise ValueError("Trimmed or rotated frames need explicit restoration and are unsupported")
                entries.append((a["name"], {k: int(a[k]) for k in ("x", "y", "width", "height")}))
        else:
            document = json.loads(data)
            if PurePosixPath(document.get("meta", {}).get("image", "")).name != image_name:
                raise ValueError("Atlas metadata must identify this image")
            raw = document["frames"]
            iterable = raw.items() if isinstance(raw, dict) else ((r["filename"], r) for r in raw)
            for name, entry in iterable:
                if entry.get("rotated") or entry.get("trimmed"):
                    raise ValueError("Trimmed or rotated frames are unsupported")
                rect = entry["frame"]
                original = entry.get("sourceSize", {"w": rect["w"], "h": rect["h"]})
                placement = entry.get("spriteSourceSize", {"x": 0, "y": 0, "w": rect["w"], "h": rect["h"]})
                if (original["w"], original["h"], placement["x"], placement["y"], placement["w"], placement["h"]) != (rect["w"], rect["h"], 0, 0, rect["w"], rect["h"]):
                    raise ValueError("Trimmed frame restoration is unsupported even when trimmed is omitted")
                pivot = entry.get("pivot", {"x": 0.5, "y": 0.5})
                entries.append((name, {"x": rect["x"], "y": rect["y"], "width": rect["w"], "height": rect["h"],
                    "pivot_x": pivot["x"], "pivot_y": pivot["y"], "duration_ms": entry.get("duration", 100)}))
        if not entries:
            raise ValueError("Atlas contains no frames")
        regions, used = [], set()
        for name, rect in entries[:64]:
            clean = re.sub(r"[^A-Za-z0-9_-]", "_", str(name)).strip("_-") or "frame"
            clean = clean[:48] + "_" + hashlib.sha256(str(name).encode()).hexdigest()[:8]
            if clean.casefold() in used:
                raise ValueError("Duplicate metadata frame name")
            used.add(clean.casefold())
            regions.append(FrameRegion(name=clean, **rect))
        return AtlasMetadata(**identity, status="available", regions=tuple(regions), truncated=len(entries) > 64)
    except (ValueError, KeyError, TypeError, AttributeError, ElementTree.ParseError):
        return AtlasMetadata(**identity, status="unsupported", reason="Metadata is malformed, references another image, or uses unsupported rotated/trimmed frames. No coordinates were inferred.")
