extends RefCounted
## Persistent unlock progress saved under user://.

const SAVE_PATH := "user://timeloop_progress.cfg"

var highest_unlocked: int = 0

func _init() -> void:
	load_save()

func is_unlocked(level_index: int) -> bool:
	return level_index <= highest_unlocked

func unlock_next(level_index: int) -> void:
	if level_index + 1 > highest_unlocked:
		highest_unlocked = level_index + 1
		save()

func save() -> void:
	var cfg := ConfigFile.new()
	cfg.set_value("progress", "highest_unlocked", highest_unlocked)
	cfg.save(SAVE_PATH)

func load_save() -> void:
	var cfg := ConfigFile.new()
	if cfg.load(SAVE_PATH) == OK:
		highest_unlocked = int(cfg.get_value("progress", "highest_unlocked", 0))
