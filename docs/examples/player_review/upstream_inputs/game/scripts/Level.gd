extends Node2D
## Drives the 30-second loop, the recorder, the ghost replays, the interactables
## and the completion gating for one level.

const LevelData = preload("res://scripts/LevelData.gd")
const GhostScene = preload("res://scenes/Ghost.tscn")
const PlayerScene = preload("res://scenes/Player.tscn")

const LOOP_SECONDS := 30.0
const TICK_HZ := 30.0
const TICKS_PER_LOOP := 900  # 30 s * 30 Hz
const MAX_RECORDED_GHOSTS := 3  # 3 past ghosts + current loop = 4 loops

var main: Node = null
var level_index: int = 0
var level_def: Dictionary = {}

var tick: int = 0
var frame_accumulator: float = 0.0

var current_record: Array = []
var current_sample: Array = []
var ghosts: Array = []

var switches_held: Dictionary = {}
var required_ids: Array = []
var switch_nodes: Dictionary = {}
var plate_nodes: Dictionary = {}
var door_nodes: Dictionary = {}
var laser_nodes: Array = []
var enemy_nodes: Array = []

var player: CharacterBody2D = null
var exit_pos: Vector2 = Vector2.ZERO
var completed: bool = false
var flashing: bool = false
var flash_timer: float = 0.0
var jumps_used: int = 0
var exit_at_time: float = 0.0

@onready var world: Node2D = $World
@onready var entity_root: Node2D = $Entities
@onready var platforms_root: Node2D = $Platforms
@onready var hud: CanvasLayer = $HUD
@onready var flash_rect: ColorRect = $HUD/FlashRect
@onready var overlay: Control = $HUD/CompleteOverlay

func _ready() -> void:
	level_def = LevelData.build(level_index)
	required_ids = level_def["required_switch_ids"]
	exit_pos = level_def["exit"]
	_build_world()
	_spawn_player()
	if hud != null and hud.has_method("bind_level"):
		hud.call("bind_level", self)
	if overlay != null:
		overlay.visible = false
	if flash_rect != null:
		flash_rect.color = Color(1, 1, 1, 0)
	completed = false
	tick = 0
	frame_accumulator = 0.0
	if player != null:
		player.can_control = true

func _build_world() -> void:
	var platforms: Array = level_def["platforms"]
	for r in platforms:
		var rect: Rect2 = r
		var body := StaticBody2D.new()
		body.position = rect.position
		var cs := CollisionShape2D.new()
		var shape := RectangleShape2D.new()
		shape.size = rect.size
		cs.shape = shape
		cs.position = rect.size * 0.5
		body.add_child(cs)
		var poly := Polygon2D.new()
		poly.color = Color(0.24, 0.26, 0.42)
		poly.polygon = PackedVector2Array([
			Vector2(0, 0), Vector2(rect.size.x, 0),
			Vector2(rect.size.x, rect.size.y), Vector2(0, rect.size.y)
		])
		body.add_child(poly)
		platforms_root.add_child(body)

	# Switches, plates, doors, lasers, enemies.
	for sw in level_def["switches"]:
		var node := _make_switch(sw["id"], sw["pos"])
		switch_nodes[sw["id"]] = node
		switches_held[sw["id"]] = false
	for pl in level_def["plates"]:
		var node := _make_plate(pl["id"], pl["pos"])
		plate_nodes[pl["id"]] = node
		switches_held[pl["id"]] = false
	for dr in level_def["doors"]:
		var node := _make_door(dr["id"], dr["pos"], dr["h"])
		door_nodes[dr["id"]] = node
	for lz in level_def["lasers"]:
		laser_nodes.append(_make_laser(lz["emitter"], lz["length"]))
	for en in level_def["enemies"]:
		enemy_nodes.append(_make_enemy(en["pos"], en["min_x"], en["max_x"]))
	var exit_sprite := _make_exit(exit_pos)
	entity_root.add_child(exit_sprite)

func _make_switch(id: String, pos: Vector2) -> Node2D:
	var root := Node2D.new()
	root.position = pos
	root.name = "Switch_" + id
	var spr := Sprite2D.new()
	spr.texture = load("res://assets/props/switch_button.png")
	spr.hframes = 2
	spr.frame = 0
	spr.region_enabled = true
	spr.region_rect = Rect2(0, 0, 32, 32)
	spr.texture_filter = CanvasItem.TEXTURE_FILTER_NEAREST
	spr.scale = Vector2(2, 2)
	root.add_child(spr)
	root.set_meta("sprite", spr)
	entity_root.add_child(root)
	return root

