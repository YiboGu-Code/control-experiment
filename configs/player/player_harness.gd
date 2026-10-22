extends SceneTree
## Trusted external observer/injector. Never edits the loaded game or calls debug hooks.

var job: Dictionary = {}
var frame: int = -1
var started: bool = false
var captured_frame: int = -2
var event_index: int = 0
var executed: Array = []
var frame_log: FileAccess
var event_log: FileAccess
var last_mouse := Vector2.ZERO
var buttons: int = 0
var truncated: int = 0
var checkpoints: Array[int] = []
const COMMON_PROPERTIES = ["position", "rotation", "scale", "visible", "velocity", "frame", "animation", "playing", "text", "disabled", "button_pressed", "value", "max_value"]

func _initialize() -> void:
	var args := OS.get_cmdline_user_args()
	var job_path := ""
	for i in range(args.size() - 1):
		if args[i] == "--player-job":
			job_path = args[i + 1]
	var parsed = JSON.parse_string(FileAccess.get_file_as_string(job_path))
	if not parsed is Dictionary:
		push_error("Player harness could not read its job")
		quit(2)
		return
	job = parsed
	# Godot parses JSON numbers as floats; normalize protocol integers explicitly.
	job["trace"]["duration_frames"] = int(job["trace"]["duration_frames"])
	for event in job["trace"]["events"]:
		event["frame"] = int(event["frame"])
		if event.has("x"):
			event["x"] = int(event["x"])
			event["y"] = int(event["y"])
	for checkpoint in job["checkpoints"]:
		checkpoints.append(int(checkpoint))
	seed(int(job["seed"]))
	Input.use_accumulated_input = false
	root.size = Vector2i(1280, 720)
	root.content_scale_size = Vector2i(1280, 720)
	root.content_scale_mode = Window.CONTENT_SCALE_MODE_CANVAS_ITEMS
	root.content_scale_aspect = Window.CONTENT_SCALE_ASPECT_IGNORE
	frame_log = FileAccess.open(str(job["output"]) + "/frames.jsonl", FileAccess.WRITE)
	event_log = FileAccess.open(str(job["output"]) + "/events.jsonl", FileAccess.WRITE)
	if frame_log == null or event_log == null:
		push_error("Player harness could not open journals")
		quit(2)
		return
	call_deferred("_start_game")

func _start_game() -> void:
	var main_path: String = str(ProjectSettings.get_setting("application/run/main_scene", ""))
	var scene = load(main_path)
	if not scene is PackedScene:
		push_error("Player harness requires a real project main scene")
		quit(2)
		return
	var game: Node = scene.instantiate()
	root.add_child(game)
	current_scene = game
	RenderingServer.frame_post_draw.connect(_capture)
	started = true

func _process(_delta: float) -> bool:
	if not started:
		return false
	frame += 1
	if frame >= int(job["trace"]["duration_frames"]):
		return false
	var events: Array = job["trace"]["events"]
	while event_index < events.size() and int(events[event_index]["frame"]) == frame:
		var event: Dictionary = events[event_index]
		_inject(event)
		executed.append(event)
		event_log.store_line(JSON.stringify(event))
		event_log.flush()
		event_index += 1
	return false

func _inject(event: Dictionary) -> void:
	var kind: String = str(event["type"])
	if kind == "wait":
		return
	if kind.begins_with("key_"):
		var key := InputEventKey.new()
		var code: int = OS.find_keycode_from_string(str(event["keycode"]))
		key.keycode = code
		key.physical_keycode = code
		key.pressed = kind == "key_down"
		key.echo = false
		Input.parse_input_event(key)
		return
	var pos := Vector2(float(event["x"]), float(event["y"]))
	if kind == "mouse_move":
		var motion := InputEventMouseMotion.new()
		motion.position = pos
		motion.global_position = pos
		motion.relative = pos - last_mouse
		motion.button_mask = buttons
		Input.parse_input_event(motion)
	else:
		# Update Godot's pointer position before routing a GUI button event.
		var motion := InputEventMouseMotion.new()
		motion.position = pos
		motion.global_position = pos
		motion.relative = pos - last_mouse
		motion.button_mask = buttons
		Input.parse_input_event(motion)
		var click := InputEventMouseButton.new()
		click.position = pos
		click.global_position = pos
		click.button_index = MOUSE_BUTTON_LEFT if event["button"] == "left" else MOUSE_BUTTON_RIGHT
		click.pressed = kind == "mouse_down"
		var mask: int = MOUSE_BUTTON_MASK_LEFT if click.button_index == MOUSE_BUTTON_LEFT else MOUSE_BUTTON_MASK_RIGHT
		buttons = (buttons | mask) if click.pressed else (buttons & ~mask)
		click.button_mask = buttons
		Input.parse_input_event(click)
	last_mouse = pos

