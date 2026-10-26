"""Versioned, host-owned guidance. These resources grant no new tool permissions."""

ACTOR_MOTION = """Actor motion v1:
Treat an actor as a state machine, not a single picture sliding over the screen.
Provide motion={mode,display_height,clips,implementation} on its anchor asset. For mode=frames,
clips use {name,frames:[\"asset_id/frame_name\",...],fps,loop}. Frame references may span
separate clean library files. At least one clip needs two genuinely different body poses.
Choose idle/locomotion/airborne states appropriate to actual mechanics. Reuse the SAME character,
costume, proportions, lighting, camera angle, foot baseline and palette across states. No labels.
Prefer a matching animation pack from one library. Otherwise request 2-4 aligned poses of ONE
character in a small sheet, or separate poses with reference_asset_ids. Never declare duplicate
images, horizontal flips or translation of one still as a walk cycle. Do not promise unseen poses.
mode=procedural requires a concrete state-specific motion recipe with resettable simulation ticks
(lean, body bob, landing response); it is a fallback and does not prove articulated limb animation.
mode=static requires an explicit reason appropriate to the task, e.g. a board-game piece. Do not
choose static merely to avoid producing movement for a platformer. display_height is the logical
on-screen actor height, not a pixel resize request. Grounded characters must fit their collision
body and share foot pivots; initial facing and ghost clones need the same animation contract.
Prioritize a coherent playable actor and essential world objects over many unrelated decorative
panels. A pixel geometry check cannot verify the animation's semantics; runtime review is required.
"""

REFERENCE_EDIT = """Reference editing v1:
When image_service.supports_reference_images=true, use reference_asset_ids (at most 3) to name
existing assets or earlier assets in this batch. The host resolves hashed public image bytes,
never model URLs/host paths. Produce an identity anchor before its motion variants. For another
pose reference the same character and describe precisely what changes and what remains fixed.
For other objects in the same art set, specify that the reference guides palette/material/lighting,
not subject identity. Avoid using a title scene as the reference for an isolated actor.
A failed candidate can reference itself on the next quality attempt; the host also automatically
uses the previous candidate for supported providers when an explicit reference was omitted.
This is a newly metered SDK image edit, with the same budgets and normalization checks. No
automatic hidden retry. References improve conditioning but do not guarantee identity or pose.
Keep the style_guide stable across repair rounds; do not silently redesign the entire palette.
"""

DEVELOPER_SKILLS = {
    "skill/actor_motion": ACTOR_MOTION + """
Host-provided motion_contracts list assets/animations/*.tres SpriteFrames resources and frame geometry.
Use those resources in AnimatedSprite2D or select all documented textures manually. Do not let
automatic wall-time playback control deterministic replay: stop automatic playback and select
frame from elapsed simulation ticks since entering the state (fps / physics_tick_rate). Reset
state tick on state transition, restart and scenario initialization. Loop or clamp as specified.
Use one common canvas/pivot and uniform scale; display_height / native canvas height is only a
starting fit. Inspect the actual visible subject and feet against collision bounds. A native
1024px image is not a 1024-unit actor. Never hardcode a universal scale such as 0.25 for all art.
Record animation state/frame and pose in ghost tracks, then reconstruct them at the same tick.
Player must capture multiple checkpoints during locomotion/jump/landing; a static screenshot
and a matching replay can both pass while the character still has no motion. Keep this unverified
until temporal evidence exists.
""",
    "skill/presentation_checks": """Presentation checks v1:
Review ordinary menus AND actual gameplay at the intended viewport. Confirm actor/prop/world
scale, feet on platforms, unclipped HUD, consistent colors, readable button labels and no atlas
misuse. Use NinePatchRect for resizable decorative panels or aspect-preserving texture modes.
For every flash/fade, check entry, active and expired frames. Decrement an effect countdown on
simulation ticks and reset it on restart; assigning a positive timer once leaves an overlay on
forever. Check first and last frames of ghost spawning so no actor appears at the origin.
Inspect displayed format strings (e.g. literal Chapter %d). Input traces must click actual button
rectangles and exercise pause/resume. Report measured symptoms, not intent or untested guesses.
""",
}
