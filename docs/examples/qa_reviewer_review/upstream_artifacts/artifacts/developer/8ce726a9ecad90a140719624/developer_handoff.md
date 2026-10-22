# Developer handoff

Game revision: `game-0001`. Basic build: **passed**.

## Changes

- Initial full implementation of Time Loop as a Godot 4 project at project root: title screen with rotating overlapping clock hands and the supplied backdrop, chapter menu (4 chapters x 6 levels with locked/unlocked state), 24 procedurally-defined levels with chapter escalation, a 30-second loop with deterministic 30 Hz player recording, ghost replays driven by recorded state, ghost-driven switch/plate/door/laser/enemy interactions, timeline HUD with per-ghost markers, reset, exit-crystal gating, and level-complete metrics (loops used + final-loop exit time). Uses the supplied Art package textures verbatim via res:// paths with nearest filtering and integer scaling; all art remains documented as mock placeholders. Interprets 'up to four loops' as three recorded ghosts plus the current controlled loop, as instructed. Deterministic named scenarios are implemented, with near_rewind and near_victory first. No input traces are fabricated (Player owns demo_outputs).

## Run

Open game/project.godot in Godot 4, or run `godot --path game` from this package. In the target Benchmark environment, deploy the game directory to /workspace/game.

## Controls

- A / Left Arrow: move left
- D / Right Arrow: move right
- Space / Up Arrow: jump
- E: interact
- R: reset level and clear all ghosts
- Esc: return to menu / back
- Mouse: menu and chapter/level selection, reset button

## Named scenarios

- `near_rewind`: Start level 1 with three recorded ghost replays present and the loop clock advanced to 24 seconds so the imminent rewind is exercised quickly.
- `near_victory`: Start level 1 with one recorded ghost and all required switches held, clock advanced to 12 seconds so the exit crystal is active and completion is one touch away.
- `title_flow`: Ordinary title screen; normal flow to chapter menu and gameplay.
- `level_1_single_ghost`: Chapter 1 level with one active ghost replay for a single-ghost puzzle demonstration.
- `level_2_two_loop`: Chapter 2 level with two active ghost replays exercising layered temporal cooperation.
- `level_3_three_loop`: Chapter 3 level with three active ghosts, plus doors, lasers and enemy distraction.
- `level_complete`: Loads level 1 already completed so the Level Complete overlay with loops used and exit time is shown.
- `reset_flow`: Level 1 with two ghosts loaded so the reset button can be demonstrated clearing all ghosts.
- `final_chapter`: Chapter 4 four-loop-orchestration level with three active ghosts and the maximum interactable set.

## Assets

- `assets/sprites/player_sheet.png`: player_sheet, source version 1, mock=True.
- `assets/sprites/ghost_sheet.png`: ghost_sheet, source version 1, mock=True.
- `assets/tiles/tileset_platform.png`: tileset_platform, source version 1, mock=True.
- `assets/props/switch_button.png`: switch_button, source version 1, mock=True.
- `assets/props/pressure_plate.png`: pressure_plate, source version 1, mock=True.
- `assets/props/door_gate.png`: door_gate, source version 1, mock=True.
- `assets/props/laser_hazard.png`: laser_hazard, source version 1, mock=True.
- `assets/sprites/enemy_patroller.png`: enemy_patroller, source version 1, mock=True.
- `assets/props/exit_crystal.png`: exit_crystal, source version 1, mock=True.
- `assets/ui/hud_panel.png`: hud_panel, source version 1, mock=True.
- `assets/ui/ui_controls.png`: ui_controls, source version 1, mock=True.
- `assets/ui/title_backdrop.png`: title_backdrop, source version 1, mock=True.

## Limitations and remaining validation

- Player owns generation of the input-trace files under demo_outputs/ and replay-based evidence; no traces are fabricated here.
- Visual review of mock-art placeholder assets and UI layout via the screenshot helper is still pending and not claimed as accepted.
- Host import, ordinary-launch and bounded scenario-launch checks have not yet returned logs; success is not claimed before they pass.
- In-engine gameplay verification (loop rewind, ghost holding of switches/plates/doors/lasers, reset and completion metrics) still needs to be observed; not yet claimed as verified.
- Basic engine checks do not verify gameplay, visual quality, replay behavior, or official Benchmark acceptance.
- Integrated Art Producer images are mock placeholders; final artwork and visual acceptance remain pending.

Player must produce real interactions, traces and replay evidence; QA must review the original instruction. No requirement was marked verified by Developer.