func _make_plate(id: String, pos: Vector2) -> Node2D:
	var root := Node2D.new()
	root.position = pos
	root.name = "Plate_" + id
	var spr := Sprite2D.new()
	spr.texture = load("res://assets/props/pressure_plate.png")
	spr.hframes = 2
	spr.frame = 0
	spr.region_enabled = true
	spr.region_rect = Rect2(0, 0, 32, 32)
	spr.texture_filter = CanvasItem.TEXTURE_FILTER_NEAREST
	spr.scale = Vector2(2, 2)
	root.add_child(spr)
	root.set_meta("sprite", spr)
	entity_root.add_child(root)
	return root

func _make_door(id: String, pos: Vector2, h: float) -> StaticBody2D:
	var body := StaticBody2D.new()
	body.position = pos
	body.name = "Door_" + id
	var spr := Sprite2D.new()
	spr.texture = load("res://assets/props/door_gate.png")
	spr.hframes = 2
	spr.frame = 0
	spr.region_enabled = true
	spr.region_rect = Rect2(0, 0, 32, 32)
	spr.texture_filter = CanvasItem.TEXTURE_FILTER_NEAREST
	spr.scale = Vector2(1, h / 32.0)
	body.add_child(spr)
	var cs := CollisionShape2D.new()
	var shape := RectangleShape2D.new()
	shape.size = Vector2(32, h)
	cs.shape = shape
	cs.position = Vector2(16, h * 0.5)
	body.add_child(cs)
	body.set_meta("sprite", spr)
	body.set_meta("collision", cs)
	body.set_meta("open", false)
	entity_root.add_child(body)
	return body

func _make_laser(emitter: Vector2, length: float) -> Node2D:
	var root := Node2D.new()
	root.position = emitter
	root.name = "Laser"
	var em := Sprite2D.new()
	em.texture = load("res://assets/props/laser_hazard.png")
	em.region_enabled = true
	em.region_rect = Rect2(0, 0, 32, 32)
	em.texture_filter = CanvasItem.TEXTURE_FILTER_NEAREST
	em.scale = Vector2(2, 2)
	root.add_child(em)
	var beam := Sprite2D.new()
	beam.texture = load("res://assets/props/laser_hazard.png")
	beam.region_enabled = true
	beam.region_rect = Rect2(32, 0, 32, 32)
	beam.texture_filter = CanvasItem.TEXTURE_FILTER_NEAREST
	beam.position = Vector2(32, 16)
	beam.centered = false
	beam.scale = Vector2(1.0, 0.6)
	root.add_child(beam)
	root.set_meta("beam", beam)
	root.set_meta("length", length)
	entity_root.add_child(root)
	return root

func _make_enemy(pos: Vector2, min_x: float, max_x: float) -> Node2D:
	var root := Node2D.new()
	root.position = pos
	root.name = "Enemy"
	var spr := Sprite2D.new()
	spr.texture = load("res://assets/sprites/enemy_patroller.png")
	spr.hframes = 2
	spr.frame = 0
	spr.region_enabled = true
	spr.region_rect = Rect2(0, 0, 32, 32)
	spr.texture_filter = CanvasItem.TEXTURE_FILTER_NEAREST
	spr.scale = Vector2(2, 2)
	root.add_child(spr)
	root.set_meta("sprite", spr)
	root.set_meta("min_x", min_x)
	root.set_meta("max_x", max_x)
	root.set_meta("dir", 1)
	root.set_meta("distracted", false)
	entity_root.add_child(root)
	return root

func _make_exit(pos: Vector2) -> Node2D:
	var root := Node2D.new()
	root.position = pos
	root.name = "ExitCrystal"
	var spr := Sprite2D.new()
	spr.texture = load("res://assets/props/exit_crystal.png")
	spr.hframes = 2
	spr.frame = 0
	spr.region_enabled = true
	spr.region_rect = Rect2(0, 0, 32, 32)
	spr.texture_filter = CanvasItem.TEXTURE_FILTER_NEAREST
	spr.scale = Vector2(2, 2)
	root.add_child(spr)
	root.set_meta("sprite", spr)
	return root

func _spawn_player() -> void:
	player = PlayerScene.instantiate()
	player.position = level_def["spawn"]
	add_child(player)
	var cam := Camera2D.new()
	cam.position = Vector2(0, 0)
	player.add_child(cam)

# ---- Loop clock -------------------------------------------------------------

func _physics_process(delta: float) -> void:
	if completed:
		return
	frame_accumulator += delta
	while frame_accumulator >= 1.0 / TICK_HZ:
		frame_accumulator -= 1.0 / TICK_HZ
		_tick_once()
	_update_visuals(delta)

