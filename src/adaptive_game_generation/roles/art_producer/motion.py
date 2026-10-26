"""Explicit actor motion contracts, independent of the scheduler and image vendor."""
from ...runtime import InvalidSubmission


def validate_motion(assets, previous, *, required=False):
    drafts = {a.request.asset_id: a.request for a in previous}
    drafts.update({a.asset_id: a for a in assets})
    frames = {f"{a.asset_id}/{name}" for a in drafts.values() for name in a.frame_names}
    # One anchor may describe a clip assembled from several individual library files.
    covered = {key.split("/", 1)[0] for a in drafts.values() if a.motion
               for clip in a.motion.clips for key in clip.frames}
    for asset in drafts.values():
        actor = asset.layout and asset.layout.kind in ("sprite", "sprite_sheet")
        if required and actor and not asset.motion and asset.asset_id not in covered:
            raise InvalidSubmission(f"{asset.asset_id}: actor needs a motion contract or membership in another actor's clips")
        if asset.motion:
            if not actor:
                raise InvalidSubmission(f"{asset.asset_id}: actor motion requires sprite/sprite_sheet classification")
            missing = {f for c in asset.motion.clips for f in c.frames} - frames
            if missing:
                raise InvalidSubmission(f"{asset.asset_id}: unknown animation frames: {', '.join(sorted(missing))}")


def motion_quality(assets):
    """Validate available, distinct pixel frames; this is NOT semantic pose review."""
    frames = {f"{a.request.asset_id}/{f.region.name}": f for a in assets if a.normalization
              for f in a.normalization.frames}
    findings = []
    for asset in assets:
        if not asset.request.motion:
            continue
        for clip in asset.request.motion.clips:
            if any(key not in frames for key in clip.frames):
                findings.append({"asset_id": asset.request.asset_id, "clip": clip.name,
                    "code": "missing_animation_frames", "message": "Some clip frames were rejected or are unavailable"})
            elif len(set(clip.frames)) > 1 and len({frames[key].artifact_ref.sha256 for key in clip.frames}) == 1:
                findings.append({"asset_id": asset.request.asset_id, "clip": clip.name,
                    "code": "duplicate_animation_frames", "message": "All motion frames contain identical pixels; distinct poses are required"})
    return findings
