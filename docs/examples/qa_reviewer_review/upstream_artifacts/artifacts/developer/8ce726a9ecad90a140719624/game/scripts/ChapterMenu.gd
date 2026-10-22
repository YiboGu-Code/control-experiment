extends Control
## Chapter menu: four chapter headers with six level buttons each. Buttons show
## locked / unlocked state from progress.

var main: Node = null

const LevelData = preload("res://scripts/LevelData.gd")

@onready var grid: GridContainer = $Panel/Grid
@onready var back_button: Button = $BackButton

func _ready() -> void:
	if back_button != null:
		back_button.pressed.connect(_on_back)
	_build()

func _build() -> void:
	if grid == null or main == null:
		return
	for ch in range(LevelData.CHAPTER_COUNT):
		var header := Label.new()
		header.text = LevelData.chapter_name(ch)
		header.add_theme_font_size_override("font_size", 22)
		header.add_theme_color_override("font_color", LevelData.chapter_tint(ch))
		header.custom_minimum_size = Vector2(300, 30)
		grid.add_child(header)
		var spacer := Control.new()
		spacer.custom_minimum_size = Vector2(10, 30)
		grid.add_child(spacer)
		for n in range(LevelData.LEVELS_PER_CHAPTER):
			var idx: int = ch * LevelData.LEVELS_PER_CHAPTER + n
			var b := Button.new()
			b.text = "Level %d" % (n + 1)
			b.custom_minimum_size = Vector2(120, 36)
			b.focus_mode = Control.FOCUS_ALL
			var unlocked: bool = main.progress != null and main.progress.is_unlocked(idx)
			b.disabled = not unlocked
			if not unlocked:
				b.text = "L%d (locked)" % (n + 1)
			else:
				b.pressed.connect(_on_level_pressed.bind(idx))
			grid.add_child(b)
		var spacer2 := Control.new()
		spacer2.custom_minimum_size = Vector2(10, 30)
		grid.add_child(spacer2)

func _on_level_pressed(level_index: int) -> void:
	if main != null and main.has_method("start_level"):
		main.call("start_level", level_index)

func _on_back() -> void:
	if main != null and main.has_method("go_title"):
		main.call("go_title")
