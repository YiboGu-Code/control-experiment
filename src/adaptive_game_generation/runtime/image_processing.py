"""Shared deterministic raster processing. No model, OCR, network or hidden inputs.

Geometry checks are conservative rejection heuristics, never semantic acceptance.
"""
import io
import json
from collections import deque

from PIL import Image, ImageChops, ImageDraw, ImageFilter

PROCESSING_KEY = "agg_asset_processing_v2"
PIPELINE_VERSION = "asset-pixels.v4"


def remove_matte(image, *, mode="auto", tolerance=40):
    """Key explicit generated mattes, or border-connected library mattes.

    Auto mode preserves existing usable alpha and never globally deletes purple
    subject colors. An inferred key needs a majority magenta border, not one corner.
    """
    image = image.convert("RGBA")
    alpha = image.getchannel("A")
    if mode == "preserve" or (mode == "auto" and alpha.getextrema()[0] < 250):
        return image, {"operation": "preserve_alpha", "removed_pixels": 0}
    w, h = image.size
    border = ([image.getpixel((x, y)) for x in range(w) for y in {0, h - 1}] +
              [image.getpixel((x, y)) for y in range(h) for x in {0, w - 1}])
    candidates = [p for p in border if p[3] >= 250 and min(p[0], p[2]) >= 128 and min(p[0], p[2]) - p[1] >= 64]
    keys = [(255, 0, 255)] if mode == "chroma_key" else []
    if len(candidates) >= len(border) * .6:
        sampled = tuple(sorted(p[c] for p in candidates)[len(candidates) // 2] for c in range(3))
        consistent = sum(max(abs(p[c] - sampled[c]) for c in range(3)) <= tolerance for p in candidates)
        if consistent >= len(border) * .6 and sampled not in keys:
            keys.append(sampled)
    if not keys:
        return image, {"operation": "preserve_alpha", "removed_pixels": 0, "reason": "No reliable magenta matte"}
    channels = image.split()[:3]
    def mask_for(key, limit):
        masks = [channel.point(lambda v, k=k: 255 if abs(v - k) <= limit else 0) for channel, k in zip(channels, key)]
        return ImageChops.multiply(ImageChops.multiply(masks[0], masks[1]), masks[2])
    mask = Image.new("L", image.size)
    for key in keys:
        mask = ImageChops.lighter(mask, mask_for(key, tolerance))
    if mode == "auto":
        # Flood each connected border component once; enclosed purple details survive.
        for x, y in ([(x, y) for x in range(w) for y in {0, h - 1}] +
                     [(x, y) for y in range(h) for x in {0, w - 1}]):
            if mask.getpixel((x, y)) == 255:
                ImageDraw.floodfill(mask, (x, y), 128)
        mask = mask.point(lambda v: 255 if v == 128 else 0)
    removed = ImageChops.multiply(mask, alpha).histogram()
    count = sum(removed[1:])
    image.paste((0, 0, 0, 0), mask=mask)
    # Limit soft keying to immediate neighbors of the removed matte. Do not
    # desaturate a character's interior or manufacture alpha across opaque scenes.
    edge = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(3)), mask)
    pixels, edges = image.load(), edge.load()
    softened = 0
    for y in range(h):
        for x in range(w):
            if not edges[x, y]:
                continue
            r, g, b, a = pixels[x, y]
            if not a or min(r, b) - g < 40:
                continue
            key = min(keys, key=lambda k: max(abs(v - c) for v, c in zip((r, g, b), k)))
            distance = max(abs(v - c) for v, c in zip((r, g, b), key))
            if tolerance < distance < max(tolerance * 2, 1):
                fraction = (distance - tolerance) / max(tolerance, 1)
                rgb = tuple(max(0, min(255, round((v - c * (1 - fraction)) / fraction))) for v, c in zip((r, g, b), key))
                pixels[x, y] = (*rgb, round(a * fraction))
                softened += 1
    return image, {"operation": "chroma_key", "mode": mode, "tolerance": tolerance,
                   "keys": ["#%02X%02X%02X" % k for k in keys],
                   "removed_pixels": count, "softened_edge_pixels": softened}


