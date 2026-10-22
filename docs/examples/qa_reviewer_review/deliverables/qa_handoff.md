# Internal QA handoff

Reviewed 116 acceptance criteria. Host withheld 18 unsupported criterion decisions and 3 invalid issue updates. See the retained model proposal and effective coverage for details.

Delivery ready: **False**.

Not ready. The build passes and a few requirements are evidenced (Godot 4 project layout except demo_outputs, clean headless launch, title first screen, four chapters of six level buttons, scenario skip-to-level and seeded entry, timer visible at 30.00, flash/rewind/respawn observed, several trace-format rules), but the game is not a shippable micro-game: the player falls out of the world (QA-002), interact is inert (QA-003), level content is a degenerate template with no temporal puzzles (QA-001), completion and reset were never observed (QA-005, QA-006), ghost interactions are unexercised (QA-004), the title trace is non-deterministic (QA-008), zero delivery traces are packaged (QA-007), and all art is mock placeholder (QA-010). Open blocker/major issues remain, and the host gates packaged_traces_replayed and final_art are false. Host abstained on 18 criterion decisions and withheld 3 issue updates. Host gate failures: packaged_traces_replayed, final_art, all_requirements_verified, no_blocking_issues, no_open_design_or_evidence_gaps.

## Instruction audit

The instruction is precise enough to design and audit against: 30-second loop, ghost replay of the previous loop, layered loops, top timeline bar, exit-crystal gating by required switches, reset, completion metrics, 24 levels in four chapters escalating to four-loop orchestrations, asset selection from mounted libraries into project assets/, fixed project layout including demo_outputs/, exact headless command, engine-flags-before-`--` rule, screenshot helper, 1-10 traces named *.json, 600-frame cap, deterministic replay, and named scenarios. Two dimensions are not verifiable by this text-only reviewer: the title-screen visual (overlapping clock hands and ghost silhouettes) and any rendered screenshot/polish claim; these must be settled by pixel inspection and are flagged insufficient, not failed. The Designer requirements faithfully restate the instruction with no over-constraint: the instruction does not fix key bindings, a scenario list, or a minimum ghost-switch gameplay recipe, so the authored control assumptions and scenario ids are design choices, not conflicts. The one real design concern is the authoring of level content, which is an implementation defect (degenerate content) rather than a contradiction of the instruction, and is tracked as a runtime/design issue for the Developer rather than a design_conflict verdict against a criterion. The asset libraries were reported unavailable in this environment; using mock placeholders does not satisfy the instruction's asset-sourcing requirement and remains an open art/asset gap.

## Requirements

| Requirement | Status |
| --- | --- |
| R001 | implemented_unverified |
| R002 | implemented_unverified |
| R003 | implemented_unverified |
| R004 | implemented_unverified |
| R005 | implemented_unverified |
| R006 | verified_fail |
| R007 | implemented_unverified |
| R008 | implemented_unverified |
| R009 | implemented_unverified |
| R010 | verified_fail |
| R011 | implemented_unverified |
| R012 | implemented_unverified |
| R013 | implemented_unverified |
| R014 | implemented_unverified |
| R015 | implemented_unverified |
| R016 | implemented_unverified |
| R017 | implemented_unverified |
| R018 | verified_fail |
| R019 | verified_fail |
| R020 | not_implemented |
| R021 | verified_fail |
| R022 | implemented_unverified |
| R023 | implemented_unverified |
| R024 | not_implemented |
| R025 | verified_fail |
| R026 | verified_fail |
| R027 | not_implemented |
| R028 | implemented_unverified |
| R029 | implemented_unverified |
| R030 | implemented_unverified |
| R031 | not_implemented |
| R032 | not_implemented |
| R033 | not_implemented |
| R034 | not_implemented |
| R035 | not_implemented |
| R036 | not_implemented |
| R037 | not_implemented |

## Issues

### QA-002 (major, open)

Player can leave the ground platform and fall unbounded with no out-of-bounds death or respawn, leaving the player unrecoverable and preventing completion attempts: near_rewind reaches y~34033 at frame 599 with velocity 8980 and near_victory reaches y~29004 at frame 450, both outside the 720-pixel view.

Suggested role: developer. Requirements: R010, R015, R018.

- Run scenario near_rewind; step off the ground platform; observe Player position y growing past 540 without any respawn within 600 frames.
- Run scenario near_victory; jump at frame 140 and move right; observe Player y beyond the platform with no recovery.

### QA-003 (major, open)

Interact input is inert: Level.handle_interact() is empty and no interaction radius exists, so the E action cannot trigger switches or objects. Switches only react to body-overlap queries, never to the player's interact action as specified.