func _tick_once() -> void:
	tick += 1
	_record_sample()
	for g in ghosts:
		g.advance(tick)
	_resolve_holders()
	_update_enemies()
	_update_lasers()
	_check_exit()
	if tick >= TICKS_PER_LOOP:
		_loop_expire()

func _record_sample() -> void:
	var pos: Vector2 = player.position if player != null else Vector2.ZERO
	var vx: float = player.velocity.x if player != null else 0.0
	var ground: bool = player.is_on_floor() if player != null else true
	current_sample = [pos, vx, ground]
	current_record.append({"pos": pos, "vx": vx, "ground": ground})

func _loop_expire() -> void:
	if ghosts.size() >= MAX_RECORDED_GHOSTS:
		_ghost_drop_oldest()
	_add_ghost(current_record.duplicate(true))
	current_record = []
	tick = 0
	if player != null:
		player.respawn()
	_flash()
	for g in ghosts:
		g.advance(0)

func _ghost_drop_oldest() -> void:
	if ghosts.is_empty():
		return
	var g: Node = ghosts.pop_front()
	if is_instance_valid(g):
		g.queue_free()
	for i in range(ghosts.size()):
		ghosts[i].marker_color_index = i

func _add_ghost(rec: Array) -> void:
	if rec.is_empty():
		return
	var g: Area2D = GhostScene.instantiate()
	add_child(g)
	g.setup(rec, ghosts.size())
	g.advance(0)
	ghosts.append(g)

func _flash() -> void:
	flashing = true
	flash_timer = 0.35
	if flash_rect != null:
		flash_rect.color = Color(1, 1, 1, 0.85)

# ---- Interactables ----------------------------------------------------------

func _resolve_holders() -> void:
	var positions: Array = []
	if player != null:
		positions.append(_body_rect(player.position))
	for g in ghosts:
		positions.append(_body_rect(g.position))

	for id in switch_nodes.keys():
		var node: Node2D = switch_nodes[id]
		var held: bool = _overlaps_any(node.position + Vector2(16, 16), 30.0, positions)
		switches_held[id] = held
		var spr: Sprite2D = node.get_meta("sprite")
		if spr != null:
			spr.region_rect = Rect2(32.0 if held else 0.0, 0, 32, 32)

	for id in plate_nodes.keys():
		var node: Node2D = plate_nodes[id]
		var held: bool = _overlaps_any(node.position + Vector2(16, 20), 40.0, positions)
		switches_held[id] = held
		var spr: Sprite2D = node.get_meta("sprite")
		if spr != null:
			spr.region_rect = Rect2(32.0 if held else 0.0, 0, 32, 32)

	for id in door_nodes.keys():
		var body: StaticBody2D = door_nodes[id]
		var open: bool = _overlaps_any(body.position + Vector2(16, 32), 40.0, positions)
		body.set_meta("open", open)
		var cs: CollisionShape2D = body.get_meta("collision")
		if cs != null:
			cs.disabled = open
		var spr: Sprite2D = body.get_meta("sprite")
		if spr != null:
			spr.region_rect = Rect2(32.0 if open else 0.0, 0, 32, 32)

func _body_rect(pos: Vector2) -> Rect2:
	return Rect2(pos + Vector2(-12, -16), Vector2(24, 32))

func _overlaps_any(center: Vector2, radius: float, bodies: Array) -> bool:
	for b in bodies:
		var rect: Rect2 = b
		if rect.position.distance_to(center) <= radius + 24.0:
			return true
	return false

func _update_enemies() -> void:
	var positions: Array = []
	if player != null:
		positions.append(_body_rect(player.position))
	for g in ghosts:
		positions.append(_body_rect(g.position))
	for e in enemy_nodes:
		var node: Node2D = e
		var distracted: bool = _overlaps_any(node.position, 96.0, positions)
		node.set_meta("distracted", distracted)
		var spr: Sprite2D = node.get_meta("sprite")
		if spr != null:
			if distracted:
				spr.modulate = Color(1.0, 0.7, 0.25)
			else:
				spr.modulate = Color(1, 1, 1)
				spr.region_rect = Rect2(0 if (int(node.position.x) / 8) % 2 == 0 else 32.0, 0, 32, 32)

func _update_lasers() -> void:
	var positions: Array = []
	if player != null:
		positions.append(_body_rect(player.position))
	for g in ghosts:
		positions.append(_body_rect(g.position))
	for lz in laser_nodes:
		var node: Node2D = lz
		var beam: Sprite2D = node.get_meta("beam")
		var length: float = node.get_meta("length")
		var best: float = length
		for p in positions:
			var rect: Rect2 = p
			var rel: Vector2 = rect.get_center() - node.position
			if rel.y > -24 and rel.y < 24 and rel.x > 0 and rel.x < best:
				best = rel.x
		if beam != null:
			beam.scale = Vector2(maxf(best / 32.0, 0.1), 0.6)

