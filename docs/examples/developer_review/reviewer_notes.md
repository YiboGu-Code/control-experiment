# Developer example review notes

The role integration test passed in `runs/developer-live-006`. The generated game's delivery status remains **partial**. These are development review notes, not Player evidence or an official QA decision. No hidden benchmark material was read.

## What was actually exercised

- The exact original `platformer-time-loop/instruction.md`, SHA-256 `dcb85ce30a25d88a5e0a12b6a0f91a8783e896d6816cdcb88d6b8432e5c10969`, was preserved byte for byte.
- The existing real Designer and Art planning outputs were imported as preconditions. Their references were rebased for the new workspace; the 12 mock images were unchanged. There were no new upstream model calls or image-generation calls.
- Two real `deepseek-flash` calls used the recorded non-thinking configuration. Call 1 returned `completed` with nonempty remaining work and was rejected before staging. Call 2 received the original response and actual validation error, then supplied the accepted proposal. The host did not rewrite model code or its JSON.
- Actual Godot `4.5.1.stable.official.f62fdbde1` ran in a network-disabled container using the immutable image ID in [sandbox_config.json](sandbox_config.json). Engine version, import, ordinary launch, `near_rewind` launch and `near_victory` launch all passed. No engine-error repair was needed in this successful live run; diagnostic-driven repair has separate offline tests.
- The committed project contains 16 model-authored source files and 12 unchanged mock images, plus engine-generated resource reference files. Its artifact hash is recorded in the role result and checked by the live test. All 24 model implementation claims remain `implemented_unverified`; none is QA-approved.
- The ordinary launch lasted five engine iterations. Each named scenario check lasted 60 iterations at fixed 60 FPS. This establishes limited startup behavior, not a completed rewind, victory, or replay.

## Source package and provenance

Open [project.godot](deliverables/game/project.godot) in Godot 4 and allow asset import. For a fresh command-line checkout, run these from the `deliverables` directory:

```text
godot --headless --path game --import
godot --path game
```

The source package omits the derived `.godot` cache. The passing sequence included import before startup; a bare launch of an unimported copy was not established. Import at `/workspace/game` when preparing the final Benchmark delivery. The role's current handoff template now states this explicitly; the archived handoff remains the exact artifact produced during live-006.

The model's remaining-work text was written **before** the host checks. Its statement that host logs had not returned is historically accurate for that response; [build_log.json](deliverables/build_log.json) supplies the subsequent successful check results. No archived model response, role result, report or source file has been edited to make the run look successful.

## Observations to hand to Player and QA

Static source reading found concrete concerns that basic launch checks do not resolve:

1. [LevelData.gd](deliverables/game/scripts/LevelData.gd) defines 24 indices, but its `build()` layout only varies by chapter; the six levels within each chapter reuse the same layout. Distinct puzzle content and chapter progression need review and repair.
2. [Level.gd](deliverables/game/scripts/Level.gd) leaves `handle_interact()` empty despite listing E as an interaction control. Enemy distraction currently changes a flag/tint; laser blocking adjusts beam length. Their complete gameplay consequences need implementation review, not a success claim based on startup.
3. [Main.gd](deliverables/game/scripts/Main.gd) initializes `near_victory` by setting held-switch flags, while the ordinary holder calculation overwrites those flags on the next tick. The supplied ghost samples do not establish a demonstrated solution. The `level_complete` scenario invokes a direct completion hook; it is not evidence of reaching the exit through ordinary rules.
4. `near_rewind` starts at 24 seconds. Its one-second startup check does not cross the rewind. Seeking advances the clock without a matching full current-loop recording, so its next ghost also needs behavioral verification.
5. Ghost replay, door collisions, jump reachability, all chapter puzzles, UI layout, save/progression behavior and deterministic replay require real interactions. No screenshot inspection, playthrough, official evaluation, or input-trace generation is claimed here. Player still owns the required `demo_outputs` traces.
6. The twelve source images remain mock placeholders. The two external libraries are not mounted in this local live run; their existing Art interfaces remain available for the server deployment.

The four-loop interpretation is at most three recorded ghosts plus the current controlled loop. This follows the original task's loop count and is explicitly recorded in the model input, without editing the instruction. The earlier Designer ambiguity remains visible in its historical source.

## Earlier development runs and total usage

| Run | Recorded result | Real model calls | Input tokens | Output tokens |
| --- | --- | ---: | ---: | ---: |
| [live-001](failed_attempts/live-001/review.md) | Thinking exhausted the 32,768-token output allowance; no final code | 1 | 38,844 | 32,768 |
| [live-002](failed_attempts/live-002/review.md) | Output truncated at 65,536 tokens | 1 | 38,844 | 65,536 |
| [live-003](failed_attempts/live-003/review.md) | Complete response rejected for inconsistent status/remaining-work fields | 1 | 38,819 | 19,186 |
| [live-004](failed_attempts/live-004/review.md) | Host `PermissionError` before model dispatch; precise cause not established | 0 | 0 | 0 |
| [live-005](failed_attempts/live-005/review.md) | Duplicate JSON status key; rejected before engine execution | 1 | 38,901 | 21,909 |
| [live-006](review.md) | One protocol correction, then all five actual engine checks passed | 2 | 98,652 | 35,071 |
| Total for this Developer development session | Includes rejected and truncated calls | 6 | 254,060 | 174,470 |

Usage is provider-reported token consumption, not a monetary bill. Historical Designer and Art model usage is separate. The earlier failed runs used earlier prompt/adapter revisions; their complete requests and configurations are retained. They are engineering trials, not six independent fixed-loop-versus-scheduler comparison samples.

The final adapter shares at most three model turns between protocol correction and engine-error repair. Both controller types use the same role implementation, prompt, tool boundary and configured limits. The comparison controllers themselves have not yet been implemented.

## Test verification

- Final full suite: `python -m pytest -q` — 238 passed, 5 skipped.
- Developer-specific offline suite: 34 passed, including controller-equivalent prompts, protected assets, incremental history, source deletion, protocol repair, engine diagnostics, timeouts, budgets and cleanup failure.
- Explicit real-model/Godot test: `python -m pytest -q -s tests/test_developer_live.py` with the documented opt-in — 1 passed for live-006.
- The full suite skips the three opt-in paid live tests, a Windows symlink privilege check and the older generic sandbox test requiring a separately supplied Python image. Those skips do not stand in for the actual Godot run above.
- Archived artifact hashes, exact response text, the original instruction bytes, review links and absence of configured gateway secrets were independently checked.
