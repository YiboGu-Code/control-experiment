"""Small host-selected image capabilities; never load arbitrary model-named files."""

ACTOR = """ASSET CAPABILITY: isolated actor animation.
Render only the same character performing the requested poses, one complete character per
cell. A sprite sheet is a collection of cutout poses, not illustrated cards or gameplay scenes.
Keep body proportions, outfit, camera and scale consistent. Motion is shown by the body pose;
running and jumping do not need a visible floor, platform, obstacle or scenic context.
No ground, grass, rocks, podium, scenery, panel, frame, UI, text, letters, numbers, pose labels,
motion captions, contact shadow or drawn checkerboard. The empty space around and between
limbs must use the requested background. No decorative extras or unrelated game objects.
If a style reference mentions terrain/UI/other assets, ignore those subjects for this image.
"""

ENVIRONMENT = """ASSET CAPABILITY: environment or tile artwork.
Render only the requested background/tile content. Scenery is allowed for this asset type.
Do not add a player, presentation board, caption, watermark or unrequested UI.
"""

UI = """ASSET CAPABILITY: UI artwork.
Render only the requested UI component. Do not add a sample gameplay scene or presentation
board. Keep text out of the image unless the asset request explicitly requires baked glyphs.
"""


def is_actor(job):
    return job.layout.kind in ("sprite", "sprite_sheet") if job.layout else job.transparent_background


def subject_instructions(job):
    kind = job.layout.kind if job.layout else ("sprite" if job.transparent_background else "background")
    return ACTOR if kind in ("sprite", "sprite_sheet") else UI if kind == "ui" else ENVIRONMENT