func _check_exit() -> void:
	if player == null:
		return
	var all_held: bool = true
	for id in required_ids:
		if not switches_held.get(id, false):
			all_held = false
			break
	var crystal: Node2D = null
	for c in entity_root.get_children():
		if c.name == "ExitCrystal":
			crystal = c
			break
	if crystal != null:
		var spr: Sprite2D = crystal.get_meta("sprite")
		if spr != null:
			spr.region_rect = Rect2(32.0 if all_held else 0.0, 0, 32, 32)
	if player.position.distance_to(exit_pos) < 34.0:
		if all_held:
			_complete()
		else:
			_reject_flash()

func _reject_flash() -> void:
	if flash_rect != null and not flashing:
		flash_rect.color = Color(0.85, 0.2, 0.2, 0.4)
		flash_timer = 0.15
		flashing = true

func _complete() -> void:
	if completed:
		return
	completed = true
	exit_at_time = float(tick) / TICK_HZ
	if player != null:
		player.can_control = false
	if main != null and main.has_method("on_level_completed"):
		main.call("on_level_completed", level_index)
	_show_complete_overlay(float(ghosts.size() + 1), exit_at_time)

func _show_complete_overlay(loops_used: float, exit_time: float) -> void:
	if overlay == null:
		return
	overlay.visible = true
	var loops_label: Label = overlay.get_node_or_null("VBox/LoopsLabel") as Label
	var time_label: Label = overlay.get_node_or_null("VBox/TimeLabel") as Label
	var name_label: Label = overlay.get_node_or_null("VBox/NameLabel") as Label
	if loops_label != null:
		loops_label.text = "Loops used: %d" % int(loops_used)
	if time_label != null:
		time_label.text = "Exit time: %.1f s" % exit_time
	if name_label != null:
		name_label.text = str(level_def.get("name", "Level"))

# ---- Visual update ----------------------------------------------------------

func _update_visuals(delta: float) -> void:
	if flashing and flash_rect != null:
		flash_timer -= delta
		if flash_timer <= 0.0:
			flashing = false
			flash_rect.color = Color(1, 1, 1, 0)
		else:
			var a: float = flash_rect.color.a
			flash_rect.color = Color(flash_rect.color.r, flash_rect.color.g, flash_rect.color.b, maxf(a - delta * 2.5, 0.0))
	if hud != null and hud.has_method("update_clock"):
		var rem: float = maxf(float(TICKS_PER_LOOP - tick) / TICK_HZ, 0.0)
		var loop_time: float = float(tick) / TICK_HZ
		hud.call("update_clock", rem, loop_time, ghosts.size())
	if hud != null and hud.has_method("update_input"):
		hud.call("update_input", Input.is_action_pressed("move_right"), Input.is_action_pressed("move_left"), Input.is_action_just_pressed("jump"), Input.is_action_just_pressed("interact"), Input.is_action_just_pressed("reset_level"))

# ---- Debug / scenario hooks -------------------------------------------------

func debug_seek(seconds: float) -> void:
	tick = int(clampf(seconds, 0.0, LOOP_SECONDS - 0.5) * TICK_HZ)
	frame_accumulator = 0.0
	for g in ghosts:
		g.advance(tick)

func debug_spawn_ghost_samples(ghost_count: int) -> void:
	for i in range(ghost_count):
		var rec: Array = []
		for f in range(TICKS_PER_LOOP):
			var p: Vector2 = level_def["spawn"] + Vector2(float(f) * 0.4 + float(i) * 60.0, 0)
			rec.append({"pos": p, "vx": 0.0, "ground": true})
		_add_ghost(rec)
	for g in ghosts:
		g.advance(tick)

func debug_hold_all_switches() -> void:
	for id in required_ids:
		switches_held[id] = true

func debug_force_complete() -> void:
	tick = 12 * 30
	_complete()

func debug_has_control() -> bool:
	return player != null and player.can_control

func reset_ghosts() -> void:
	for g in ghosts:
		if is_instance_valid(g):
			g.queue_free()
	ghosts.clear()
	current_record = []
	tick = 0
	if player != null:
		player.respawn()

func handle_interact() -> void:
	pass

func request_back() -> void:
	if main != null and main.has_method("go_chapters"):
		main.call("go_chapters")
