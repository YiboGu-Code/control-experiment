extends Area2D
## A recorded past-loop replay. Replays a recorded sample stream. Ghosts do not
## collide with the player; they pose as Bodies-like holders for switches and
## blockers for lasers and enemies via the Level queries on their position.

var samples: Array = []
var playback_frame: int = 0
var marker_color_index: int = 0
var done: bool = false

@onready var anim: AnimatedSprite2D = $AnimatedSprite2D

func setup(rec: Array, color_index: int) -> void:
	samples = rec
	playback_frame = 0
	marker_color_index = color_index
	modulate = Color(0.61, 0.48, 1.0, 0.55)

func is_active() -> bool:
	return not done and samples.size() > 0

func advance(frames: int) -> void:
	if samples.is_empty():
		done = true
		return
	playback_frame = frames
	var idx: int = clampi(frames, 0, samples.size() - 1)
	var s = samples[idx]
	if s is Dictionary:
		position = s.get("pos", position)
		var on_floor: bool = s.get("ground", true)
		var vx: float = s.get("vx", 0.0)
		_update_anim(on_floor, vx)

func _update_anim(on_floor: bool, vx: float) -> void:
	if anim == null or anim.sprite_frames == null:
		return
	if not on_floor:
		if anim.sprite_frames.has_animation("jump"):
			anim.play("jump")
	elif absf(vx) > 1.0:
		if anim.sprite_frames.has_animation("run"):
			anim.play("run")
	else:
		if anim.sprite_frames.has_animation("idle"):
			anim.play("idle")
