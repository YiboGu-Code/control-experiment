# Time Loop example data

`source.json` pins the original GameCraft-Bench instruction in the repository's
`FreedomIntelligence-gamecraft-bench-a433475` snapshot, including its SHA-256.
The example reads that source file directly and verifies its bytes before running.
There is no substitute instruction in this directory.

No model response fixture is stored here. The hand-authored offline unit test
fixture is isolated at `tests/fixtures/designer_proposal.json`; the live example
never reads it. Hidden evaluation files and reference solutions are not read or
imported.

Run `python examples/designer_demo.py --output <new-directory>` from the repository
after installing the package with its `live` extra. This always calls the real
model configured in `.env`. The export includes the original instruction, source
manifest, actual API request and response, model text, role result, English design
files when accepted, committed state, and tool audit. Failed calls are preserved
and never replaced with a fixture.
