"""Small host-selected image capabilities; never load arbitrary model-named files."""

SINGLE_SPRITE = """ASSET CAPABILITY: one isolated sprite.
Draw exactly ONE instance of the requested subject in its requested state, centered and
fully visible with empty space around it. If the request is a door, switch, crystal
or other object, draw that object alone; do not add a character using or standing beside it.
No repeated copies, alternative states, animation sequence, contact sheet or sample gameplay.
No ground, grass, rocks, podium, scenery, panel, frame, UI, text, letters, numbers, labels,
captions, contact shadow or drawn checkerboard. Keep natural subject proportions.
Use the requested empty background around the subject and between limbs or object parts.
Style context controls colors/rendering only; never draw its other named assets.
"""

ACTOR = """ASSET CAPABILITY: isolated sprite sequence.
Render only the same requested subject in the requested states, one complete subject per
cell. A sprite sheet is a collection of cutouts, not illustrated cards or gameplay scenes.
For an object sprite, do not add an actor demonstrating it. Keep camera and scale consistent.
For character animation, keep body proportions and outfit consistent; show motion through
the body pose without a visible floor, platform, obstacle or scenic context.
No ground, grass, rocks, podium, scenery, panel, frame, UI, text, letters, numbers, pose labels,
motion captions, contact shadow or drawn checkerboard. The empty space around and between
limbs must use the requested background. No decorative extras or unrelated game objects.
If a style reference mentions terrain/UI/other assets, ignore those subjects for this image.
"""

ENVIRONMENT = """ASSET CAPABILITY: environment or tile artwork.
Render only the requested background/tile content. Scenery is allowed for this asset type.
Do not add a player, presentation board, caption, watermark or unrequested UI.
"""

PROP = """ASSET CAPABILITY: one isolated world object.
Draw only the requested functional object in the game's camera view. A switch is an actual
world switch, not a console/controller button glyph. A door is a gate/door, not a building.
The object's own base/body is allowed; no unrelated ground, character, scene, text, labels,
UI, contact shadow or presentation board. Preserve proportions and the requested background.
"""

UI = """ASSET CAPABILITY: UI artwork.
Render only the requested UI component. Do not add a sample gameplay scene or presentation
board. Keep text out of the image unless the asset request explicitly requires baked glyphs.
"""


def is_actor(job):
    return job.layout.kind in ("sprite", "sprite_sheet") if job.layout else job.transparent_background


def subject_instructions(job):
    kind = job.layout.kind if job.layout else ("sprite" if job.transparent_background else "background")
    if kind == "prop":
        return PROP
    return (SINGLE_SPRITE if len(job.frame_names) == 1 else ACTOR) if kind in ("sprite", "sprite_sheet") else UI if kind == "ui" else ENVIRONMENT
