# Game design specification: platformer-time-loop

Design version: 1; prompt version: designer.v2.

This is a design and acceptance plan, not a claim of implementation or verification.

## Changes in this revision

Initial English specification for Time Loop covering all eight design sections. It translates the public instruction into 37 grounded requirements, separates explicit requirements from assumptions, and defines observable acceptance, deterministic scenario/demo behavior, asset sourcing rules, project structure, and replay validation without claiming implementation or execution evidence.

Feedback issues: None

Changed requirements: R001, R002, R003, R004, R005, R006, R007, R008, R009, R010, R011, R012, R013, R014, R015, R016, R017, R018, R019, R020, R021, R022, R023, R024, R025, R026, R027, R028, R029, R030, R031, R032, R033, R034, R035, R036, R037

Changed sections: gameplay, controls, state_flow, content_plan, visual_feedback, asset_requirements, implementation_plan, validation_plan

## Explicit requirements and source grounding

### R001 — Time Loop is a per-level 30-second time-loop platformer: every level has a 30-second active loop duration before rewind.

Source excerpt:

> Each level is a 30-second loop.

Acceptance criteria:

- Entering any level initializes a visible countdown at 30 seconds.
- The active loop expires after exactly 30 seconds of gameplay time at the configured simulation rate.
- All 24 levels use the same 30-second loop duration.

Dependencies: None

### R002 — When the loop timer expires, the player is rewound and a ghost of the previous loop replays simultaneously.

Source excerpt:

> player starts again — but a ghost of the previous loop replays simultaneously

Acceptance criteria:

- At timer expiry the current player is restored to the level spawn point.
- A ghost instance is created at the spawn point and begins replaying the previous loop from t=0.
- Ghost movement and actions correspond to the recorded player state at the same loop time.

Dependencies: R001

### R003 — Ghosts can hold switches, distract enemies, and stand on pressure plates while the current player pursues other objectives.

Source excerpt:

> The ghost can hold switches, distract enemies, or
> stand on pressure plates while the current player tackles other objectives.

Acceptance criteria:

- Ghost occupancy of a switch or pressure plate counts as held while the ghost remains on it.
- Enemies within the defined distraction range enter a distracted state while a ghost is near.
- These ghost interactions can occur concurrently with independent player actions elsewhere in the level.

Dependencies: R002

### R004 — Loops layer so prior-loop ghosts can cooperate with the current player and with each other to solve a level.

Source excerpt:

> Multiple loops layer: loop 1's ghost holds a door open, loop 2's ghost stands
> on a platform to create a bridge, and in loop 3 the player finally reaches the
> exit using both ghosts' contributions.

Acceptance criteria:

- At least one authored level requires simultaneous contributions from two prior ghosts before the current player can reach the exit.
- Removing either required ghost contribution makes the exit unreachable.
- Ghost contributions persist across subsequent loops until reset or the maximum loop limit is reached.

Dependencies: R002, R003

### R005 — The puzzle design is temporal coordination: the player plans what each loop-self must do and when so all versions cooperate across time.

Source excerpt:

> The puzzle is temporal coordination —
> planning what each loop-self needs to do and when, so that all versions
> cooperate across time.

Acceptance criteria:

- At least one level cannot be solved in a single loop.
- The authored solution requires scheduling distinct actions in different loop iterations at specified times.
- Earlier-loop ghost actions are necessary for later-loop success.

Dependencies: R004

### R006 — The game contains twenty-four levels across four chapters, escalating from single-ghost puzzles to four-loop orchestrations.

Source excerpt:

> Twenty-four levels across four chapters escalate from
> single-ghost puzzles to four-loop orchestrations.

Acceptance criteria:

- Exactly 24 playable levels are defined in level data.
- The levels are grouped into four chapters with six levels per chapter.
- Chapter 1 levels require at most one ghost; the final chapter includes levels requiring four active loops.

Dependencies: R005

### R007 — The title screen visually shows overlapping clock hands and ghost silhouettes.

