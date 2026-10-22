# Player: real play, input capture and deterministic replay

Optional [Developer MCP inspection](godot_mcp.md) adds current-project diagnostic
hints to Player's input. These are test-planning hints, not captured inputs or
acceptance evidence. The common executor also permits Player to use the read-only
inspection service when a host adapter supplies it; the current Fixed Loop Player
still runs its existing capture/replay driver. MCP previews never replace its
independent sessions, frame journals, project hashes or replay comparisons.

Run 003 follow-up: Fixed Loop now allows five trials and five model turns per
invocation, including fresh replay of packaged inputs. The public per-trace cap
remains 600 frames. `PLAYER_VISION=true` sends up to eight checked, deduplicated
capture screenshots as actual image inputs in subsequent observation turns.
Only listed `visual_inputs` have supplied pixels; other image paths are not visual
observations. Use deterministic boundary scenarios for longer mechanics and
report missing scenario support to Developer. See [003 diagnosis](fixed_loop_real_images_003_diagnosis.md).

Player is the fourth implemented worker in `role_design.md`. It consumes the
current registered Developer game and handoff through the existing executor.
Both controllers use the same role, prompt, tools, configuration and budget.
The role publishes evidence; it cannot change game source, assets, requirement
verification, issues or readiness. [QA Reviewer](qa_reviewer.md) consumes this evidence in the next worker step.

The Fixed Loop runner enables `player.v2`: before model-selected exploration, the
host captures and replays every demo assembled into the current Developer project.
These trials consume the same trace and tool budgets. Original capture provenance
and exact bytes are checked; old-version replay verdicts are never reused.
Existing single-role examples retain v1. See [Fixed Loop](fixed_loop.md).

## Inputs and decisions

The role validates the current game's directory checksum and the Developer
report's revision/project reference. The English model input contains the
unchanged public Benchmark instruction, requirements, objective, controls,
supported scenarios, selected issues, requested historical traces and actual
runtime observations. It does not contain controller identity, private budget
history, hidden evaluation files or API credentials.

The initial six-frame ordinary-launch probe produces real scene observations
before the first model call. The model returns `PlayerDecision`:

| Decision | Behavior |
| --- | --- |
| `play` | Execute bounded trace plans, collect frames, replay each captured trace twice, return observations to the next model call |
| `finish` | Publish findings and coverage limitations based on executed sessions |
| `blocked` | Record the dependency preventing further play; no invented observations |

The default limit is four model calls, three traces, two replays per capture,
180 seconds per session and six screenshot/state checkpoints per trace. Invalid
JSON, duplicate keys, unknown requirement/scenario IDs and invalid events feed
back into the same bounded model-call budget. A correction is a paid call and
does not reset resources. The common executor separately enforces model, token,
tool, wall-time, output-size and cancellation limits. Subsequent invocations in
the same run must use the saved `PlayerConfig`.

At most four checkpoint entries may be requested by the model under the default
configuration; frame 0 and the final frame are added automatically. The prompt
schema exposes this limit, and feedback identifies every over-budget trial in
the proposed batch without executing any of them.

The current adapter uses a **text model**. It receives actual UI text, button
rectangles, transforms and bounded exposed script state, plus screenshot
references. It does **not** receive image pixels or claim visual understanding.
The full original snapshots and screenshots are retained for human/QA review;
model context filters nodes and explicitly records truncation.

## Real execution and isolation

`configs/player/player_driver.py` is trusted container code. For each probe,
capture or replay it copies the immutable published project into a new temporary
directory, imports it with Godot, creates empty HOME/XDG save/cache directories,
and starts a new software-rendered Godot instance under Xvfb. The published
project is mounted read-only. No game script, scene, physics tick rate, debug
method or stored progress is edited by Player.

The external `SceneTree` harness loads the project's actual main scene. It uses
`Input.parse_input_event` for real keyboard/mouse dispatch and captures the
viewport after drawing. It fixes rendered/input frames at 30 fps and 1280x720.
The project's own `--scenario` argument handles named scenarios; no unsupported
scenario is invented. Physics remains the project's configured rate.

The existing Docker backend enforces no network, a read-only container root,
non-root execution, CPU/memory/PID limits and cancellation cleanup. Only the
public input tree and this invocation's output are mounted; Benchmark source,
private state and `.env` are absent. Missing Docker/rendering support records
`not_run`; there is no host execution fallback. A cleanup failure aborts the
invocation and retains the executor's fail-closed behavior.

This observer runs in the same game process. Its captured data is useful for
development and replay diagnostics, **not a tamper-proof anti-cheat oracle for
adversarial game scripts**. It is not the hidden official Benchmark evaluator.

## Trace and replay semantics

The original task permits 1–10 JSON demos, at most 600 frames each. Player's
default three-trace budget is a subset of that allowance. The parser supports
the public keyboard, mouse and wait events, an optional scenario, integer
coordinates within the viewport and frame indices inside the duration.

Taps become explicit down/up events on adjacent frames. Held inputs are released
inside the final frame; unmatched releases, overlapping downs and final-frame
taps are rejected. Captured files contain these **executed canonical events**.
Replay reads the saved capture artifact back, instead of replaying an unchecked
model proposal. A failed/incomplete execution never becomes a demo artifact.