Suggested role: developer. Requirements: R010, R012.

- Inspect scripts/Level.gd handle_interact(): the body is 'pass'.
- Launch a level, stand near a switch, send E; observe no switch state change.

### QA-004 (major, open)

Ghost physical interactions are unverified by any mapped playthrough: no run places a ghost on a switch/pressure plate, holds a door, blocks a laser, or distracts an enemy while the player acts elsewhere. A mapped scenario transcript covering each interaction with observed held/door-open/enemy-distracted/beam-length state is required, and a run to four active ghosts with per-ghost timeline markers.

Suggested role: player. Requirements: R003, R012, R013, R014.

- Provide a scenario and trace where a ghost occupies a switch and a plate while the player is distant; record ghost positions plus switches_held, door open, enemy distracted and laser beam length.
- Drive a loop to four active ghosts and capture four distinct per-ghost timeline markers at different positions.

### QA-005 (major, open)

The level-complete overlay path was never observed: no mapped playthrough drove the player to the exit crystal with all required switches held, so loops-used and final-loop exit time display remain unverified, and rejection-with-unheld-switch was never exercised.

Suggested role: player. Requirements: R015, R017.

- Using scenario near_victory, provide a trace that touches the exit while s1 is unheld (expect rejected flash) and then when s1 is held; capture CompleteOverlay visible with LoopsLabel and TimeLabel values.

### QA-006 (major, open)

Reset behavior is unverified: no mapped playthrough activates reset and confirms all ghosts are removed and the level restarts at spawn with a fresh 30-second timer and no retained replay. The reset_flow scenario exists but was never run.

Suggested role: player. Requirements: R016.

- Launch scenario reset_flow, click ResetButton (screen_rect 1080,8,184,40), and capture a frame where ghosts list is empty, tick=0, TimerLabel=30.00.

### QA-008 (major, open)

Title trace replay is non-deterministic: title_nav_probe failed both fresh replays with state and pixel mismatch at frame 0, traced to TitleScreen.gd driving hand rotation from Time.get_ticks_msec(), a wall-clock source. Deterministic demo replay requires a fixed simulated clock for title animation.

Suggested role: developer. Requirements: R028, R037.

- Run title_nav trace twice from fresh launches; observe comparison:title_nav_probe-replay-1/2 status failed with first_state_difference=0 and first_pixel_difference=0.
- Change TitleScreen animation to advance from a fixed simulated delta so title state is reproducible.

### QA-009 (major, open)

Full 30-second loop duration from a fresh clock (expiry at exactly 30s) and ordinary level entry starting the countdown are unexercised; the 600-frame cap prevents a full loop from an ordinary start and all rewind evidence used preloaded scenarios. A clock-exposing run and a normal (no scenario) level-entry trace are needed.

Suggested role: player. Requirements: R001, R009.

- Provide a scenario starting at tick=0 with TimerLabel 30.00 and capture the frame where tick crosses TICKS_PER_LOOP and TimerLabel reads 00.00 (or expose loop tick and verify expiry tick==900).
- Provide a normal (no scenario) trace that clicks the Level 1 button rect [678,130,300,36] and captures level entry with TimerLabel 30.00.

### QA-011 (minor, open)

The screenshot helper usage requirement is unverified: no mapped evidence records invoking /workspace/tools/screenshot.sh with --path/--out/--frames/--scenario, and rendered title, active-play, multi-ghost and completion screens have not been pixel-inspected by this reviewer.

Suggested role: player. Requirements: R024.

- Run /workspace/tools/screenshot.sh --path /workspace/game -- --out <file> --frames N and with --scenario <id>, and supply the rendered captures for title, active play, multi-ghost and completion screens.

### QA-012 (minor, open)

A drag trace has never been exercised: no mouse_down/mouse_move/mouse_up drag segment exists in any supplied trace, so drag ordering and bracketed-move semantics could not be validated even though the game does not currently need drags.

Suggested role: player. Requirements: R035.

- Provide a trace with mouse_down at a start point, at least one mouse_move, and mouse_up after the moves, and confirm replay respects the bracketed ordering.

### QA-host-evidence-gaps (major, open)

Acceptance remains unverified. Collect or correctly map current evidence for the listed criteria; no missing test is claimed as a gameplay defect.

Suggested role: player. Requirements: R002, R004, R005, R006, R007, R010, R011, R018, R019, R026, R027, R028, R029, R030, R031, R032, R033, R034, R036, R037.