Source excerpt:

> A title screen shows overlapping clock hands and ghost silhouettes.

Acceptance criteria:

- The ordinary launch title screen displays at least two overlapping clock-hand graphics.
- Ghost silhouettes are visible on the title screen.
- The title screen is the first ordinary-entry screen before menus.

Dependencies: None

### R008 — A chapter menu reveals four chapters, each containing six levels.

Source excerpt:

> A chapter
> menu reveals four chapters of six levels each.

Acceptance criteria:

- The chapter menu presents four chapter entries.
- Each chapter entry exposes six level entries, for 24 total level entries.
- Each level entry displays a locked or unlocked state consistent with progression.

Dependencies: R006

### R009 — Selecting and entering a level starts its 30-second countdown.

Source excerpt:

> Entering a level starts a 30-second countdown.

Acceptance criteria:

- On level entry the countdown begins at 30.0 seconds.
- The countdown is visible during play.
- The countdown starts only after the level scene is ready and player control begins.

Dependencies: R001, R008

### R010 — The player can run, jump, and interact with switches and level objects.

Source excerpt:

> The player runs, jumps, and
> interacts with switches and objects.

Acceptance criteria:

- Horizontal movement responds to left and right input.
- Jump input produces a jump when the player is grounded.
- Interact input triggers eligible switches and interactive objects within range.

Dependencies: R009

### R011 — At zero timer the screen flashes and rewinds; the player restarts at spawn while a translucent ghost replays the previous loop exactly.

Source excerpt:

> When the timer hits zero, the screen
> flashes and rewinds — the player restarts at the spawn point, but a translucent
> ghost replays exactly what they did in the previous loop.

Acceptance criteria:

- A visible screen-flash effect occurs when the timer reaches zero.
- The level rewinds the current player to the spawn point.
- A ghost with alpha less than 1 is shown replaying the recorded previous loop.
- The ghost reproduces the previous loop's player state sequence rather than a newly generated approximation.

Dependencies: R002, R009

### R012 — Ghosts physically interact with the world by pressing buttons, holding doors, and blocking lasers.

Source excerpt:

> The ghost physically
> interacts with the world: it presses buttons, holds doors, and blocks lasers.

Acceptance criteria:

- A ghost overlapping a button or switch activates or holds it according to its type.
- A ghost standing at a door or door-hold point keeps the door open for as long as the replay remains there.
- A laser ray hitting a ghost is blocked at the ghost position instead of continuing to its normal target.

Dependencies: R003, R011

### R013 — The player can layer up to four loops, producing at most four active ghost instances.

Source excerpt:

> The player can layer up to four loops.

Acceptance criteria:

- The loop manager permits no more than four active ghost instances.
- The timeline and HUD represent each active ghost up to the four-loop maximum.
- Attempting another loop after four active ghosts follows the defined maximum-loop reset behavior rather than creating a fifth ghost.

Dependencies: R004

### R014 — A top timeline bar shows all active ghosts and their current positions in the 30-second loop window.

Source excerpt:

> A timeline bar at the top shows all
> active ghosts and their current positions in the 30-second window.

Acceptance criteria:

- The play HUD includes a timeline bar spanning 0 to 30 seconds.
- Each active ghost has a distinct marker on the timeline.
- Each ghost marker updates to reflect that ghost's current playback time within the loop.

Dependencies: R013

### R015 — Touching the exit crystal completes the level only when all required switches are held by ghosts or the current player.

Source excerpt:

> Reaching the
> exit crystal with all required switches held (by ghosts or player) completes
> the level.

Acceptance criteria:

- Each level defines the required switches needed for exit completion.
- The exit crystal rejects completion while any required switch is unheld.
- The exit crystal completes the level when all required switches are held by any combination of ghosts and the current player.

Dependencies: R003, R012

### R016 — A reset button clears all ghosts so the player can start the level fresh.

Source excerpt:

> A reset button clears all ghosts to start fresh.

Acceptance criteria:

- The play HUD provides a visible reset button.
- Activating reset removes all active ghosts.
- Activating reset restarts the current level at its spawn point with a fresh 30-second timer and no retained loop replay.

Dependencies: R013

### R017 — The level-complete display reports loops used and the time of exit within the final loop.

Source excerpt:

> Level-complete
> shows loops used and time of exit within the final loop.

Acceptance criteria:

- The level-complete overlay shows an integer count of loops used.
- The overlay shows the exit time within the final loop.
- The displayed values match the completed session's loop count and final-loop exit timestamp.

Dependencies: R015

### R018 — The deliverable is a complete, shippable micro-game built in Godot 4 at /workspace/game, not a prototype.

Source excerpt:

> Build **Time Loop**, a 30-second time loop platformer where past-self replays
> help solve puzzles in Godot 4 at `/workspace/game/`. This is not a prototype.
> It is a **complete, shippable micro-game** that could sit on an itch.io page or
> Steam as a polished vertical slice.

Acceptance criteria:

- A Godot 4 project exists at /workspace/game.
- The ordinary game path includes title, chapter menu, all 24 levels, loop rewinds, ghosts, timeline, reset, exit completion, and completion metrics.
- Core gameplay does not depend on placeholder-only debug controls or an unfinished test scene.

Dependencies: None

### R019 — 2D assets must be sourced from the mounted read-only Kenney CC0 library or OpenGameArt entries, respecting each OpenGameArt subdirectory license.

Source excerpt:

> 2D assets are mounted read-only at:
> 
> - `/workspace/assets/library/` — Kenney CC0 packs (sprites, tiles, UI, fonts).
> - `/workspace/assets/library-oga/` — OpenGameArt entries; respect each
>   subdir's `LICENSE.txt`.

Acceptance criteria:

- All imported visual assets come from the mounted asset libraries or from permissible engine primitives.
- Kenney-sourced files are used as CC0 assets.
- Any OpenGameArt-derived file is accompanied by or used in accordance with its subdirectory LICENSE.txt obligations.
- No unlicensed external asset is introduced.

Dependencies: R018

### R020 — The implementer must browse the mounted libraries, choose suitable packs, and copy needed files into the project's assets folder.

Source excerpt:

> Browse the library and choose packs.
> Copy what you need into your project's `assets/` folder.

Acceptance criteria:

- Selected asset files are copied under /workspace/game/assets/.
- Game scenes reference copied assets through project-relative res:// paths.
- No gameplay asset is loaded from a read-only mount at runtime.

Dependencies: R019

### R021 — The Godot project layout must include project.godot, Main.tscn, demo_outputs, scripts, scenes, and assets under /workspace/game.

Source excerpt:

> /workspace/game/
>   project.godot
>   Main.tscn
>   demo_outputs/    <- your input traces (1-10 files)
>   scripts/  scenes/  assets/

Acceptance criteria:

- /workspace/game/project.godot exists.
- /workspace/game/Main.tscn exists as the entry scene.
- /workspace/game/demo_outputs/, scripts/, scenes/, and assets/ exist.
- The project opens and runs from this layout without missing-path errors for required scenes or assets.

Dependencies: R018

### R022 — The built project must launch cleanly with the specified headless command.

Source excerpt:

> godot --headless --path /workspace/game --quit-after 5

Acceptance criteria:

- Running the exact command exits without script parse errors or missing-resource errors.
- The command completes within the five-second quit window.
- The project creates no failure dialog or blocking startup error in headless mode.

Dependencies: R021

### R023 — Engine flags such as --headless and --quit-after N must precede the -- separator; user scenario arguments must come after --.

Source excerpt:

> **Engine flags like `--headless` and `--quit-after N` must come BEFORE `--`** —
> anything after `--` is forwarded to the project as user args and silently
> ignored by the engine. Correct shape:
> `godot --headless --quit-after 5 --path . -- --scenario near_victory`.

Acceptance criteria:

- Launch commands place --headless, --quit-after, and --path before --.
- Scenario identifiers are placed after --.
- Game code reads scenario identifiers from OS.get_cmdline_user_args rather than engine flags.

Dependencies: R022

### R024 — A screenshot helper at /workspace/tools/screenshot.sh is available and must be used to inspect UI and result screens.

Source excerpt:

> A screenshot helper is available at `/workspace/tools/screenshot.sh`. Use it to actually see what your UI / battlefield /
> result screens look like.

Acceptance criteria:

- Validation can invoke the helper with --path /workspace/game and --out to produce a screenshot.
- The helper supports --frames and optional --scenario arguments.
- Title, active-play, multi-ghost, and completion screens are inspected through rendered screenshots.

Dependencies: R018

### R025 — The project ships between 1 and 10 input-trace files under demo_outputs, one per demo, each named with a .json extension.

Source excerpt:

> Ship **1-10 input-trace files** under `/workspace/game/demo_outputs/`, one per
> demo, each named `*.json`.

Acceptance criteria:

- /workspace/game/demo_outputs/ contains at least 1 and at most 10 trace files.
- Every trace file has the .json suffix.
- Each demo has exactly one trace file and filenames are unique.

Dependencies: R021

### R026 — The evaluator launches a fresh game per trace, replays synthetic mouse and keyboard input at 1280x720, records the screen, evaluates only the first 10 filenames, and samples recordings longer than 20 seconds.

Source excerpt:

> The evaluator launches a fresh game per trace,
> replays your trace as synthetic mouse and keyboard input at 1280x720, and
> records the screen. Only the first 10 traces by filename are evaluated;
> recordings longer than 20 s are sampled from a random 20 s window.

Acceptance criteria:

- Every trace is authored for the 1280x720 viewport.
- Each trace can run from a fresh game launch without depending on prior process state.
- No more than 10 trace files are shipped so that filename ordering does not exclude desired demos.
- No demo depends on content outside a random 20-second sample if its recording exceeds 20 seconds.

Dependencies: R025

### R027 — Normal play starts at the title screen and demonstrates the core gameplay loop.

Source excerpt:

> Normal play should start from the title screen and demonstrate the task's
> core gameplay loop.

Acceptance criteria:

- A trace with no scenario begins on the title screen.
- Accepting normal input from the title can reach the chapter menu and enter a level.
- The demonstrated normal flow includes movement, timer expiry, ghost replay, and an exit attempt.

Dependencies: R007, R009

### R028 — Demo playback must be deterministic.

Source excerpt:

> Demo playback must be deterministic.

Acceptance criteria:

- Replaying the same trace from a fresh launch produces the same outcome.
- Randomized elements are seeded or fixed for trace playback.
- No timing-dependent setup varies between identical trace runs.

Dependencies: R026

### R029 — Demos that need a specific state must use named scenarios that the game loads when launched with a scenario argument.

Source excerpt:

> For demos that need a specific state
> (a specific level, combat state, upgrade screen, result state, or late-game
> setup), define named scenarios your game loads when launched with:

Acceptance criteria:

- The project defines a finite set of named scenarios for level, loop, completion, and reset states.
- Each scenario is selectable through the documented --scenario launch form.
- Trace files may reference these scenario identifiers.

Dependencies: R028, R023

### R030 — When a scenario argument is present, the game skips menus, sets up the named state deterministically, seeds any RNG, and immediately accepts input.

Source excerpt:

> When `--scenario <id>` is present the game must skip menus, set up the named
> state deterministically (seed any RNG), and begin accepting input immediately.

Acceptance criteria:

- Scenario launch opens directly into the named gameplay or result state without title or chapter-menu steps.
- Level selection, active loop count, ghost positions, and required switches match the scenario definition at frame zero.
- All RNG used by the scenario is seeded deterministically.
- Player input is accepted immediately after scenario setup.

Dependencies: R029

### R031 — A trace's scenario field is optional; omitting it means normal launch from the title screen.

Source excerpt:

> `scenario` — optional; omit for a normal game launch from the title screen.

Acceptance criteria:

- A trace without a scenario field launches the ordinary title flow.
- A trace with a scenario field loads that named scenario.
- Scenario omission does not produce a parse error.

Dependencies: R029, R027

### R032 — The duration_frames field defines recorded frames at 30 fps with a maximum of 600 frames (20 seconds).

Source excerpt:

> `duration_frames` — total frames to record at 30 fps; cap at **600 (20 s)**.

Acceptance criteria:

- Every trace includes an integer duration_frames value.
- No trace exceeds 600 frames.
- Trace event frames are interpreted at 30 frames per second.

Dependencies: R025

### R033 — Trace events are time-ordered synthetic inputs with coordinates in the 1280x720 viewport and use the supported event types.

Source excerpt:

> `events` — time-ordered inputs. Coordinates are pixels in the 1280x720
>   viewport. Supported types:

Acceptance criteria:

- The events array is ordered by frame number.
- Mouse coordinates lie within the 1280x720 viewport.
- Only supported event types are used.

Dependencies: R032

### R034 — Keyboard trace events use one of the supported keycodes.

Source excerpt:

> `A`-`Z`, `0`-`9`, `ESCAPE`, `ENTER`, `SPACE`, `TAB`, `BACKSPACE`,
>     `DELETE`, `SHIFT`, `CTRL`, `ALT`, `UP`, `DOWN`, `LEFT`, `RIGHT`.

Acceptance criteria:

- Every key event uses a listed keycode.
- Keycodes outside the listed set are not emitted.
- Held and released keys use matching supported keycodes.

Dependencies: R033

### R035 — Mouse drag traces use mouse_down at the start, one or more mouse_move events, and mouse_up at the end; a mouse_click is a down-up pair at the same point.

Source excerpt:

> use these for drag interactions: emit `mouse_down` at the start point,
>     one or more `mouse_move` events along the way, and `mouse_up` at the end.
>     A `mouse_click` is a `mouse_down` + `mouse_up` at the same point in tight
>     succession.

Acceptance criteria:

- A drag trace emits mouse_down before its mouse_move events and mouse_up after them.
- Every mouse_move event lies between a matching down and up event.
- A mouse_click event is replayable as down and up at the same coordinates.

Dependencies: R033

### R036 — A wait event has a frame and type only and acts as an anchor frame with no input.

Source excerpt:

> `wait`: `{frame, type}` — anchor frame, no input.

Acceptance criteria:

- Wait events contain no input payload beyond frame and type.
- Wait events do not change game state.
- Wait events can anchor timing between other events.

Dependencies: R033

### R037 — Replay must be deterministic: the same trace on a fresh launch must produce the same outcome every time.

Source excerpt:

> Replay must be deterministic: same trace, fresh launch, same outcome every time.

Acceptance criteria:

- Two identical fresh-launch replays of one trace produce the same final state and outcome.
- No fresh-launch replay depends on mutable state from a prior replay.
- Timing, ghost playback, and level completion are reproducible for the same trace.

Dependencies: R028, R030

## Core gameplay

Related requirements: R001, R002, R003, R004, R005, R006, R009, R010, R011, R012, R013, R014, R015, R016, R017, R018

Time Loop is a side-view 2D platformer built around a repeating 30-second level loop. Entering a level starts the countdown. The player runs, jumps, and interacts with switches and objects. When the countdown reaches zero, the screen flashes, the level rewinds, the current player restarts at spawn, and a translucent ghost replays the previous loop exactly while physically interacting with buttons, doors, lasers, switches, pressure plates, and enemies. The player plans each loop-self's timing so that prior ghosts hold required switches or create traversal opportunities for later loops. Up to four loops may be layered. The top timeline shows each active ghost's position in the 30-second window. Touching the exit crystal completes the level only when all required switches are held by the current player or ghosts. Reset clears all ghosts and restarts fresh. Level-complete reports loops used and final-loop exit time. Twenty-four levels across four chapters escalate from single-ghost puzzles to four-loop orchestrations.

