# Human review notes for the real QA example

The role implementation and live integration succeeded. This does **not** mean
the model's semantic judgments are ground truth. These notes were written after
reviewing the recorded outputs; they were not supplied to the model, are not
hidden Benchmark material, and do not rewrite the recorded response or state.

## What was actually run

- Original public Time Loop instruction, unchanged: 37 Designer requirements,
  116 acceptance criteria, the unchanged Developer game, and mock image assets.
- QA independently checked 10 recorded real Godot sessions, 5,406 frame journal
  entries and 47 PNGs. No new engine run or image generation was claimed.
- Final integration `runs/qa-live-005`: 2 real DeepSeek calls, 211,551 input tokens
  (195,072 reported cached), 28,953 output tokens. First response needed enum
  corrections; the second provided the complete review.
- Effective coverage: 36 criterion passes, 10 failures, 70 insufficient-evidence
  decisions. At requirement level: 0 verified passes, 7 verified failures,
  20 implemented/unverified and 10 still not implemented. These aggregate labels
  also depend on the pre-existing requirement dependencies and specification.
- Host withheld 18 criterion decisions and 3 issue updates because their
  citations/grounding were invalid. The raw and effective proposals are both in
  [qa_report.json](deliverables/qa_report.json). Twelve issues were committed.
- Delivery readiness is **false**. Missing packaged replayed traces, mock art,
  incomplete acceptance and open issues independently block delivery.

## Semantic findings that still require review

1. **Falling does not prove permanent non-recovery.** QA-002 says the player is
   unrecoverable. The near_victory capture's frame 599 shows automatic rewind
   returning the player to approximately `[96, 508]`. The defensible concern is
   prolonged falling with no immediate out-of-bounds reset before timed rewind.
   Do not report permanent softlock based on this record.
2. **Three ghosts plus the current player can represent four loops.** R006's
   failure rationale partly counts ghosts as loops. That arithmetic alone does
   not establish noncompliance. Level variety and required temporal cooperation
   still need a content review and successful play evidence. R013's Designer
   criteria also warrant checking against the original wording.
3. **An empty interaction handler is a static finding.** It is a useful repair
   lead, but runtime interaction acceptance needs an actual interaction trace.
   The original instruction does not mandate a particular key; automatic contact
   interactions and the chosen design controls must be distinguished. R010/R018
   currently contain stronger model conclusions than this static fact alone proves.
4. **Zero is within an upper bound of ten.** R026 criterion 2 is marked failed
   because the package has zero traces. That reasoning does not establish failure
   of a `no more than ten` condition. The separate minimum-one requirement R025
   and the host packaging gate correctly fail. The shipped project actually lacks
   `demo_outputs/`; generally, a file-only inventory cannot prove an empty directory
   is absent.
5. **Do not turn asset provenance into a requirement to use both libraries.**
   R019's Kenney-specific failure needs review: using one permitted source does
   not imply that the other must also be used. The user permits library extraction
   or generation in this development workflow. Preserve that permission and the
   unchanged original instruction; distinguish the Designer's stricter wording,
   library availability, licensing, and experimental mocks. Mock art independently
   prevents final-art acceptance; this is not a blanket prohibition on generation.
6. **Exact replay disagreement is narrower than game-outcome disagreement.**
   The two ordinary-title replays differ in clock-hand state/pixels from frame 0;
   four scenario replays match. This is a reproducibility concern, not proof that
   all gameplay outcomes differ. The model's title-replay issue should be read
   with that scope.
7. **Unexercised drag input is not automatically a mandatory game feature.** The
   instruction describes supported trace inputs; standalone mouse movement is
   allowed. Review R035's bracketing constraint and QA-012 before requesting a
   drag interaction that the game does not need.

The host checks citations, revisions, integrity, coverage and gates. It cannot
prove every natural-language inference or perfectly identify a runtime criterion
that the model labels `static`. In particular, the model's broad instruction
audit is overconfident. Read the above concerns before using `verified_fail`
labels as development priorities or treating this example as a semantic gold set.

The readiness rationale retains the original model narrative followed by host
gate failures; it may name model issues that were withheld. The committed
`issues.json`, `blocking_issue_ids`, `grounding_notes` and host gates identify the
effective state. The original narrative is retained for audit, not independently
endorsed by the host.

## Tests and development accounting

- Full offline regression before the final abstention/publication refinements:
  **304 passed, 2 environment skips, 6 live tests deselected**.
- Final QA tests: **31 passed**, plus the subsequently added failed-capture
  boundary test **1 passed** (32 QA tests in total).
- Publication/executor regression after the Windows rename fix: **51 passed,
  2 environment skips**. Provider tests: **31 passed**.
- Final real model integration: **1 passed**. The skips concern Windows symlink
  permission and an explicitly configured Docker image, not QA model execution.

All development attempts are in [development_attempts.json](development_attempts.json).
The first three runs failed strict protocol/citation repair, with no QA state
committed; the fourth failed import before any model call due to a Windows file
lock. The fifth passed. Total development usage, including failures: **12 real
calls, 1,516,713 input tokens, 162,107 output tokens, 0 image calls**. These are
development iterations with changing code/prompts, not comparable experimental
samples or official Benchmark scores.