- Review R002 criterion 2: Ghost positions advance with playback_frame, but no mapped evidence samples a ghost's recorded state against the contemporaneous recorded player stream to confirm frame-correspondence at the same loop time.
- Review R004 criterion 0: No authored level data requires simultaneous contributions from two prior ghosts before the current player reaches the exit, and no mapped playthrough demonstrates such cooperation; this is an implementation/content defect needing a mapped scenario, not a contradiction of the instruction.
- Review R004 criterion 1: Removing a ghost contribution is not shown to make the exit unreachable; the authored template uses independent switch/plate holds all reachable by the current player, so there is no observed removable ghost dependency.
- Review R004 criterion 2: No mapped playthrough keeps multiple ghost contributions alive across successive loops to confirm persistence until reset or the loop ceiling.
- Review R005 criterion 0: No mapped playthrough shows any level that cannot be solved in a single loop; the authored template is a collection of independent holds, but no run confirms single-loop solvability or non-solvability.
- Review R005 criterion 1: No observed solution schedules distinct actions in different loop iterations at specified times; the shared template provides no cross-time dependency in the inspected data or snapshots.
- Review R005 criterion 2: Earlier-loop ghost actions are not observed to be necessary for later-loop success in any mapped playthrough; only static level data is available and it does not encode this dependency.
- Review R006 criterion 1: Host withheld the model's 'pass' decision: Criterion evidence does not cover its requirement: R006. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R007 criterion 0: Title snapshot shows three HandA/HandB/HandC Sprite2D nodes at [640,340] (overlapping by position), but confirming rendering of overlapping clock-hand graphics requires pixel inspection, which this text-only model has not performed.
- Review R007 criterion 1: Ghost silhouettes on the title exist only via the mock backdrop texture; no node-level silhouette is present and pixel inspection is unavailable, and the mock art cannot establish final visual compliance.
- Review R007 criterion 2: Host withheld the model's 'pass' decision: Acceptance basis lacks a corresponding inspected source: R007. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R010 criterion 0: Host withheld the model's 'pass' decision: Criterion evidence does not cover its requirement: R010. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R010 criterion 1: No mapped observation captures Player velocity.y becoming negative (an upward jump) while grounded; the player instead fell off the platform, so grounded jump behavior is untested.
- Review R011 criterion 2: Ghost.gd sets modulate alpha 0.55, but confirming a translucent ghost is actually shown requires rendered pixel inspection, which this text-only model has not performed.
- Review R011 criterion 3: No mapped evidence proves the ghost reproduces the prior loop's exact state sequence rather than an approximation; matching replays cover the whole session but not an isolated ghost-vs-recording state comparison.
- Review R018 criterion 1: The ordinary path never demonstrated the full chain (title, level entry, timer expiry, ghost replay, exit completion, metrics): the title trace missed the Level 1 button and completion was never reached.
- Review R019 criterion 2: No OpenGameArt-derived file is present, so there are no LICENSE.txt obligations to check, but also no evidence of correct OGA usage or attribution.
- Review R026 criterion 0: The project viewport is 1280x720 and player traces use in-range coordinates, but they are not packaged delivery traces and only three were run, so fresh-launch 1280x720 evaluation is only partially exercised.
- Review R026 criterion 1: Most scenario replays matched, but title_nav diverged in both fresh replays (state and pixel mismatch), showing at least one trace is not reproducible from a fresh launch.
- Review R026 criterion 3: Player traces are exactly 600 frames (20s) and thus not sampled, but no packaged demo exists to assess random 20-second sampling robustness.
- Review R027 criterion 2: The ordinary trace never entered a level (later clicks missed the Level 1 rect), so it did not demonstrate movement, timer expiry, ghost replay, and an exit attempt in normal play.
- Review R028 criterion 0: near_rewind and near_victory replays matched, but title_nav failed both fresh replays at frame 0; determinism of the same trace is not established because a wall-clock title animation breaks exact replay.
- Review R028 criterion 1: Level.gd does not seed any RNG for ordinary play; the only seed(1337) is in Main._boot_scenario, so randomized elements in ordinary play are not shown to be seeded.
- Review R028 criterion 2: TitleScreen.gd drives hand rotation from Time.get_ticks_msec(), a timing-dependent value that causes the observed title replay divergence, so identical trace runs vary.
- Review R029 criterion 1: Host withheld the model's 'pass' decision: Criterion evidence does not cover its requirement: R029. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R030 criterion 1: near_victory frame 0 shows switches_held {s1:true} and one ghost matching intent, but near_rewind seeds three ghosts on a level whose max_ghosts is 1 and preloads tick=720 rather than a frame-zero state, so exact scenario-definition match at frame zero is not demonstrated.
- Review R031 criterion 1: Host withheld the model's 'pass' decision: Criterion evidence does not cover its requirement: R031. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R032 criterion 0: Host withheld the model's 'pass' decision: Acceptance basis lacks a corresponding inspected source: R032. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R032 criterion 1: Host withheld the model's 'pass' decision: Acceptance basis lacks a corresponding inspected source: R032. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R032 criterion 2: No mapped evidence confirms the evaluator interprets trace event frames at exactly 30 fps; the player harness ran at engine frame rate and cannot establish the 30 fps interpretation.
- Review R033 criterion 0: Host withheld the model's 'pass' decision: Acceptance basis lacks a corresponding inspected source: R033. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R033 criterion 1: Host withheld the model's 'pass' decision: Acceptance basis lacks a corresponding inspected source: R033. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R033 criterion 2: Host withheld the model's 'pass' decision: Acceptance basis lacks a corresponding inspected source: R033. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R034 criterion 0: Host withheld the model's 'pass' decision: Acceptance basis lacks a corresponding inspected source: R034. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R034 criterion 1: Host withheld the model's 'pass' decision: Acceptance basis lacks a corresponding inspected source: R034. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R034 criterion 2: Host withheld the model's 'pass' decision: Acceptance basis lacks a corresponding inspected source: R034. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R036 criterion 0: Host withheld the model's 'pass' decision: Acceptance basis lacks a corresponding inspected source: R036. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R036 criterion 1: No mapped evidence proves a wait event leaves game state unchanged; wait frames were only anchors and state changes at those frames were not isolated to wait behavior.
- Review R036 criterion 2: Host withheld the model's 'pass' decision: Acceptance basis lacks a corresponding inspected source: R036. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.
- Review R037 criterion 0: near_rewind and near_victory replays matched, but title_nav diverged in both replays, so two identical fresh replays of one trace do not always produce the same outcome.
- Review R037 criterion 2: Level completion was never observed in any trace, so timing, ghost playback, and level-completion reproducibility for a completing trace is untested.

