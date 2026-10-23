"""Offline review of saved real assets through the metered normalization capability.

No .env, model, image-generation service, engine or hidden evaluator is accessed.
The layout requests are explicit review fixtures, not new model responses.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
from pathlib import Path
import shutil

from PIL import Image, ImageDraw

from adaptive_game_generation.models import ArtifactRef, ResourceLimits, RoleResult
from adaptive_game_generation.models.asset_pipeline import AssetLayout, FrameRegion
from adaptive_game_generation.roles.developer.assets import asset_reference_findings, asset_package_findings
from adaptive_game_generation.runtime import Executor, PublicResource, RuntimeConfig, Workspace

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/asset_normalization"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def review_cases():
    return [
        {"file": "generated_player.png", "asset_id": "recorded_player", "columns": 4, "rows": 1,
         "names": ["idle", "run", "jump", "fall"],
         "layout": AssetLayout(kind="sprite_sheet", mode="grid", cell_width=32, cell_height=32),
         "observation": "Recorded real generated image. The old declared four-cell grid is tested geometrically only. Tiny scene/poster content remains unsuitable as clean actor animation; slicing does not fix it."},
        {"file": "oga_font.png", "asset_id": "recorded_font", "columns": 1, "rows": 1,
         "names": ["digit_0", "digit_1"], "layout": AssetLayout(kind="tile_set", mode="regions", regions=(
             FrameRegion(name="digit_0", x=0, y=12, width=8, height=12),
             FrameRegion(name="digit_1", x=8, y=12, width=8, height=12))),
         "observation": "Recorded real OpenGameArt bitmap font. Explicit glyph rectangles preserve original alpha and pixels; the original license is retained."},
        {"file": "oga_scene_layer.png", "asset_id": "recorded_cavern", "columns": 1, "rows": 1,
         "names": ["ledge_fragment", "empty_probe"], "layout": AssetLayout(kind="source_atlas", mode="regions", regions=(
             FrameRegion(name="ledge_fragment", x=128, y=144, width=32, height=16, pivot_y=0),
             FrameRegion(name="empty_probe", x=0, y=48, width=32, height=16))),
         "observation": "The old asset named platform_atlas is a composed scene layer. A hand-selected ledge fragment demonstrates exact cropping, not a complete reusable tileset. The deliberately empty rectangle is a negative review probe."},
    ]


async def run(output):
    if output.exists():
        raise ValueError("Use a new output directory; previous evidence is never overwritten")
    provenance = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
    source = json.loads((ROOT / "examples/designer_case/source.json").read_text(encoding="utf-8"))
    instruction_path = ROOT / source["instruction_path"]
    instruction = instruction_path.read_bytes()
    if hashlib.sha256(instruction).hexdigest() != source["instruction_sha256"]:
        raise ValueError("Pinned Benchmark instruction changed")
    before = {p["file"]: hashlib.sha256((FIXTURES / p["file"]).read_bytes()).hexdigest() for p in provenance}
    if any(before[p["file"]] != p["sha256"] for p in provenance):
        raise ValueError("Recorded real source bytes changed")
    output.mkdir(parents=True)
    shutil.copytree(FIXTURES, output / "recorded_sources")
    (output / "instruction.md").write_bytes(instruction)
    workspace, task = Workspace.prepare(output / "workspace", instruction_path.parent,
        task_id=source["task_id"], benchmark_snapshot=source["snapshot"],
        resources=(PublicResource("recorded", output / "recorded_sources"),))
    limits = ResourceLimits(max_model_calls=0, max_image_calls=0, max_tool_calls=500)
    executor = Executor.create(workspace, task, run_id="offline-asset-review", controller="fixed_loop",
        budget=limits, config=RuntimeConfig(role_limits=limits))
    packages, inputs = [], []

    async def normalize(context):
        refs = []
        for case in review_cases():
            relative = "input/resources/recorded/" + case["file"]
            data = await context.tools.read_bytes(relative)
            path = await context.tools.write_bytes("sources/" + case["file"], data)
            ref = ArtifactRef(artifact_id="source-" + case["asset_id"], kind="asset", path=path,
                created_by="art_producer", media_type="image/png", sha256=hashlib.sha256(data).hexdigest())
            refs.append(ref)
            request = {"asset_id": case["asset_id"], "source_target": "assets/" + case["file"],
                "layout": case["layout"].model_dump(mode="json"), "columns": case["columns"],
                "rows": case["rows"], "names": case["names"]}
            inputs.append({"source_ref": ref.model_dump(mode="json"), **request})
            package = await context.tools.normalize_asset(ref, **request)
            packages.append(package)
            refs.extend(package.artifact_refs)
        return RoleResult(request_id=context.request.request_id, role="art_producer",
            state_revision=context.request.state_revision, status="partial",
            summary="Offline deterministic crop capability probe with saved real images; no model call or visual acceptance.",
            artifact_refs=tuple(refs))

    request = executor.request("art_producer", "Validate saved real asset cropping without network access",
        ("Preserve source pixels and original instruction; expose review limitations",))
    result = await executor.execute(request, normalize)
    write_json(output / "role_request.json", request.model_dump(mode="json"))
    write_json(output / "role_result.json", result.model_dump(mode="json"))
    write_json(output / "tool_inputs.json", inputs)
    if result.status != "partial":
        raise RuntimeError(f"Offline crop probe failed: {result.error}")
    checks, summaries = [], []
    for case, package in zip(review_cases(), packages):
        folder = output / case["asset_id"]
        folder.mkdir()
        (folder / "source.png").write_bytes((FIXTURES / case["file"]).read_bytes())
        (folder / "contact_sheet.png").write_bytes(workspace.read_public(package.preview_ref.path).read_bytes())
        write_json(folder / "manifest.json", package.model_dump(mode="json"))
        source_image = Image.open(folder / "source.png").convert("RGBA")
        annotated = source_image.copy()
        draw = ImageDraw.Draw(annotated)
        exact = []
        for frame in package.frames:
            data = workspace.read_public(frame.artifact_ref.path).read_bytes()
            (folder / (frame.region.name + ".png")).write_bytes(data)
            r = frame.region
            rect = (r.x, r.y, r.x + r.width, r.y + r.height)
            equal = Image.open(io.BytesIO(data)).convert("RGBA").tobytes() == source_image.crop(rect).tobytes()
            exact.append(equal)
            draw.rectangle((r.x, r.y, r.x + r.width - 1, r.y + r.height - 1), outline="#ffcf30", width=1)
        annotated.save(folder / "source_regions.png")
        bad = f'extends Node2D\nvar sprite = preload("res://{package.source_target_path}")\n'
        good = (f'extends Node2D\nvar sprite = preload("res://{package.frames[0].target_path}")\n'
                if package.frames else "extends Node2D\n# Quarantined asset: request replacement; no texture is available.\n")
        (folder / "rejected_reference.gd").write_text(bad, encoding="utf-8")
        (folder / "accepted_reference.gd").write_text(good, encoding="utf-8")
        bad_findings = asset_reference_findings({"actor.gd": bad}, [package])
        good_findings = asset_reference_findings({"actor.gd": good}, [package])
        renamed = asset_package_findings({"assets/renamed.png": package.source_ref.sha256}, [package])
        correct = asset_package_findings({f.target_path: f.artifact_ref.sha256 for f in package.frames}, [package])
        check = {"asset_id": package.asset_id, "status": package.status,
                 "exact_rgba_crop": all(exact) if exact else None, "full_source_reference": bad_findings,
                 "derived_reference": good_findings, "renamed_atlas_copy": renamed, "derived_package": correct}
        checks.append(check)
        summaries.append({"asset_id": package.asset_id, "observation": case["observation"],
                          "frames": len(package.frames), "empty_frames": [f.region.name for f in package.frames if not f.visible_pixels]})
        assert all(exact) and bad_findings and not good_findings and renamed and not correct
    unchanged = before == {p["file"]: hashlib.sha256((FIXTURES / p["file"]).read_bytes()).hexdigest() for p in provenance}
    write_json(output / "checks.json", checks)
    write_json(output / "summary.json", {"mode": "offline saved-real-image validation; explicit layout fixtures; no model or engine call",
        "source": source, "original_fixture_bytes_unchanged": unchanged, "cases": summaries,
        "usage": executor.state.ledger.usage.model_dump(mode="json"), "visual_acceptance": "not_performed"})
    write_json(output / "state_after.json", executor.state.model_dump(mode="json"))
    lines = ["# Saved real asset normalization review", "",
        "Offline crop and misuse-check evidence. No online models, image generation or engine execution.",
        "The original GameCraft-Bench instruction is in instruction.md. Full tool requests/results, source provenance, licenses and metered logs are saved here.", ""]
    for item in summaries:
        identity = item["asset_id"]
        lines += [f"## {identity}", "", item["observation"], "",
            f"![Source with selected rectangles]({identity}/source_regions.png)", "",
            f"![Actual normalized frames]({identity}/contact_sheet.png)", ""]
    lines += ["Accepted library crops match original RGBA pixels. Suspect actor sources are quarantined and publish no frames. Full-source literal references and renamed atlas copies were rejected; available derived frame references were accepted.",
              "These checks do not certify a game's animation, appearance, or Benchmark requirements. Inspect contact sheets and the actual runtime preview before acceptance.", ""]
    (output / "review.md").write_text("\n".join(lines), encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs/asset-normalization-real-assets-001"))
    args = parser.parse_args()
    print(asyncio.run(run(args.output)).resolve())
