"""Host assembly of motion resources from verified normalized Art frames."""
import json

from ..art_producer.motion import motion_quality


def animation_resources(art):
    if not art:
        return {}, []
    frames = {f"{a.request.asset_id}/{f.region.name}": f for a in art.assets if a.normalization
              for f in a.normalization.frames}
    invalid = {f["asset_id"] for f in motion_quality(art.assets)}
    files, contracts = {}, []
    for asset in art.assets:
        motion = asset.request.motion
        if not motion:
            continue
        record = {"asset_id": asset.request.asset_id, **motion.model_dump(mode="json"),
                  "resource_path": None, "status": "pending" if asset.request.asset_id in invalid else "available",
                  "runtime_verified": False}
        if motion.mode == "frames" and asset.request.asset_id not in invalid:
            names = list(dict.fromkeys(key for clip in motion.clips for key in clip.frames))
            lookup = {key: str(i + 1) for i, key in enumerate(names)}
            lines = [f'[gd_resource type="SpriteFrames" load_steps={len(names) + 1} format=3]', ""]
            lines += [f'[ext_resource type="Texture2D" path="res://{frames[key].target_path}" id="{lookup[key]}"]' for key in names]
            clips = []
            for clip in motion.clips:
                entries = ', '.join('{"duration": 1.0, "texture": ExtResource(' + json.dumps(lookup[key]) + ')}' for key in clip.frames)
                clips.append('{"name": &' + json.dumps(clip.name) + ', "speed": ' + str(float(clip.fps)) +
                             ', "loop": ' + str(clip.loop).lower() + ', "frames": [' + entries + ']}')
            lines += ["", "[resource]", "animations = [" + ",\n".join(clips) + "]", ""]
            record["resource_path"] = f"assets/animations/{asset.request.asset_id}.tres"
            files[record["resource_path"]] = "\n".join(lines).encode()
            record["frame_geometry"] = {key: {"native_size": [frames[key].region.width, frames[key].region.height],
                "pivot": [frames[key].region.pivot_x, frames[key].region.pivot_y]} for key in names}
        contracts.append(record)
    if contracts:
        files["assets/animations/motion_contracts.json"] = (json.dumps(contracts, indent=2) + "\n").encode()
    return files, contracts


def animation_integration_findings(files, contracts):
    """Unused host frame resources are conclusive; runtime behavior still needs play."""
    all_source = "\n".join(line for path, text in files.items() if path.endswith((".gd", ".tscn", ".tres"))
                           for line in text.splitlines() if not line.lstrip().startswith(("#", ";", "//")))
    findings = []
    for contract in contracts:
        path = contract["resource_path"]
        if path and path not in all_source:
            # Manual deterministic texture selection is valid too, but every clip
            # frame must be referenced; merely loading the idle texture is not.
            frame_paths = ["assets/normalized/" + f + ".png" for c in contract["clips"] for f in c["frames"]]
            if not all(p in all_source for p in frame_paths):
                findings.append({"path": "project.godot", "line": 1, "severity": "error", "code": "unused_actor_animation",
                    "message": f"Actor {contract['asset_id']} has motion clips but the project uses neither {path} nor all clip frames. "
                    "Integrate state-specific movement/idle/jump playback using simulation ticks; a single sliding texture is incomplete."})
    return findings
