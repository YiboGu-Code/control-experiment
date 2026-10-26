# QA Reviewer: evidence-based internal acceptance

Concrete defect closure supports `IssueReview.resolved_criteria`, a list of
`{requirement_id, criterion_index}` identifying passing criteria that prove the
specific runtime/gameplay/trace/visual fix. Fresh successful evidence after the
previous review and linked inspected sources are required; visual closure needs
actual supplied pixels. Unrelated criteria, open gaps and all delivery gates
remain. Without this scope, or for design/evidence-gap issues, every affected
requirement must still be verified.

Optional [Developer MCP diagnostics](godot_mcp.md) are loaded from registered,
hash-checked result artifacts as `interpretation` sources. They have no acceptance
evidence IDs; visual feedback is explicitly a model interpretation. QA still has
no engine/MCP execution permission. A preview or Developer self-review cannot
replace current Player observations, replay comparisons, or pixels actually
supplied to the QA model.

QA Reviewer is the fifth worker in `role_design.md`. `QAReviewer` runs through
the existing `Executor` and publishes only requirement verification, issue
updates, readiness and report artifacts. It does not modify game files, assets,
design specifications or Player evidence, execute the game, call another role,
read hidden evaluation files, or produce an official Benchmark score.

## Public input and evidence integrity

The English input includes the unchanged Benchmark instruction, current
requirements/design, current Developer handoff, project inventory and source
text, Art manifest, current Player recordings, and existing issues. Controller
identity, ledger, secrets and hidden evaluator inputs are absent. Both
controllers use this same role, prompt and frozen `QAConfig`.

Reads are metered through the common tool facade. QA checks the registered
artifact hashes and game directory digest before analysis and rechecks the game
before publication. Developer build attempts must agree with the build log.
For the latest Player report describing this exact game, it independently checks:

- current game and Developer handoff identity;
- successful process receipt, fresh process/save directory, network isolation,
  and unchanged authored project;
- viewport, frame rate, harness version, complete ordered frame journals;
- actual executed events against the canonical input trace and exported trace;
- checkpoint JSON hashes and decoded RGBA screenshot hashes against journals;
- each replay comparison recomputed from the validated complete digest arrays.

The model sees a bounded projection of **raw** scene properties, with explicit
truncation counts. Player and Developer freeform interpretations are labeled as
claims and cannot serve as sole verification sources. PNG integrity checks do
not constitute visual inspection. The default role configuration remains
text-only and cannot pass or fail a criterion on a `visual` basis. In Fixed Loop,
`QA_VISION=true` enables up to eight validated capture screenshots as actual
image inputs. A visual decision must cite a screenshot supplied in that call,
current-version evidence and the corresponding requirement. Unseen frames and
behavior still remain evidence gaps. See the run 003 update below.

These checks establish consistency with recorded public artifacts. The existing
Player observer is not an adversarial anti-cheat oracle. Source relevance and
whether a criterion concerns static structure or observed behavior still require
semantic model/human judgment; the host does not claim to prove those judgments.

## Acceptance contract

`QAProposal.criteria` must cover every `(requirement_id, criterion_index)` exactly
once; indices are zero-based. Each row contains a verdict, basis, rationale,
current `evidence_ids` and inspected `source_ids`.

| Verdict | Meaning | Shared requirement status |
| --- | --- | --- |
| `pass` | Relevant evidence establishes this criterion | All criteria and dependencies must pass for `verified_pass` |
| `fail` | Relevant evidence establishes a contradiction | `verified_fail` if at least one fails and there is no design conflict |
| `insufficient` | Behavior is untested, incomplete or unsupported | Remains unverified; does not invent a gameplay failure |
| `design_conflict` | Designer criterion contradicts or overstates the original instruction | Remains unverified; route a quoted original-instruction issue to Designer |

Requirement wording, original excerpts and dependency mappings are preserved.
An originally `not_implemented` requirement without a verified verdict retains
that status. Previously verified results become unverified if the new review
cannot substantiate them. Dependency gaps prevent a full passing requirement.

Static acceptance requires inspected project text/inventory, build logs or art
provenance plus a current build evidence anchor. That anchor identifies the
revision, not a claim that the build proved gameplay. Runtime acceptance requires
raw session/state/trace evidence mapped to that requirement. Replay acceptance
requires a recomputed comparison with the claimed outcome. Unknown, stale,
unmapped, or unsupported citations are rejected. A successful playthrough
receipt establishes capture success, not successful completion of a game goal.

The report separates `proposal` (the original parsed model response) from
`effective_proposal` (the host-grounded decision). Invalid citations or missing
proof **cannot** publish a pass/fail: the host downgrades that criterion to
`insufficient`, records the exact reason in `grounding_notes`, and supplies a
concrete evidence-gap issue when needed. Invalid model issue updates are withheld;
existing issues are preserved and a reassessment request is added. Missing issue
coverage is filled with explicit follow-up requests, never invented observations.
Any such abstention vetoes readiness. The full raw response is retained unchanged.
This is deterministic validation and abstention, not a replacement mock response.

The model also audits the **original instruction** for omissions outside the
Designer list. Such issues can have no requirement IDs and include exact original
excerpts. The role does not alter the instruction to accommodate experimental
mock art or reinterpret a Designer mistake as a binding Benchmark requirement.

## Issue lifecycle and readiness

New issues use descriptive stable `QA-*` IDs. Repeated IDs update existing issues;
unmentioned historical issues remain. Issues include type, severity, suggested
role, affected requirements, evidence and concrete reproduction steps or a
requested follow-up test. Every non-pass criterion needs an open issue; missing
evidence and specification conflicts require their corresponding issue types.

