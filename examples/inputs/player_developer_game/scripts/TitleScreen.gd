extends Control
## Title screen: overlapping clock hands over the supplied backdrop plus a
## hover/pressable Play button using the shared UI atlas.

var main: Node = null

@onready var play_button: Button = $Menu/PlayButton
@onready var hands: Array = [$Hands/HandA, $Hands/HandB, $Hands/HandC]

func _ready() -> void:
	if play_button != null:
		play_button.pressed.connect(_on_play_pressed)

func _process(delta: float) -> void:
	var t: float = Time.get_ticks_msec() / 1000.0
	for i in range(hands.size()):
		var h: Sprite2D = hands[i]
		if h != null:
			h.rotation = t * (0.2 + 0.15 * float(i)) + float(i) * 0.7
	var _unused: float = delta

func _on_play_pressed() -> void:
	if main != null and main.has_method("go_chapters"):
		main.call("go_chapters")