### QA-host-failed-acceptance (major, open)

Address the failed acceptance criteria using their current cited evidence, then collect fresh verification.

Suggested role: developer. Requirements: R006, R019, R021, R025, R026.

- Review R006 criterion 2: Chapter 1 uses max_ghosts=1 but no level data distinguishes progressive escalation and no level requires four active loops; max_ghosts returns 1/2/3/3 with identical geometry, so the escalation to four-loop orchestrations is not implemented.
- Review R019 criterion 0: project-inventory lists only the asset paths, and the art manifest declares all 12 assets mock-image.v1 placeholders with both mounted libraries reported unavailable; imports are not sourced from the Kenney/OGA libraries.
- Review R019 criterion 1: No file in the project is Kenney-sourced; all art files match the mock placeholder set, so no CC0 Kenney assets are used.
- Review R021 criterion 2: scripts/, scenes/, and assets/ exist, but the project inventory contains no demo_outputs/ directory, so the required layout is incomplete.
- Review R025 criterion 0: The deliverable project inventory contains no demo_outputs/ directory or trace files; player traces live in the player artifact area, not the packaged game.
- Review R025 criterion 1: No .json trace files exist under the packaged demo_outputs/ per the project inventory.
- Review R025 criterion 2: There are no packaged trace files, so the one-trace-per-demo uniqueness condition cannot hold in the delivery package.
- Review R026 criterion 2: No traces are shipped in the package, so the delivery set is empty and the 'no more than 10 traces' condition is not demonstrated.

### QA-host-rejected-issues (major, open)

Some proposed model issue updates lack valid grounding. Existing issues remain unchanged unless a valid update was accepted.

Suggested role: qa_reviewer. Requirements: .

- Reassess model issue QA-001 using inspected current sources: Issue instruction excerpt is not verbatim
- Reassess model issue QA-007 using inspected current sources: Issue instruction excerpt is not verbatim
- Reassess model issue QA-010 using inspected current sources: Issue instruction excerpt is not verbatim

## Limits

- This review uses mapped current evidence only; no new play sessions were run and no image pixels were inspected by this text-only model.
- Visual acceptance (title clock hands, ghost translucency, timeline rendering, screenshots) is unsupported and flagged insufficient rather than failed.
- Player traces are a custom harness and are not packaged delivery traces; the wall-clock title-animation explanation for the replay divergence is plausible but not definitively proven.
- The 600-frame window cannot cover a full 30-second loop, limiting full-duration loop verification (R001 criterion 1, R009).
- Level-content degeneration is established from LevelData.gd and chapter-menu/template snapshots as a content defect and evidence gap; per-criterion ghost-dependency claims remain unverified without a dedicated two-ghost scenario.
- Internal review only; no official Benchmark score. Screenshot integrity is verified; this model received no image pixels.
