# Review notes

This review preserves the unedited real Art planner response from `runs/art-producer-live-002`. The requested model was `deepseek-flash`; the call completed with `finish_reason=stop`. The executor committed 12 assets and returned `partial` because every generated image is an explicit mock placeholder.

## What is ready to inspect

- The original Time Loop instruction, the previously recorded real Designer specification, and the complete current Art model request are available in this review.
- The plan covers player and ghost sheets, a shared tileset, switches, pressure plates, doors, lasers, an enemy, the exit crystal, HUD elements, controls, and the title backdrop.
- Each asset has exact dimensions, a row-major frame inventory, a suggested Developer target, affected scenes, requirement references, source metadata, and integration notes.
- The raw response is preserved byte-for-byte; no missing fields were filled by hand. Model usage matches the ledger: 19,720 input tokens (18,048 cached) and 6,877 output tokens. The 12 image calls are local mock calls, not billed image-model calls.

## Limits of this result

The PNG files show labelled mock grids. They do not depict the characters, scenery, colors or animations described by the planner. Format and dimensions passed checks; visual quality, frame semantics, contrast, game integration, engine loading and gameplay remain untested. `visual_review=not_performed` is intentional.

Neither server asset library was mounted during the real planner run. The model received truthful unavailable statuses and chose the permitted generation route. Separate offline tests exercise both read-only filesystem interfaces with small synthetic asset files and per-entry licenses; those are not claims about the actual server catalogs. Original license text is authoritative when real assets are selected.

The style guide's palette, separate ghost sheet, chapter recoloring, built-in-font fallback, and suggested scene names are model-authored choices. Developer should reconcile them with the original instruction and the actual project structure. Decorative tiles do not authorize adding new game mechanics. The previous Designer review's ambiguity about four loops versus four ghosts remains unresolved by this Art role.

## Retained first attempt

[Attempt 001 summary](attempts/001/summary.json) and [original model reply](attempts/001/model_calls/001/response.txt) record the rejected first attempt. It omitted required `license_handling` fields and added an unsupported `generation_notes` field. Validation rejected it before any image calls or asset publication. Prompt v2 made the mandatory fields explicit; attempt 002 passed without response repair or automatic API retry. Both attempts and their costs remain in the project.

## Final implementation checks

The offline suite passed **194 tests**, with **4 skips**: two opt-in live suites, a Windows symlink privilege test, and an optional Docker test. The Art live suite was run separately and passed **1 test**. Static unused-name/import checking passed for the implementation and new test/example files.

A final byte comparison exposed an older Windows import behavior: attempt 002's public workspace copy used CRLF, while the Benchmark source, review copy, state instruction and actual model input retained the original LF content. The importer now preserves original instruction bytes, and both LF and CRLF inputs have regression tests through executor creation. The captured live attempt was not rewritten to disguise its historical workspace copy. The final example also checks workspace instruction bytes explicitly. This follow-up did not change the Art model prompt or its recorded response.

The final host setup additionally records a hash of library paths, limits and snapshot ID in the service configuration and rejects mounts overlapping the Benchmark or execution workspace. These additions were covered by the final offline suite; they do not imply a new live model call beyond the two retained attempts.
