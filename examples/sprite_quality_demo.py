"""Offline before/after review using saved real images and the shared pixel pipeline.

No .env, image generation, chat API, engine or hidden evaluation material is read.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from PIL import Image, ImageDraw

from adaptive_game_generation.models.asset_pipeline import AssetLayout
from adaptive_game_generation.providers.openai_image import OpenAICompatibleImageProvider
from adaptive_game_generation.roles.art_producer import ImageJob
from adaptive_game_generation.runtime.asset_normalization import generated_layout, normalize_pixels
from adaptive_game_generation.runtime.image_processing import remove_matte

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/sprite_quality"
LIBRARY = ROOT / "tests/fixtures/asset_normalization"


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def comparison(images, labels, destination):
    canvas = Image.new("RGB", (448 * len(images), 520), "#17212b")
    draw = ImageDraw.Draw(canvas)
    for index, (image, label) in enumerate(zip(images, labels)):
        x = index * 448
        draw.text((x + 12, 12), label, fill="white")
        draw.text((x + 12, 32), f"{image.width} x {image.height} pixels", fill="#b7c8d7")
        for y in range(64, 496, 16):
            for cx in range(8, 440, 16):
                draw.rectangle((x + cx, y, x + cx + 15, y + 15), fill="#8b929a" if ((y + cx) // 16) % 2 else "#c8cdd3")
        image = image.convert("RGBA")
        scale = min(424 / image.width, 424 / image.height)
        thumb = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.NEAREST)
        canvas.paste(thumb, (x + 8 + (432 - thumb.width) // 2, 64 + (432 - thumb.height) // 2), thumb)
    canvas.save(destination)


def run(output):
    output = Path(output)
    if output.exists():
        raise ValueError("Use a fresh output directory; saved evidence is never overwritten")
    output.mkdir(parents=True)
    source = json.loads((ROOT / "examples/designer_case/source.json").read_text(encoding="utf-8"))
    instruction = (ROOT / source["instruction_path"]).read_bytes()
    assert hashlib.sha256(instruction).hexdigest() == source["instruction_sha256"]
    (output / "instruction.md").write_bytes(instruction)
    write_json(output / "benchmark_source.json", source)
    inputs, outputs = [], []
    fixtures = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
    settings = SimpleNamespace(transparency="chroma_key", chroma_tolerance=40, resample="nearest")
    provider = SimpleNamespace(config=settings)
    for provenance in fixtures:
        name = provenance["name"]
        directory = output / name; directory.mkdir()
        original = (FIXTURES / (name + ".png")).read_bytes()
        assert hashlib.sha256(original).hexdigest() == provenance["sha256"]
        job = ImageJob.model_validate_json((FIXTURES / (name + ".job.json")).read_text(encoding="utf-8"))
        candidate, transforms = OpenAICompatibleImageProvider._normalize(provider, original, job)
        layout, logical_regions = generated_layout(job, transforms["output"]["width"], transforms["output"]["height"])
        info, frames, preview = normalize_pixels(candidate, layout, job.columns, job.rows, job.frame_names, check_quality=True)
        assert info["quality"]["status"] == "rejected" and not frames
        (directory / "source.png").write_bytes(original)
        shutil.copyfile(FIXTURES / (name + ".previous.png"), directory / "before.png")
        (directory / "candidate_after.png").write_bytes(candidate)
        (directory / "quality_preview.png").write_bytes(preview)
        keyed, key_report = remove_matte(Image.open(io.BytesIO(original)), mode="chroma_key")
        decoded = Image.open(io.BytesIO(candidate)).convert("RGBA")
        assert decoded.size == keyed.size and decoded.tobytes() == keyed.tobytes()
        pixel_check = {"native_dimensions": list(keyed.size), "logical_canvas": [job.width, job.height],
            "decoded_rgba_identical_after_matte": True, "changed_pixels_after_matte": 0,
            "resampled": False, "encoding": "lossless_png", "source_bytes": len(original), "output_bytes": len(candidate),
            "decoded_rgba_sha256": hashlib.sha256(decoded.tobytes()).hexdigest(),
            "logical_regions": [r.model_dump(mode="json") for r in logical_regions]}
        write_json(directory / "pixel_fidelity.json", pixel_check)
        keyed.save(directory / "matte_removed_full_resolution.png")
        (directory / "current_image_prompt.txt").write_text(OpenAICompatibleImageProvider._image_prompt(provider, job), encoding="utf-8")
        write_json(directory / "recorded_image_job.json", job.model_dump(mode="json"))
        write_json(directory / "processing.json", transforms)
        write_json(directory / "quality.json", info["quality"])
        write_json(directory / "provenance.json", provenance)
        (directory / "LICENSE.txt").write_text(provenance["license"], encoding="utf-8")
        comparison([Image.open(io.BytesIO(original)), Image.open(directory / "before.png"), Image.open(io.BytesIO(candidate))],
                   ["Recorded original", "Previous tiny output", "Native pixels: REJECTED for content"], directory / "comparison.png")
        inputs.append({"case": name, "source_sha256": provenance["sha256"], "image_job": job.model_dump(mode="json"),
                       "layout": layout.model_dump(mode="json"), "settings": vars(settings)})
        outputs.append({"case": name, "status": "rejected", "published_frames": 0,
            "pixel_fidelity": pixel_check,
            "matte_removed_pixels": key_report["removed_pixels"], "findings": info["quality"]["findings"],
            "transforms": transforms["operations"], "candidate_sha256": hashlib.sha256(candidate).hexdigest()})
    library_provenance = json.loads((LIBRARY / "provenance.json").read_text(encoding="utf-8"))
    for filename, kind in [("oga_font.png", "ui"), ("oga_scene_layer.png", "background")]:
        name = Path(filename).stem
        directory = output / name; directory.mkdir()
        original = (LIBRARY / filename).read_bytes()
        provenance = next(p for p in library_provenance if p["file"] == filename)
        assert hashlib.sha256(original).hexdigest() == provenance["sha256"]
        layout = AssetLayout(kind=kind, mode="whole")
        info, frames, preview = normalize_pixels(original, layout, 1, 1, [name], check_quality=True)
        before = Image.open(io.BytesIO(original)).convert("RGBA")
        after = Image.open(io.BytesIO(frames[0]["data"])).convert("RGBA")
        assert before.tobytes() == after.tobytes()
        (directory / "source.png").write_bytes(original)
        (directory / "after.png").write_bytes(frames[0]["data"])
        (directory / "quality_preview.png").write_bytes(preview)
        shutil.copyfile(LIBRARY / (filename + ".license.txt"), directory / "LICENSE.txt")
        write_json(directory / "provenance.json", provenance)
        write_json(directory / "quality.json", info["quality"])
        comparison([before, after], ["Real library source", "Correct asset type: unchanged RGBA"], directory / "comparison.png")
        # The same scenic sheet is rejected if incorrectly declared as an actor.
        wrong = None
        if kind == "background":
            wrong, rejected_frames, _ = normalize_pixels(original, AssetLayout(kind="sprite", mode="whole"), 1, 1, [name], check_quality=True)
            assert wrong["quality"]["status"] == "rejected" and not rejected_frames
            write_json(directory / "incorrect_actor_declaration.json", wrong["quality"])
        inputs.append({"case": name, "source_sha256": provenance["sha256"], "layout": layout.model_dump(mode="json")})
        outputs.append({"case": name, "status": info["quality"]["status"], "published_frames": len(frames),
                        "rgba_unchanged": True, "incorrect_actor_declaration_rejected": bool(wrong)})
    write_json(output / "inputs.json", inputs)
    write_json(output / "outputs.json", outputs)
    write_json(output / "summary.json", {"mode": "offline saved-real-image processing", "online_calls": 0,
        "original_sources_unchanged": all(hashlib.sha256((FIXTURES / (p["name"] + ".png")).read_bytes()).hexdigest() == p["sha256"] for p in fixtures),
        "cases": [{k: v for k, v in o.items() if k not in ("findings", "transforms")} for o in outputs],
        "semantic_acceptance": "not_performed", "limitations": "Conservative structural rejection, no OCR or semantic segmentation. Rejected scenes are not reconstructed into characters."})
    lines = ["# Native sprite fidelity and quality: saved real evidence", "",
        "No online calls. Original GameCraft-Bench instruction and source/license hashes are preserved.",
        "Generated candidates retain native dimensions. PNG decoded pixels equal the full-resolution matte-cleaned source exactly; no resampling.",
        "Matte removal intentionally changes background/edge pixels. Native PNG cannot restore loss already present in a JPEG/model source.",
        "After images are processing candidates, not approved artwork. Rejected candidates publish zero game frames.", ""]
    for result in outputs:
        name = result["case"]
        lines += [f"## {name}: {result['status']}", "", f"![Before and after]({name}/comparison.png)", "",
                  f"[Quality details]({name}/quality.json)", ""]
        if result.get("pixel_fidelity"):
            lines += [f"[Pixel equality proof]({name}/pixel_fidelity.json)", ""]
        if result["status"] == "rejected":
            lines += [f"![Rejected frames]({name}/quality_preview.png)", ""]
    lines += ["A legitimate scenic background and font keep their original pixels. Declaring the scenic layer as an actor is rejected.",
              "Generated sprites containing baked ground/cards/text are withheld, not repaired by deleting arbitrary subject pixels.",
              "Tests and separate controller traces cover the common executor, both library adapters, Art partial status, Developer packaging, Player replay and QA veto."]
    (output / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output.resolve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs/sprite-fidelity-review-001/evidence"))
    print(run(parser.parse_args().output))