def sprite_findings(image, name, *, check_matte=False, kind="sprite"):
    """Bounded structural checks. Suspect cards, bases and glyphs are quarantined.

    This cannot prove the absence of baked scenery/text. Actual visual acceptance
    remains with Developer's preview and QA; no OCR result is fabricated.
    """
    image = image.convert("RGBA")
    image.thumbnail((192, 192), Image.Resampling.NEAREST)
    w, h = image.size
    mask = image.getchannel("A").point(lambda a: 255 if a > 32 else 0)
    bbox = mask.getbbox()
    codes = []
    if bbox is None:
        return [{"frame": name, "code": "empty_sprite", "message": "No visible subject remains."}]
    visible = mask.histogram()[255]
    x0, y0, x1, y1 = bbox
    bw, bh = x1 - x0, y1 - y0
    fill = visible / (bw * bh)
    if visible / (w * h) > .98:
        codes.append(("opaque_sprite", "No usable transparent space around the actor; background may be baked in."))
    if bw >= 8 and bh >= 8 and fill > .88:
        codes.append(("rectangular_panel", "Foreground nearly fills a rectangle; possible card, scenery or background panel."))
    values = mask.load()
    base_rows = sum(sum(bool(values[x, y]) for x in range(x0, x1)) >= bw * .88
                    for y in range(y0 + int(bh * .72), y1))
    if bw >= 12 and bh >= 12 and base_rows >= max(3, int(bh * .12)):
        codes.append(("wide_base", "Wide solid band under the subject; possible ground/platform or caption panel."))
    edge_pixels = [values[x, y] for x in range(w) for y in {0, h - 1}] + [values[x, y] for y in range(h) for x in {0, w - 1}]
    if sum(bool(v) for v in edge_pixels) > .25 * len(edge_pixels):
        codes.append(("clipped_or_background", "Too much foreground touches cell boundaries; inspect frame layout/background."))
    remaining = {(x, y) for y in range(h) for x in range(w) if values[x, y]}
    components = []
    for seed in sorted(remaining, key=lambda p: (p[1], p[0])):
        if seed not in remaining:
            continue
        remaining.remove(seed)
        queue, count = deque([seed]), 0
        left = right = seed[0]
        top = bottom = seed[1]
        while queue:
            x, y = queue.popleft()
            count += 1
            left, right, top, bottom = min(left, x), max(right, x), min(top, y), max(bottom, y)
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
                p = x + dx, y + dy
                if p in remaining:
                    remaining.remove(p)
                    queue.append(p)
        if count >= 2:
            components.append((count, right - left + 1, bottom - top + 1))
    if any(cw >= 12 and ch >= 12 and count / (cw * ch) > .90 for count, cw, ch in components):
        codes.append(("solid_component_panel", "A foreground component nearly fills a rectangle; possible scenic/presentation panel."))
    largest = max((c[0] for c in components), default=0)
    if any(20 < count < largest * .6 and cw > w * .2 and cw > ch * 2.5 for count, cw, ch in components):
        codes.append(("caption_or_extra_object", "A separate wide foreground component resembles a caption, base or extra object."))
    if len(components) >= 7:
        codes.append(("text_or_fragments", "Many disconnected foreground fragments; possible labels, multiple subjects or debris."))
    purple = sum(a > 32 and min(r, b) > 150 and min(r, b) - g > 95 for r, g, b, a in image.getdata())
    if check_matte and purple > max(4, visible * .08):
        codes.append(("magenta_residue", "Substantial visible magenta remains; inspect matte or legitimate subject colors."))
    # Solid rectangles, boundary contact and bases are expected terrain/prop
    # geometry. This is a declared-use profile, not semantic image approval.
    if kind in ("tile", "tile_set", "prop"):
        allowed = {"magenta_residue", "text_or_fragments"} if kind == "prop" else {"magenta_residue"}
        codes = [(code, message) for code, message in codes if code in allowed]
    return [{"frame": name, "code": code, "message": message} for code, message in codes]