Closing an existing issue requires a resolution, preserved affected requirements,
and new successful current evidence. With no explicit `resolved_criteria`, all
affected requirements must be verified; scoped concrete defects follow the
criterion-specific rules above without waiving remaining gaps.
That evidence must be newer than the prior finding/review, with inspected source
citations. Runtime/gameplay/trace issues cannot close on a build alone. QA does
not dispatch the suggested role; the controller decides what to do next.

The host derives readiness as the conjunction of the model recommendation and:

- a successful current build and actual captured play evidence;
- every requirement verified, including dependencies;
- 1–10 valid traces **inside the current game package**, matching actual captures
  with at least the recorded Player configuration's number of successful fresh
  comparisons for each trace;
- no mock art, open major/blocker issues, or open design/evidence gaps.

The packaging check is deliberately conservative: exact replay disagreement can
block this internal gate without proving that the game's outcome is
nondeterministic. QA preserves both the model recommendation and the effective
gate result so a veto is visible. It does not copy Player traces into the game;
a future controlled assembly step must produce a new version and matching
evidence. Successful QA work returns `completed` even when delivery is not ready.
The existing common controller stop rule still owns run termination.

## Bounds, failure behavior and output

Defaults: 4 model attempts, 24,000 output tokens per attempt, 1 MiB prompt,
512 KiB response, 512 registered artifact reads, 64 MiB input artifacts,
256 project files, 512 KiB source text, 32 sessions. The common executor adds
token/tool/time/output limits. Configurations remain fixed within a run.
Invalid JSON (including duplicate keys), schema violations, and missing coverage
receive bounded repair feedback in the same invocation budget. Repair prompts
contain the previous complete response so valid rows can be retained. Once the
proposal is structurally valid, the host records unsupported semantic citations
as abstentions rather than repeatedly paying to regenerate the full review.
Provider calls have no hidden retries. Exhausted structural repair attempts or
invalid input evidence fail without publishing QA state. Private provider
captures retain every attempted request/response for review.

Artifacts:

- `qa_report.json`: original/effective proposals, abstentions, statuses, gates, integrity counts, repairs;
- `coverage.json`: every criterion and its citations;
- `issues.json`: merged issue list;
- `readiness.json`: effective internal recommendation;
- `evidence_catalog.json`: complete inspected model input and provenance;
- `qa_handoff.md`: readable summary, statuses, issues and requested next work.

## Running the real example

The example uses the original `platformer-time-loop/instruction.md` and recorded
real Designer, Art planning, Developer and Player outputs. The original game's
immutable copy is `examples/inputs/player_developer_game`; it does not replace
the user's editor-modified preview. Player journals, traces and screenshots are
copied unchanged; only registered logical references in handoffs are rebased.
No upstream calls or engine runs are claimed as new. Images stay mock.

Configure `QA_MODEL`, `MODEL_GATEWAY_API_KEY`, `MODEL_GATEWAY_BASE_URL` in `.env`.
The adapter also accepts `QA_MAX_OUTPUT_TOKENS`, `QA_TIMEOUT_SECONDS`,
`QA_THINKING`, `QA_STREAM`, `QA_JSON_MODE`, `QA_TOKEN_PARAMETER`,
`QA_REASONING_EFFORT`, `QA_INPUT_TOKEN_MARGIN`. DeepSeek testing here uses
24,000 output tokens, 300 seconds and disabled thinking. Credentials and gateway
URLs are excluded from public configuration and model I/O exports.

```powershell
python -m pytest tests/test_qa_reviewer.py -q
$env:GAMECRAFT_QA_LIVE_TESTS = '1'
$env:GAMECRAFT_QA_LIVE_OUTPUT = 'E:/Game_Generation/adaptive_game_generation/runs/qa-review-new'
python -m pytest tests/test_qa_reviewer_live.py -q -s
```

Use a new output directory. The live example needs the Python `live` dependencies
and network access to the configured model. It does not require Docker because
it checks already recorded real execution. Review `review.md`, full provider
requests/responses, role input/output, public upstream artifact snapshot,
criterion coverage, issue list, readiness and resource accounting.

Offline tests use a clearly labeled protocol response fixture with real public
input/evidence. They test the implementation and never stand in for the live
model's semantic review.

On Windows, artifact publication uses the same bounded atomic-rename retry as
state journal replacement (six attempts, only transient error codes 5/32/33).
This handles short reader/indexer locks without rerunning a model call or
regenerating evidence. Persistent permission errors still fail publication.

## Visual review and bounded citation repair (run 003 follow-up)

Fixed Loop enables `max_grounding_repairs=1` within the existing four-call
allowance. Invalid citations are returned to the model once before conservative
host abstention. Unsupported claims still cannot become verified facts.

With `QA_VISION=true`, the runner sets `max_visual_images=8`. The reader verifies
the current project's captured screenshots against frame journals and selects a
bounded, deduplicated set across capture sessions. Actual pixels are attached to
the Chat request. A visual criterion must cite a supplied screenshot source and
its current play/replay evidence with the correct requirement mapping. Other
screenshots and unseen scenes cannot establish visual acceptance. Text-only mode
remains available and continues to reject visual pass/fail decisions.

Requests preserve labeled image content blocks; the evidence catalog lists
`visual_source_ids` and `screenshot_pixels_supplied`. Supplying pixels is not proof
that every visual requirement passed, and still images do not prove motion or
interaction. See [003 diagnosis](fixed_loop_real_images_003_diagnosis.md).
