# Review notes: actual evidence and limits

The final integration run, `player-live-004`, passed its execution/provenance
checks. The Player role correctly returned **partial**. It made four actual
`deepseek-flash` calls: an invalid plan, a corrected plan, a rejected finish
interpretation, and a corrected finish. Three real 600-frame traces were
captured, each replayed twice in a new process with empty user data. The image
assets are the unchanged recorded mock images; the screenshots themselves are
real Godot renders. No source repair or QA verdict was made.

| Trace | Actual behavior | Two fresh replay results |
| --- | --- | --- |
| `title_nav_probe` | Play click reached ChapterMenu; later clicks missed Level 1 | Both failed state and pixel comparison from frame 0 |
| `near_rewind_probe` | Movement, jump, timed rewind/flash and ghost restart; later left platform | Both matched all 600 state and pixel digests |
| `near_victory_probe` | Movement/jump; left platform without touching exit; later timed rewind | Both matched all 600 state and pixel digests |

There are ten actual sessions including the initial probe, 5,406 recorded
frames, and 47 rendered PNG checkpoints. The final run used 184,425 input tokens
and 6,937 output tokens, with zero image calls. The integration status does not
turn a failed replay or an unreached gameplay objective into a pass.

## Evidence interpretation

1. **Title determinism:** compare frame 0 in the title capture and either replay.
   The three `Main/TitleScreen/Hands/Hand*` rotation values differ. The recorded
   pixel hashes differ too. This supports a title-animation nondeterminism
   finding. It does not establish that all later gameplay is nondeterministic.
2. **Ordinary navigation remains incomplete:** the actual level-select clicks
   were `(300,360)` and `(300,300)`. The observed enabled Level 1 button is at
   `[678,130,300,36]`. These clicks missed. Do not treat this trace as proof that
   the game refuses a valid Level 1 click. The model consumed all three trial
   slots in one batch, so it had no slot left for an observed-coordinate retry.
   This is a real limitation of this bounded Player run, retained in the result.
3. **Rewind is observed:** `near_rewind_probe-capture/frame_0000.json` has tick
   720; frame 180 has tick 0, `flashing=true`, and player position `[96,480]`.
   Frame 240 has tick 60 and HUD `28.00`. This tests a preloaded near-rewind
   window, not a complete 30-second ordinary-start loop.
4. **Initial HUD is stale for one checkpoint:** frame 0 initially displays
   `30.00` and `Loop 1 / 4` while the scenario has already set later simulation
   state. Later checkpoints show the actual progressed timer/loop. Some model
   prose conflates these checkpoints; the JSON observations are authoritative.
5. **Falling is observed, permanent inability to recover is not proved:** at
   near-rewind frame 599 the player is approximately `[-344,34183]` with downward
   velocity 8980. In near-victory the player is approximately `[1416,29143]` at
   frame 450, then returns to `[96,508]` after the timed rewind by frame 599.
   The model's wording about being unrecoverable is too strong; the observed
   concern is extended off-screen falling before the next loop reset.
6. **Victory is untested:** near-victory checkpoints keep `completed=false` and
   the completion overlay hidden. The captured input did not reach the exit.
   A passing replay means the same unsuccessful attempt repeats, not that exit
   gating or victory works. Switch/plate/laser/enemy interactions, reset-button
   behavior, complete levels and later chapters remain unverified.

The text model received actual scene observations and executed input traces,
not image pixels. Its free-text findings can still contain mistakes; they are
stored verbatim as attributed interpretations. Human/QA should use the linked
JSON checkpoints, event journals and PNGs to assess them. The public custom
harness is not the official hidden evaluator and is not an adversarially
tamper-proof measurement boundary.

## Full input/output and development failures

- [Original task instruction](instruction.md), SHA-256
  `dcb85ce30a25d88a5e0a12b6a0f91a8783e896d6816cdcb88d6b8432e5c10969`.
- [Original Developer project](upstream_inputs/game/project.godot), directory
  SHA-256 `eb929b96a5bdd894db35ea59191e8f109a38221372d62b6d5d28a8ab4f244d22`.
- [Complete model I/O index and all output links](review.md).
- [All development attempts and measured resource usage](development_attempts.json).

The earlier Developer review's browsable project had been converted by a local
Godot editor. Those user-local changes were preserved. This test uses a verified
copy of the original published game, as documented in `examples/inputs/README.md`.

Failures were retained rather than silently removed:

| Attempt | Outcome | Retained material |
| --- | --- | --- |
| 001 | Two real model calls and seven actual sessions; final publication failed with a transient Windows journal replacement error | `prior_attempts/001/` includes full calls, ledger and uncommitted runtime output |
| 002 | Precondition import failed on the same Windows error before any model call | `prior_attempts/002/state_after.json` records the failed call and zero model usage |
| 003 | Four real model calls repeatedly exceeded the checkpoint limit; only the probe ran | `prior_attempts/003/` retains all rejected replies and partial report |
| 004 | Final complete integration; one plan repair and one invalid finding-category repair, three actual captures and six replays | Main review bundle |

Across these four full pipeline attempts: **10 real model calls, 321,182 input
tokens, 12,910 output tokens, zero image calls**. Engine-only smoke tests and
offline regression tests are separate; no API cost estimate is inferred.

The Windows fix retries only a short-lived failed atomic file replacement,
never the model request or game session. Persistent access failures still
abort. Checkpoint limits now appear directly in the model schema and every
invalid trial receives a specific error. These are implementation changes made
during development; freeze this final configuration before comparing control
strategies. This single-task demonstration is not a Fixed Loop versus Scheduler
performance result.

## Validation and artifact spot checks

The full offline regression run passed 274 tests and skipped seven opt-in or
environment-dependent tests. One subsequently added interior-frame comparison
test passed separately, giving 275 passing offline tests in the final tree.
The real-model integration test and real-engine movement/one-frame-jump test
each passed separately. See [validation details](validation.json).

The coding assistant also opened two actual PNGs for a limited artifact spot
check (separate from the text-only Player model):

- [Chapter menu, frame 120](deliverables/sessions/title_nav_probe-capture/frame_0120.png)
  shows the enabled Level 1 button near the top-right portion of the panel and
  chapter/level layout that extends beyond the visible width.
- [Post-rewind gameplay, frame 240](deliverables/sessions/near_rewind_probe-capture/frame_0240.png)
  shows `28.00`, `Loop 4 / 4`, mock-labeled assets and large gray areas around
  the visible world. Finished-art quality and complete visual layout were not
  accepted by this implementation task.
