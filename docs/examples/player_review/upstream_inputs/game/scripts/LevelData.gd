extends RefCounted
## Procedural definition of the 24 levels (4 chapters x 6). Each level is
## described by a spawn point, exit crystal, required switches, interactables,
## platforms and enemies. Coordinates are in world pixels, 32px grid.

const LEVEL_COUNT: int = 24
const CHAPTER_COUNT: int = 4
const LEVELS_PER_CHAPTER: int = 6
const CH1_AMBER := Color(1.0, 0.71, 0.25)
const CH2_TEAL := Color(0.31, 0.76, 0.85)
const CH3_MAGENTA := Color(0.85, 0.31, 0.76)
const CH4_GOLD := Color(0.878, 0.816, 0.576)

static func chapter_of(level_index: int) -> int:
	return clampi(level_index / LEVELS_PER_CHAPTER, 0, CHAPTER_COUNT - 1)

static func chapter_tint(chapter: int) -> Color:
	match chapter:
		0: return CH1_AMBER
		1: return CH2_TEAL
		2: return CH3_MAGENTA
		_: return CH4_GOLD

static func level_name(level_index: int) -> String:
	var ch: int = chapter_of(level_index)
	var n: int = level_index - ch * LEVELS_PER_CHAPTER + 1
	return "%s - Level %d" % [chapter_name(ch), n]

static func chapter_name(ch: int) -> String:
	match ch:
		0: return "Chapter 1: Amber"
		1: return "Chapter 2: Teal"
		2: return "Chapter 3: Magenta"
		_: return "Chapter 4: Pale Gold"

static func max_ghosts(level_index: int) -> int:
	# Chapter 1: 1 ghost; Ch2: 2; Ch3: 3; Ch4: 3 (three recorded ghosts + player = 4 loops).
	var ch: int = chapter_of(level_index)
	match ch:
		0: return 1
		1: return 2
		2: return 3
		_: return 3

static func build(level_index: int) -> Dictionary:
	var ch: int = chapter_of(level_index)
	var n: int = level_index - ch * LEVELS_PER_CHAPTER
	var data: Dictionary = {
		"index": level_index,
		"chapter": ch,
		"max_ghosts": max_ghosts(level_index),
		"spawn": Vector2(96, 480),
		"exit": Vector2(1180, 400),
		"switches": [],
		"plates": [],
		"doors": [],
		"lasers": [],
		"enemies": [],
		"platforms": [],
		"required_switch_ids": [],
	}
	var platforms: Array = data["platforms"]
	# Common ground floor (wide).
	platforms.append(Rect2(0, 540, 1280, 40))
	# A raised ledge that contains the exit.
	platforms.append(Rect2(1120, 420, 160, 24))

	var switches: Array = data["switches"]
	var plates: Array = data["plates"]
	var doors: Array = data["doors"]
	var lasers: Array = data["lasers"]
	var enemies: Array = data["enemies"]
	var required: Array = data["required_switch_ids"]

	switches.append({"id": "s1", "pos": Vector2(360, 520)})
	required.append("s1")

	if ch >= 1:
		platforms.append(Rect2(520, 430, 220, 20))
		plates.append({"id": "p1", "pos": Vector2(600, 412)})
		required.append("p1")
	if ch >= 2:
		switches.append({"id": "s2", "pos": Vector2(760, 520)})
		required.append("s2")
		doors.append({"id": "d1", "pos": Vector2(900, 468), "h": 72})
		lasers.append({"emitter": Vector2(700, 480), "length": 220})
		enemies.append({"pos": Vector2(200, 508), "min_x": 160, "max_x": 480})
	if ch >= 3:
		plates.append({"id": "p2", "pos": Vector2(430, 512)})
		required.append("p2")
		switches.append({"id": "s3", "pos": Vector2(1000, 520)})
		required.append("s3")
		enemies.append({"pos": Vector2(860, 508), "min_x": 820, "max_x": 1080})
		lasers.append({"emitter": Vector2(1020, 500), "length": 160})

	if n == 0 and ch == 0:
		pass
	data["chapter_tint"] = chapter_tint(ch)
	data["name"] = level_name(level_index)
	return data