## Controls

Related requirements: R010, R013, R016, R030, R031, R033, R035, R036

Default keyboard bindings are A/D or Left/Right for horizontal movement, Space for jump, E for interact, R for reset, and Esc for pause or back navigation; UI buttons and level selection use the mouse. The reset button is clickable in the play HUD. Scenario launches begin accepting these controls immediately. Demo traces express the same actions through mouse_click, mouse_down, mouse_move, mouse_up, key_press, key_down, key_up, and wait events using the specified keycodes and 1280x720 coordinates. A mouse_click is a down-up pair; dragging uses down, move, and up.

## State transitions, outcomes, and retry

Related requirements: R007, R008, R009, R011, R015, R016, R017, R027, R029, R030, R031

Ordinary flow: title screen with overlapping clock hands and ghost silhouettes, then chapter menu with four chapters of six levels, then level play with 30-second countdown. Timer expiry is a loop transition: flash, rewind, player respawn, previous-loop ghost begins replay. The play HUD provides reset. Reaching the exit crystal with all required switches held enters level-complete, showing loops used and final-loop exit time, with options to continue, retry, or return to menus. Reset returns the current level to a clean first-loop state. A scenario launch bypasses menus and deterministically loads the named level or result state, seeds RNG, and accepts input immediately. A normal trace without a scenario starts from the title screen.

## Levels, content, and numeric configuration

Related requirements: R006, R008, R013, R014, R015, R017, R025, R029, R031, R032, R033, R034

Content consists of exactly 24 levels in four chapters of six levels. Chapter 1 (levels 1-6) introduces single-ghost switch holding and pressure-plate timing. Chapter 2 (7-12) requires two-loop cooperation with held doors and bridges. Chapter 3 (13-18) adds lasers, more complex enemy distraction, and three-loop timing. Chapter 4 (19-24) requires four-loop orchestrations. Level data fields include level id, chapter, display name, required switch ids, maximum active ghosts, spawn point, exit crystal, interactables, enemies, and initial camera bounds. Global parameters: 30-second loop, maximum 4 active ghosts, 1280x720 viewport, demo recording at 30 fps, duration cap 600 frames. Demo trace output defaults to six traces, within the allowed 1-10 range. Named scenarios include title_flow, level_1_single_ghost, level_2_two_loop, level_3_three_loop, level_complete, reset_flow, and final_chapter, each mapped to a deterministic setup.

## Visual feedback and UI states

Related requirements: R007, R011, R012, R014, R015, R017, R024

The title screen displays overlapping clock hands and ghost silhouettes. During play, the HUD shows the countdown and top timeline with ghost markers. Ghosts are rendered as translucent silhouettes distinguishable from the player. Timer expiry triggers a visible flash and rewind effect. Switches, pressure plates, doors, lasers, and the exit crystal visibly react when held, blocked, or activated by ghosts or the player. The reset control visibly clears ghost renders. Level-complete displays loops used and time of exit within the final loop. Rendered screenshots are inspected with the screenshot helper for title, active play, multi-ghost, and completion states.

## Asset requirements and integration

Related requirements: R018, R019, R020, R024

Use only the mounted read-only 2D asset libraries: Kenney CC0 packs under /workspace/assets/library/ and OpenGameArt entries under /workspace/assets/library-oga/. Select appropriate sprites, tiles, UI elements, and fonts, then copy needed files into /workspace/game/assets/. Required visual roles are background/tileset, player, ghost silhouette, switch, pressure plate, door, laser emitter and beam, enemy, exit crystal, HUD panel, timeline markers, buttons, and a readable font. One coherent tileset and UI style may serve all chapters, with color or decoration variants per chapter. Any OpenGameArt file must respect its subdirectory LICENSE.txt. If a suitable pack is unavailable, fall back to Godot primitive shapes, simple programmatic silhouettes, and built-in fonts; do not introduce unlicensed external assets. No specific pack, file count, or license beyond the mounted libraries is assumed.