func _hash(data: PackedByteArray) -> String:
	var context := HashingContext.new()
	context.start(HashingContext.HASH_SHA256)
	context.update(data)
	return context.finish().hex_encode()

func _scalar(value, depth: int = 0):
	match typeof(value):
		TYPE_NIL, TYPE_BOOL, TYPE_INT:
			return value
		TYPE_FLOAT:
			return value if is_finite(value) else str(value)
		TYPE_STRING, TYPE_STRING_NAME, TYPE_NODE_PATH:
			if str(value).length() > 512:
				truncated += 1
			return str(value).left(512)
		TYPE_VECTOR2, TYPE_VECTOR2I:
			return [value.x, value.y]
		TYPE_VECTOR3, TYPE_VECTOR3I:
			return [value.x, value.y, value.z]
		TYPE_COLOR:
			return [value.r, value.g, value.b, value.a]
		TYPE_RECT2, TYPE_RECT2I:
			return [value.position.x, value.position.y, value.size.x, value.size.y]
		TYPE_OBJECT:
			if is_instance_valid(value) and value is Node and value.is_inside_tree():
				return str(root.get_path_to(value))
		TYPE_ARRAY:
			if depth < 2 and value.size() <= 16:
				var result: Array = []
				for item in value:
					result.append(_scalar(item, depth + 1))
				return result
		TYPE_DICTIONARY:
			if depth < 2 and value.size() <= 32:
				var result: Dictionary = {}
				for key in value:
					if typeof(key) in [TYPE_STRING, TYPE_STRING_NAME]:
						result[str(key)] = _scalar(value[key], depth + 1)
				return result
	return null

func _snapshot() -> Dictionary:
	var queue: Array[Node] = []
	for child in root.get_children():
		queue.append(child)
	var rows: Array = []
	truncated = 0
	while not queue.is_empty() and rows.size() < 512:
		var node: Node = queue.pop_front()
		if not is_instance_valid(node) or node.is_queued_for_deletion():
			continue
		var props: Dictionary = {}
		var count: int = 0
		for property in node.get_property_list():
			var name: String = str(property["name"])
			if name in COMMON_PROPERTIES or (int(property["usage"]) & PROPERTY_USAGE_SCRIPT_VARIABLE) != 0:
				if count >= 64:
					truncated += 1
					break
				var value = _scalar(node.get(name))
				if value != null:
					props[name] = value
				count += 1
		if node is CanvasItem:
			props["visible_in_tree"] = node.is_visible_in_tree()
		if node is Control:
			props["screen_rect"] = _scalar(node.get_global_rect())
		rows.append({"path": str(root.get_path_to(node)), "class": node.get_class(), "scripted": node.get_script() != null, "properties": props})
		for child in node.get_children():
			queue.append(child)
	if not queue.is_empty():
		truncated += queue.size()
	return {"nodes": rows, "truncations": truncated}

func _capture() -> void:
	if frame < 0 or frame == captured_frame or frame >= int(job["trace"]["duration_frames"]):
		return
	captured_frame = frame
	var image: Image = root.get_texture().get_image()
	if image == null or image.is_empty():
		push_error("Player requires a real rendering viewport")
		quit(2)
		return
	image.convert(Image.FORMAT_RGBA8)
	var snapshot := _snapshot()
	var state_json: String = JSON.stringify(snapshot)
	var row := {"frame": frame, "state_sha256": _hash(state_json.to_utf8_buffer()), "pixels_sha256": _hash(image.get_data())}
	frame_log.store_line(JSON.stringify(row))
	frame_log.flush()
	if frame in checkpoints:
		var stem: String = str(job["output"]) + "/frame_%04d" % frame
		if image.save_png(stem + ".png") != OK:
			push_error("Player screenshot could not be saved")
			quit(2)
			return
		var file := FileAccess.open(stem + ".json", FileAccess.WRITE)
		file.store_string(state_json)
		file.close()
	if frame + 1 == int(job["trace"]["duration_frames"]):
		var trace: Dictionary = job["trace"].duplicate(true)
		trace["events"] = executed
		var file := FileAccess.open(str(job["output"]) + "/executed_trace.json", FileAccess.WRITE)
		file.store_string(JSON.stringify(trace))
		file.close()
		file = FileAccess.open(str(job["output"]) + "/receipt.json", FileAccess.WRITE)
		file.store_string(JSON.stringify({"harness_version": "godot-player.v1", "completed_frames": frame + 1, "event_count": executed.size(), "width": image.get_width(), "height": image.get_height(), "fps": 30}))
		file.close()
		frame_log.close()
		event_log.close()
		quit(0)
