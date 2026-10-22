"""Exercise a minimal transaction on Time Loop without a model or game engine.

This step-2 smoke test covers one requirement, not the full Designer role.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from adaptive_game_generation.models import (
    ArtifactRef, DesignSpec, ProposedUpdates, Requirement, ResourceLimits, RoleResult,
)
from adaptive_game_generation.runtime import Executor, Workspace

TASK_DIRECTORY = Path(__file__).resolve().parents[1] / (
    "benchmark_resources/FreedomIntelligence-gamecraft-bench-a433475/tasks/platformer-time-loop"
)


async def designer(context):
    instruction = await context.tools.read_text("input/instruction.md")
    path = await context.tools.write_text("design.md", "# Design\n\n" + instruction)
    document = ArtifactRef(artifact_id="design", kind="document", path=path, created_by="designer")
    return RoleResult(
        request_id=context.request.request_id, role=context.request.role,
        state_revision=context.request.state_revision, status="completed", summary="Initial design recorded",
        artifact_refs=(document,), proposed_updates=ProposedUpdates(
            requirements=(Requirement(
                requirement_id="R1", description="Player can jump",
                source_excerpt="The player runs, jumps, and\ninteracts with switches and objects.",
                acceptance_criteria=("The configured jump action makes the player leave the ground",),
            ),), design=DesignSpec(version=1, document_ref=document, change_summary="Initial design"),
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="A new run directory")
    args = parser.parse_args()
    workspace, task = Workspace.prepare(
        args.output, TASK_DIRECTORY, task_id=TASK_DIRECTORY.name,
        benchmark_snapshot=TASK_DIRECTORY.parents[1].name,
    )
    executor = Executor.create(
        workspace, task, run_id="demo-run", controller="fixed_loop",
        budget=ResourceLimits(max_model_calls=0, max_tool_calls=5, max_wall_seconds=30.0),
    )
    request = executor.request("designer", "Record one movement requirement for a runtime smoke test", ("Requirement traces to instruction",))
    response = asyncio.run(executor.execute(request, designer))
    if response.status != "completed":
        raise RuntimeError(response.model_dump_json())
    state = executor.state
    print(f"Status: {response.status}; state revision: {state.state_revision}")
    print(f"Tool calls: {state.ledger.usage.tool_calls}; model calls: {state.ledger.usage.model_calls}")
    print(f"Design: {workspace.read_public(state.design.document_ref.path)}")
    print(f"State and ledger: {executor.store.path}")


if __name__ == "__main__":
    main()