def contained(image, size, *, resample="nearest", transparent=True, pivot=(.5, .5)):
    """One scalar factor, integer-rounded dimensions, pivot-aligned padding; never stretch."""
    factor = min(size[0] / image.width, size[1] / image.height)
    dimensions = (max(1, min(size[0], round(image.width * factor))), max(1, min(size[1], round(image.height * factor))))
    resized = image.resize(dimensions, getattr(Image.Resampling, resample.upper()))
    fill = (0, 0, 0, 0) if transparent else image.convert("RGB").getpixel((0, 0))
    output = Image.new("RGBA" if transparent else "RGB", size, fill)
    offset = (int((size[0] - dimensions[0]) * pivot[0]), int((size[1] - dimensions[1]) * pivot[1]))
    output.paste(resized, offset)  # Copy alpha; using it as mask would square alpha.
    return output, {"scale": factor, "resized": dimensions, "padding_offset": offset}


def prepare_generated(source, job, *, transparency="chroma_key", tolerance=40, resample="nearest"):
    """Preserve native decoded pixels: matte cleanup + lossless PNG, no resizing.

    resample remains accepted for older callers/configurations but is deliberately
    unused. Logical display dimensions belong to the game, not this pixel pipeline.
    """
    from .asset_normalization import generated_layout, MAX_FRAME_PIXELS
    from .media import raster_info
    details = raster_info(source)
    if source.startswith(b"\x89PNG\r\n\x1a\n") and source[24] > 8:
        raise ValueError("High-bit-depth PNG needs an explicit preservation pipeline; refusing silent conversion to 8-bit")
    layout, _ = generated_layout(job, details["width"], details["height"])
    if sum(r.width * r.height for r in layout.regions) > MAX_FRAME_PIXELS:
        raise ValueError("Generated source frame pixel budget exceeded")
    with Image.open(io.BytesIO(source)) as original:
        image = original.convert("RGBA" if job.transparent_background else "RGB")
    operations, findings = [], []
    if job.transparent_background:
        image, operation = remove_matte(image, mode=transparency if transparency == "chroma_key" else "preserve", tolerance=tolerance)
        operations.append(operation)
        lo, hi = image.getchannel("A").getextrema()
        if lo >= 250 or hi == 0:
            # Explicit layouts go through quarantine and bounded Art feedback.
            # Legacy consumers without normalization must still fail closed.
            if job.layout is None:
                raise ValueError("Generated image has no usable transparency; inspect source image and ART_IMAGE_TRANSPARENCY")
            findings.append({"frame": "source", "code": "missing_transparency" if hi else "empty_sprite",
                "message": "Requested transparent subject is opaque or empty. Repair the source/background; no usable frames will be published."})
    checked = layout.kind in ("sprite", "sprite_sheet", "tile", "tile_set", "prop")
    for r in layout.regions:
        rect = (r.x, r.y, r.x + r.width, r.y + r.height)
        if checked:
            findings.extend(sprite_findings(image.crop(rect), r.name,
                check_matte=job.transparent_background and transparency == "chroma_key", kind=layout.kind))
        operations.append({"operation": "frame_native", "frame": r.name, "source_rect": rect,
                           "target_rect": (r.x, r.y, r.width, r.height), "scale": 1,
                           "resampled": False})
    report = {"pipeline": PIPELINE_VERSION, "operations": operations, "findings": findings,
              "geometry": "source_native", "encoding": "lossless_png", "resampled": False,
              "requested_display_canvas": [job.width, job.height], "native_canvas": list(image.size),
              "semantic_review": "not_performed"}
    return image, report


def upstream_findings(image):
    """Embedded diagnostics may only reject; they never authorize an asset."""
    raw = image.info.get(PROCESSING_KEY)
    if not raw:
        return []
    try:
        if len(raw) > 65536:
            raise ValueError("Oversized report")
        result = json.loads(raw)
        findings = result["findings"]
        if not isinstance(findings, list) or len(findings) > 512:
            raise ValueError("Invalid report")
        if any(not isinstance(f, dict) or set(f) != {"frame", "code", "message"} or
               any(not isinstance(v, str) or not v.strip() or len(v) > 512 for v in f.values()) for f in findings):
            raise ValueError("Invalid finding")
        return findings
    except (ValueError, KeyError, TypeError):
        return [{"frame": "source", "code": "invalid_processing_report", "message": "Malformed source processing diagnostics require review."}]
