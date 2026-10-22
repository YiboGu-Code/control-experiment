extends CharacterBody2D
## The controllable player character: run, jump, interact. Physics are
## deterministic and independent of input source so replays are exact.

const SPEED := 220.0
const JUMP_VELOCITY := -420.0
const GRAVITY := 1200.0

var can_control: bool = false
var facing: int = 1
var spawn_position: Vector2 = Vector2.ZERO

@onready var anim: AnimatedSprite2D = $AnimatedSprite2D

func _ready() -> void:
	spawn_position = position

func respawn() -> void:
	position = spawn_position
	velocity = Vector2.ZERO

func _physics_process(delta: float) -> void:
	if not can_control:
		velocity.x = 0.0
		if not is_on_floor():
			velocity.y += GRAVITY * delta
		move_and_slide()
		_update_anim()
		return

	var move: float = 0.0
	if Input.is_action_pressed("move_left"):
		move -= 1.0
	if Input.is_action_pressed("move_right"):
		move += 1.0
	velocity.x = move * SPEED
	if move != 0.0:
		facing = int(sign(move))

	if not is_on_floor():
		velocity.y += GRAVITY * delta
	if Input.is_action_just_pressed("jump") and is_on_floor():
		velocity.y = JUMP_VELOCITY
	move_and_slide()
	_update_anim()

func _update_anim() -> void:
	if anim == null or anim.sprite_frames == null:
		return
	if not is_on_floor():
		if anim.sprite_frames.has_animation("jump"):
			anim.play("jump")
	elif absf(velocity.x) > 1.0:
		if anim.sprite_frames.has_animation("run"):
			anim.play("run")
	else:
		if anim.sprite_frames.has_animation("idle"):
			anim.play("idle")
	if facing < 0:
		anim.flip_h = true
	elif facing > 0:
		anim.flip_h = false