## Implementation contracts and dependencies

Related requirements: R018, R020, R021, R022, R023, R025, R026, R029, R030, R031, R032, R033, R034, R035, R036, R037

Create a Godot 4 project at /workspace/game with project.godot, Main.tscn, scripts/, scenes/, assets/, and demo_outputs/. Systems: ScenarioManager parses OS.get_cmdline_user_args after -- and selects deterministic setup; LevelLoader loads level data; LoopManager runs the 30-second timer, records player state, rewinds, and creates ghost replays; GhostReplayer reproduces recorded states and drives physical interactions; Interactable components cover switches, pressure plates, doors, lasers, and exit requirements; PathDistractionEnemy handles ghost distraction; TimelineHUD renders active ghost markers; ResetController clears ghosts; CompletionOverlay reports loops used and exit time. Store level definitions and scenario mappings in data resources with fixed ids. Launch commands place engine flags before -- and scenario ids after --. Use the screenshot helper with --out, --frames, and optional --scenario for visual inspection. Demo traces follow the specified JSON schema and names.

## Observable acceptance and playthrough plan

Related requirements: R001, R002, R003, R004, R005, R006, R007, R008, R009, R010, R011, R012, R013, R014, R015, R016, R017, R018, R019, R020, R021, R022, R023, R024, R025, R026, R027, R028, R029, R030, R031, R032, R033, R034, R035, R036, R037

Validation is observational and reproducible. Clean-build check: run the mandated launch command and confirm clean exit without parse or missing-resource errors; verify engine flags before -- and scenario arguments after --. Structure and asset audit: confirm required project paths, copied assets, and license handling. Title and flow check: ordinary launch shows overlapping clock hands and ghost silhouettes, chapter menu shows four chapters of six levels, and selecting a level starts a 30-second countdown. Gameplay check: move, jump, interact, let the timer expire, observe flash, rewind, spawn reset, and exact translucent ghost replay; then verify ghost switch holding, door holding, laser blocking, enemy distraction, pressure plates, timeline markers, four-loop cap, reset clearing, exit gating by required switches, and completion metrics. Content audit: count 24 levels, four chapters of six, and confirm escalation from single ghost to four-loop levels. Scenario and demo check: run each trace from a fresh launch at 1280x720, confirm only first 10 filenames matter, duration_frames is at most 600, event types and keycodes are valid, drag semantics are correct, wait events are inert, omitted scenario follows normal title flow, present scenario skips menus and starts input immediately, and duplicate runs of one trace produce the same outcome. Visual check: use the screenshot helper for title, active play, multi-ghost, and completion screens. No validation result is presumed until actually observed.

## Assumptions (not explicit task requirements)

- Default controls are A/D or Left/Right for movement, Space for jump, E for interact, R for reset, Esc for pause or back, and mouse for UI selection; these are assumed because the instruction does not enumerate bindings.
- The game renders at a fixed 1280x720 viewport to match demo evaluation coordinates.
- Ghosts are non-controllable translucent silhouettes that replay recorded player states; they can activate switches, plates, doors, lasers, and enemy distraction, but do not block the current player's movement.
- A maximum of four ghost instances is enforced; if another loop would exceed four, the level performs the defined reset-to-fresh behavior rather than creating a fifth ghost.
- Enemies are distraction targets for ghost play and do not add a combat system beyond the explicitly mentioned distraction behavior.
- Levels use a linear four-chapter unlock order, but named scenarios may load any level or result state directly.
- The default demo set uses six trace files, within the required 1-10 range, covering title flow, single-ghost play, multi-loop play, reset, completion, and a final-chapter setup.
- The named scenario identifiers in the content plan are authored defaults because the instruction names the scenario mechanism but not a mandatory scenario list.
- Asset fallback uses Godot primitive shapes, programmatic silhouettes, and built-in fonts if a suitable mounted pack is unavailable; no additional asset library or license is assumed.
