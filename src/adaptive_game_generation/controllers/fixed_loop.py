"""Deterministic cyclic scheduling. No model chooses the next role or edits state."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Literal

from pydantic import Field

from ..models import Role, RoleCallFinished, SharedState
from ..models.base import Model, PositiveCount
from ..runtime import Executor, InvalidSubmission
from ..runtime.executor import completion_ready
from ..runtime.store import StateStore, atomic_text
from .work import WorkPolicy
from .common import FIXED_ORDER, OBJECTIVES, CRITERIA, budget_stop, role_limit_reached, role_request


class FixedLoopConfig(Model):
    policy_version: Literal["fixed-loop.v1"] = "fixed-loop.v1"
    max_role_calls: PositiveCount | None = None
    max_cycles: Annotated[int, Field(strict=True, ge=1, le=100)] = 3
    max_consecutive_failures: Annotated[int, Field(strict=True, ge=1, le=10)] = 2


class FixedLoop:
    def __init__(self, executor: Executor, adapters: dict, config: FixedLoopConfig | None = None, *, progress=None):
        self.executor = executor
        self.adapters = {Role(k): v for k, v in adapters.items()}
        if set(self.adapters) != set(FIXED_ORDER):
            raise ValueError("Exactly the five fixed-loop role adapters are required")
        self.config = config or FixedLoopConfig()
        self.progress = progress
        self.work = WorkPolicy(executor.workspace)
        self.store = StateStore(executor.workspace.private / "controller")

    def _write(self, path, value):
        atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def _record_interrupted_results(self, journal, path):
        state = self.executor.state
        results = {e.payload.result.request_id: e.payload.result for e in state.ledger.events
                   if isinstance(e.payload, RoleCallFinished)}
        for slot in journal["slots"]:
            result = results.get(slot["id"])
            if result is not None and not slot.get("status"):
                folder = self.store.directory / "calls" / slot["id"]
                self._write(folder / "result.json", result.model_dump(mode="json"))
                self._write(folder / "state_after.json", state.model_dump(mode="json"))
                slot.update(status=result.status.value, state_after=state.state_revision,
                            error=result.error.model_dump(mode="json") if result.error else None)
        self._write(path, journal)

    _budget_stop = staticmethod(budget_stop)

    async def run(self) -> SharedState:
        if self.executor.state.ledger.controller != "fixed_loop":
            raise InvalidSubmission("FixedLoop requires a fixed_loop ledger")
        # Hold a controller lock across slots, separate from the executor's per-call
        # lock. A second runner cannot interleave decisions between role invocations.
        with self.store.locked():
            path = self.store.directory / "checkpoint.json"
            if path.exists():
                journal = json.loads(path.read_text(encoding="utf-8"))
                if FixedLoopConfig.model_validate(journal["config"]) != self.config:
                    raise InvalidSubmission("Fixed-loop configuration cannot change within a run")
            else:
                journal = {"config": self.config.model_dump(mode="json"), "slots": []}
                self._write(path, journal)
            try:
                return await self._run(journal, path)
            except (asyncio.CancelledError, KeyboardInterrupt):
                self._record_interrupted_results(journal, path)
                if self.executor.state.ledger.termination is None:
                    self.executor.finish("cancelled", "The operator cancelled the fixed loop; committed outputs and charged calls are preserved.")
                raise
            except Exception:
                self._record_interrupted_results(journal, path)
                if self.executor.state.ledger.termination is None:
                    self.executor.finish("failed", "The controller stopped on a host or handoff error; inspect preserved artifacts and logs.")
                raise

    async def _run(self, journal, path):
        for cycle in range(1, self.config.max_cycles + 1):
            for role in FIXED_ORDER:
                state = self.executor.state
                if state.ledger.termination:
                    return state
                if completion_ready(state):
                    return self.executor.finish("completed", "Common current-game readiness checks passed.")
                identity = f"loop-{cycle:03d}-{role.value}"
                slot = next((s for s in journal["slots"] if s["id"] == identity), None)
                if slot and slot.get("status"):
                    continue
                if slot is None:
                    stop = self._budget_stop(state)
                    if stop:
                        return self.executor.finish(*stop)
                    run, reason, work = self.work.plan(state, role)
                    slot = {"id": identity, "cycle": cycle, "role": role.value, "decision": "run" if run else "skip",
                            "reason": reason, "work_item_ids": list(work), "state_before": state.state_revision}
                    if not run:
                        slot.update(status="skipped", state_after=state.state_revision)
                    journal["slots"].append(slot)
                    self._write(path, journal)
                    if self.progress:
                        self.progress(dict(slot))
                if slot["decision"] == "skip":
                    continue
                # Reconcile the authoritative ledger after a crash between execution
                # and checkpoint saving; never repeat an already charged request.
                completed = next((e.payload for e in state.ledger.events if isinstance(e.payload, RoleCallFinished)
                                  and e.payload.result.request_id == identity), None)
                folder = self.store.directory / "calls" / identity
                if completed is None:
                    if role_limit_reached(state, self.config.max_role_calls):
                        return self.executor.finish("iteration_limit", "The shared role invocation limit was reached.")
                    request = role_request(self.executor, role, identity, slot["work_item_ids"])
                    self._write(folder / "state_before.json", state.model_dump(mode="json"))
                    self._write(folder / "request.json", request.model_dump(mode="json"))
                    result = await self.executor.execute(request, self.adapters[role])
                else:
                    result = completed.result
                state = self.executor.state
                self._write(folder / "result.json", result.model_dump(mode="json"))
                self._write(folder / "state_after.json", state.model_dump(mode="json"))
                slot.update(status=result.status.value, state_after=state.state_revision,
                            error=result.error.model_dump(mode="json") if result.error else None)
                self._write(path, journal)
                if self.progress:
                    self.progress(dict(slot))
                if state.ledger.termination:
                    return state
                if result.error and result.error.code == "budget_exceeded":
                    reason = "timeout" if "deadline" in result.error.message.lower() else "budget_exhausted"
                    return self.executor.finish(reason, result.error.message)
                stop = self._budget_stop(state)
                if stop and not completion_ready(state):
                    return self.executor.finish(*stop)
                failures = 0
                for previous in reversed([s for s in journal["slots"] if s["role"] == role.value and s["decision"] == "run"]):
                    if previous.get("status") != "failed":
                        break
                    failures += 1
                if failures >= self.config.max_consecutive_failures:
                    return self.executor.finish("failed", f"{role.value} reached the consecutive invocation failure limit.")
            state = self.executor.state
            if completion_ready(state):
                return self.executor.finish("completed", "Common current-game readiness checks passed.")
            slots = [s for s in journal["slots"] if s["cycle"] == cycle]
            if all(s.get("status") in ("skipped", "no_op", "blocked") for s in slots):
                return self.executor.finish("stalled", "A complete fixed cycle made no applicable change; dependencies and unresolved issues are preserved.")
        return self.executor.finish("iteration_limit", "The configured fixed-cycle limit was reached; the latest committed game is retained without a readiness claim.")
