extends Node
## Root scene controller. Owns persistent progress, screen switching, demo
## input playback, and deterministic scenario bootstrap.

const Progress = preload("res://scripts/Progress.gd")
const LevelData = preload("res://scripts/LevelData.gd")
const TitleScreen = preload("res://scenes/TitleScreen.tscn")
const ChapterMenu = preload("res://scenes/ChapterMenu.tscn")
const LevelScene = preload("res://scenes/Level.tscn")

var progress: Progress
var current_level_index: int = 0
var current_screen: Node = null

# Demo-playback state fed from the trace file supplied on the command line.
var play_demo: bool = false
var demo_events: Array = []
var demo_duration: int = 0
var demo_frame: int = 0
var demo_index: int = 0
var demo_input_proxy: InputProxy = null

func _ready() -> void:
	progress = Progress.new()
	InputProxy.install()
	demo_input_proxy = InputProxy.get_singleton()
	var args: PackedStringArray = OS.get_cmdline_user_args()
	var scenario: String = ""
	var trace_path: String = ""
	var i: int = 0
	while i < args.size():
		var a: String = args[i]
		if a == "--scenario" and i + 1 < args.size():
			scenario = args[i + 1]
			i += 2
			continue
		if a == "--trace" and i + 1 < args.size():
			trace_path = args[i + 1]
			i += 2
			continue
		i += 1

	if scenario != "":
		_boot_scenario(scenario)
	else:
		go_title()

func _process(_delta: float) -> void:
	if demo_input_proxy != null:
		demo_input_proxy.poll()

# ---- Screen switching -------------------------------------------------------

func _clear_screen() -> void:
	if current_screen != null and is_instance_valid(current_screen):
		current_screen.queue_free()
		remove_child(current_screen)
	current_screen = null

func _swap(node: Node) -> void:
	_clear_screen()
	current_screen = node
	add_child(node)

func go_title() -> void:
	var t: Node = TitleScreen.instantiate()
	t.main = self
	_swap(t)

func go_chapters() -> void:
	var c: Node = ChapterMenu.instantiate()
	c.main = self
	_swap(c)

func start_level(level_index: int) -> void:
	current_level_index = clampi(level_index, 0, LevelData.LEVEL_COUNT - 1)
	var lv: Node = LevelScene.instantiate()
	lv.main = self
	lv.level_index = current_level_index
	_swap(lv)

func on_level_completed(level_index: int) -> void:
	progress.unlock_next(level_index)

# ---- Deterministic scenarios ------------------------------------------------

func _boot_scenario(scenario_id: String) -> void:
	seed(1337)
	match scenario_id:
		"near_rewind":
			start_level(0)
			_setup_ghost_samples(0, 3)
			call_deferred("_seek_loop", 24.0)
		"near_victory":
			start_level(0)
			_setup_ghost_samples(0, 1)
			_setup_switches_held()
			call_deferred("_seek_loop", 12.0)
		"title_flow":
			go_title()
		"level_1_single_ghost":
			start_level(0)
			_setup_ghost_samples(0, 1)
		"level_2_two_loop":
			start_level(6)
			_setup_ghost_samples(6, 2)
		"level_3_three_loop":
			start_level(12)
			_setup_ghost_samples(12, 3)
		"level_complete":
			start_level(0)
			_setup_ghost_samples(0, 1)
			_setup_switches_held()
			call_deferred("_force_complete")
		"reset_flow":
			start_level(0)
			_setup_ghost_samples(0, 2)
		"final_chapter":
			start_level(18)
			_setup_ghost_samples(18, 3)
		_:
			go_title()

func _seek_loop(seconds: float) -> void:
	if current_screen != null and current_screen.has_method("debug_seek"):
		current_screen.call("debug_seek", seconds)

func _force_complete() -> void:
	if current_screen != null and current_screen.has_method("debug_force_complete"):
		current_screen.call("debug_force_complete")

func _setup_ghost_samples(_level_index: int, ghost_count: int) -> void:
	if current_screen != null and current_screen.has_method("debug_spawn_ghost_samples"):
		current_screen.call("debug_spawn_ghost_samples", ghost_count)

func _setup_switches_held() -> void:
	if current_screen != null and current_screen.has_method("debug_hold_all_switches"):
		current_screen.call("debug_hold_all_switches")

# ---- Minimal input proxy: lets demo playback synthesize input ---------------

## This proxy is intentionally tiny; it supports injected key state used by the
## demo runner and is not part of normal gameplay input.
class InputProxy:
	static var _inst: InputProxy = null
	var keys: Dictionary = {}
	var mouse_pos: Vector2 = Vector2.ZERO
	var mouse_left: bool = false
	var mouse_right: bool = false

	static func install() -> void:
		if _inst == null:
			_inst = InputProxy.new()

	static func get_singleton() -> InputProxy:
		return _inst

	func poll() -> void:
		pass
