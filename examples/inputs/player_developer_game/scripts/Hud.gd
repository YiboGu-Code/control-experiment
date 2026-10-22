extends CanvasLayer
## Play HUD: countdown badge, timeline window with ghost markers, reset button,
## and window-level navigation. Reads clock/ghost state each frame.

var level: Node = null

@onready var timer_label: Label = $Root/TimerLabel
@onready var timeline: Control = $Root/Timeline
@onready var reset_button: Button = $Root/ResetButton
@onready var back_button: Button = $Root/MenuButton
@onready var markers_root: Control = $Root/Timeline/Markers
@onready var loop_label: Label = $Root/LoopLabel

var marker_nodes: Array = []

func bind_level(lv: Node) -> void:
	level = lv
	if reset_button != null:
		reset_button.pressed.connect(_on_reset_pressed)
	if back_button != null:
		back_button.pressed.connect(_on_back_pressed)

func _on_reset_pressed() -> void:
	if level != null and level.has_method("reset_ghosts"):
		level.call("reset_ghosts")

func _on_back_pressed() -> void:
	if level != null and level.has_method("request_back"):
		level.call("request_back")

func update_clock(remaining: float, loop_time: float, ghost_count: int) -> void:
	if timer_label != null:
		timer_label.text = "%05.2f" % remaining
	if loop_label != null:
		loop_label.text = "Loop %d / 4" % (ghost_count + 1)
	_update_markers(loop_time, ghost_count)

func _update_markers(loop_time: float, ghost_count: int) -> void:
	if markers_root == null or timeline == null:
		return
	while marker_nodes.size() < ghost_count:
		var m := ColorRect.new()
		m.size = Vector2(10, 10)
		m.color = Color(0.61, 0.48, 1.0)
		markers_root.add_child(m)
		marker_nodes.append(m)
	for i in range(marker_nodes.size()):
		var m: ColorRect = marker_nodes[i]
		m.visible = i < ghost_count
		var frac: float = clampf(loop_time / 30.0, 0.0, 1.0)
		m.position = Vector2(frac * (timeline.size.x - 10.0), 8.0)

func update_input(right: bool, left: bool, jump: bool, interact: bool, reset: bool) -> void:
	if reset and level != null and level.has_method("reset_ghosts"):
		level.call("reset_ghosts")
	if interact and level != null and level.has_method("handle_interact"):
		level.call("handle_interact")