For every rendered frame the harness records SHA-256 of:

- the ordered, bounded scene snapshot (properties, script state and node paths);
- the actual viewport's RGBA8 pixel bytes.

The host verifies frame count/order, executed events, viewport, harness identity,
project checksum, selected snapshot hashes and decoded screenshot pixel hashes.
It also verifies all staged artifacts again after the last session, detecting
later alteration of earlier captures. Unexpected output files are rejected.

Each replay starts in a fresh process with empty user data and the same seed,
image and input trace. A replay passes only if **both complete digest sequences
match at every frame**. Reports include the first differing state/pixel frames.
Incomplete executions produce `not_run` comparisons; mismatches produce
`failed`, even if dispatch itself succeeded. Wall-clock APIs are not rewritten:
wall-clock animations can legitimately fail exact visual replay. Matching these
observables does not prove all hidden state, all seeds or cross-platform
determinism. Skipped/oversized properties are outside the observation boundary.

An execution `passed` means the real session and recording completed. It does
not mean a gameplay objective or requirement passed. Findings are explicitly
attributed model interpretations with session references and uncertainty.
`completed` requires a model conclusion, captured input, successful sessions
and all configured replays matching; otherwise usable evidence is `partial`.
Provider, budget, permission or commit failures preserve the common executor's
failure record rather than publishing unverified output.

## Artifacts and ownership

```text
artifacts/player/<invocation>/
  requests/<session>.json           # Canonical runtime job
  sessions/<session>/
    driver.json                    # Actual import/play commands and diagnostics
    receipt.json                   # Frame/event counts and viewport
    executed_trace.json            # Events actually dispatched
    events.jsonl                   # Dispatch journal
    frames.jsonl                   # State/pixel hashes for every frame
    frame_NNNN.json                 # Full selected scene observations
    frame_NNNN.png                  # Actual selected rendered screenshot
  logs/<session>.json               # Host validation and runtime-file references
  demo_outputs/<trace>.json         # Successful capture, public trace format
  player_report.json               # Sessions, comparisons, decisions, limitations
  player_handoff.md                # Readable evidence handoff
```

Every file is declared and checksummed. Evidence records bind to the current
game revision and associated requirement IDs without marking those requirements
verified. Requested old traces are rerun against the current game; prior
evidence is not relabeled as current. Player writes its own `demo_outputs/`
namespace; a later controlled assembly step must copy selected traces into the
final game package and validate that final revision. Final packaging is not
silently performed by mutating Developer's project.

## Setup and reproduction

Use any Python environment supported by the project, with its `live` and `test`
extras installed. Build the existing Godot image, then the Player extension:

```sh
docker build -t gamecraft-godot:4.5.1 configs/godot
docker build -t gamecraft-player:4.5.1 configs/player
```

The first command's context must match your existing Developer setup (see
[Developer setup](developer.md)); the Player Dockerfile accepts `GODOT_IMAGE`
as a build argument for a preloaded compatible base. It contains no API secrets
or hidden evaluator material. Preload the same image on the execution server;
the demo resolves the chosen tag to an immutable image ID before execution and
saves it in `sandbox_config.json`.

Configure `.env` using the existing shared gateway credentials:

```dotenv
PLAYER_MODEL=deepseek-flash
PLAYER_MAX_OUTPUT_TOKENS=8192
PLAYER_TIMEOUT_SECONDS=240
PLAYER_THINKING=disabled
PLAYER_SANDBOX_IMAGE=gamecraft-player:4.5.1
```

`PLAYER_THINKING` is an optional DeepSeek extension; omit it for endpoints that
do not support it. Other gateway options mirror existing role prefixes. API
credentials stay on the host and never appear in public settings or captures.
No new art calls occur: the sample imports recorded real Designer/Art/Developer
outputs, and the original images remain mock. The original Developer game is
preserved at `examples/inputs/player_developer_game/`; see its provenance README.

```sh
python examples/player_demo.py --output runs/player-review-new
python -m pytest tests/test_player.py -q
```

The demo output directory must be new. Opt-in live tests are separate:

```powershell
$env:GAMECRAFT_PLAYER_RUNTIME_TESTS='1'
python -m pytest tests/test_player_runtime_live.py -q
$env:GAMECRAFT_PLAYER_LIVE_TESTS='1'
$env:GAMECRAFT_PLAYER_LIVE_OUTPUT='runs/player-live-new'
python -m pytest tests/test_player_live.py -q -s
```

The runtime smoke test does not call a model. The full live test uses the actual
`.env` model and actual Godot renderer. Normal offline tests use explicit
synthetic boundary fixtures only; they are never presented as real gameplay.
Full live model request/response bodies, original instruction, role input/output,
tool audit, receipts, PNGs and replay results are linked from
[the Player review](examples/player_review/review.md).

## Engine references

- [Godot Input: actual input event dispatch](https://docs.godotengine.org/en/4.5/classes/class_input.html).
- [Godot SceneTree and main-loop lifecycle](https://docs.godotengine.org/en/4.5/tutorials/scripting/scene_tree.html).
- [Godot fixed-FPS recording](https://docs.godotengine.org/en/4.5/tutorials/animation/creating_movies.html).
