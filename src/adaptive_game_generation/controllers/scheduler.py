"""Serial adaptive control on the same workers, eligibility, ledger and delivery rules."""

import asyncio
import json

from ..models import RoleCallFinished, SchedulerDecision
from ..runtime import DecisionProposal, InvalidSubmission
from ..runtime.executor import completion_ready
from ..runtime.store import StateStore
from .common import FIXED_ORDER, budget_stop, role_limit_reached, role_request
from .results import write_json
from .scheduler_policy import ModelScheduler, SchedulerConfig, Selection, candidates_for, validate_selection
from .work import WorkPolicy


class AdaptiveScheduler:
    def __init__(self, executor, adapters, config=None, *, decision_adapter=None, progress=None):
        from ..models import Role
        self.executor = executor
        self.adapters = {Role(k): v for k, v in adapters.items()}
        if set(self.adapters) != set(FIXED_ORDER):
            raise ValueError("Exactly the same five worker adapters are required")
        self.config = config or SchedulerConfig()
        self.selector = decision_adapter or ModelScheduler(self.config)
        self.progress = progress
        self.work = WorkPolicy(executor.workspace)
        self.store = StateStore(executor.workspace.private / "controller")

    def _save(self, journal, path, slot=None):
        write_json(path, journal)
        if self.progress and slot:
            self.progress(dict(slot))

    def _reconcile(self, journal, path):
        state = self.executor.state
        decisions = {e.payload.decision_id: e.payload for e in state.ledger.events if isinstance(e.payload, SchedulerDecision)}
        results = {e.payload.result.request_id: e.payload.result for e in state.ledger.events if isinstance(e.payload, RoleCallFinished)}
        for slot in journal["slots"]:
            decision = decisions.get(slot["id"])
            if decision:
                write_json(self.store.directory / "decisions" / slot["id"] / "result.json", decision.model_dump(mode="json"))
                if decision.selected_role:
                    slot["role"] = decision.selected_role.value
                    identity = slot["id"] + "-" + decision.selected_role.value
                    result = results.get(identity)
                    if result and not slot.get("status"):
                        self._save_result(slot, identity, result, state)
                elif decision.error:
                    slot.update(status="decision_failed", error=decision.error.model_dump(mode="json"))
        self._save(journal, path)

    def _save_result(self, slot, identity, result, state):
        folder = self.store.directory / "calls" / identity
        write_json(folder / "result.json", result.model_dump(mode="json"))
        write_json(folder / "state_after.json", state.model_dump(mode="json"))
        slot.update(status=result.status.value, state_after=state.state_revision,
                    error=result.error.model_dump(mode="json") if result.error else None)

    def _finish_error(self, state, error):
        stop = budget_stop(state)
        if stop:
            return self.executor.finish(*stop)
        if error and error.code == "budget_exceeded":
            reason = "timeout" if "deadline" in error.message.lower() else "budget_exhausted"
            return self.executor.finish(reason, error.message)
        return None

    @staticmethod
    def _trailing(items, predicate):
        count = 0
        for item in reversed(items):
            if not predicate(item):
                break
            count += 1
        return count

    async def run(self):
        if self.executor.state.ledger.controller != "adaptive_scheduler":
            raise InvalidSubmission("AdaptiveScheduler requires an adaptive_scheduler ledger")
        with self.store.locked():
            path = self.store.directory / "checkpoint.json"
            if path.exists():
                journal = json.loads(path.read_text(encoding="utf-8"))
                if journal["config"] != self.config.model_dump(mode="json"):
                    raise InvalidSubmission("Scheduler configuration cannot change within a run")
            else:
                journal = {"config": self.config.model_dump(mode="json"), "slots": []}
                self._save(journal, path)
            try:
                self._reconcile(journal, path)
                return await self._run(journal, path)
            except (asyncio.CancelledError, KeyboardInterrupt):
                self._reconcile(journal, path)
                if self.executor.state.ledger.termination is None:
                    self.executor.finish("cancelled", "The operator cancelled scheduling; committed outputs and charged calls are preserved.")
                raise
            except Exception:
                self._reconcile(journal, path)
                if self.executor.state.ledger.termination is None:
                    self.executor.finish("failed", "The controller stopped on a host or handoff error; inspect preserved artifacts and logs.")
                raise

    async def _run(self, journal, path):
        for index in range(1, self.config.max_decisions + 1):
            state = self.executor.state
            if state.ledger.termination:
                return state
            if completion_ready(state):
                return self.executor.finish("completed", "Common current-game readiness checks passed.")
            identity = f"schedule-{index:03d}"
            slot = next((s for s in journal["slots"] if s["id"] == identity), None)
            # Re-evaluate stop conditions below even when resuming a completed slot.
            decisions = [e.payload for e in state.ledger.events if isinstance(e.payload, SchedulerDecision)]
            finished = [e.payload.result for e in state.ledger.events if isinstance(e.payload, RoleCallFinished)]
            stop = budget_stop(state)
            if stop:
                return self.executor.finish(*stop)
            if self._trailing(decisions, lambda d: d.error is not None) >= self.config.max_consecutive_failures:
                return self.executor.finish("failed", "Scheduler reached the consecutive decision failure limit.")
            for role in FIXED_ORDER:
                if self._trailing([r for r in finished if r.role == role], lambda r: r.status == "failed") >= self.config.max_consecutive_failures:
                    return self.executor.finish("failed", f"{role.value} reached the consecutive invocation failure limit.")
            no_progress = self._trailing(finished, lambda r: r.status in ("no_op", "blocked"))
            if no_progress >= self.config.max_no_progress_calls:
                return self.executor.finish("stalled", "Repeated role invocations made no applicable change.")
            if role_limit_reached(state, self.config.max_role_calls):
                return self.executor.finish("iteration_limit", "The shared role invocation limit was reached.")
            candidates = candidates_for(state, self.work, self.executor.config.limits_for)
            eligible = {c["role"] for c in candidates if c["eligible"]}
            if not eligible or (no_progress and eligible <= {r.role.value for r in finished[-no_progress:]}):
                return self.executor.finish("stalled", "No eligible work can make progress with the current dependencies.")
            if slot and slot.get("status"):
                continue
            if slot is None:
                slot = {"id": identity, "decision": "select", "state_before": state.state_revision,
                        "reason": "Select the next eligible role using public state and remaining resources"}
                journal["slots"].append(slot)
                self._save(journal, path, slot)
            folder = self.store.directory / "decisions" / identity
            decision = next((d for d in decisions if d.decision_id == identity), None)
            if decision is None:
                write_json(folder / "state_before.json", state.model_dump(mode="json"))
                write_json(folder / "candidates.json", candidates)
                revision, event_count = state.state_revision, len(state.ledger.events)

                async def choose(context):
                    if context.state.state_revision != revision or len(context.state.ledger.events) != event_count:
                        raise InvalidSubmission("State changed before scheduler dispatch")
                    proposal = DecisionProposal.model_validate(await self.selector(context, candidates, folder))
                    # Keep the host guard even when a custom decision adapter is injected.
                    selected = Selection(state_revision=revision, **proposal.model_dump())
                    validate_selection(selected, context.state, candidates)
                    return proposal

                decision = await self.executor.decide(identity, choose)
            write_json(folder / "result.json", decision.model_dump(mode="json"))
            state = self.executor.state
            write_json(folder / "state_after.json", state.model_dump(mode="json"))
            if decision.error:
                slot.update(status="decision_failed", error=decision.error.model_dump(mode="json"))
                self._save(journal, path, slot)
                stopped = self._finish_error(state, decision.error)
                if stopped:
                    return stopped
                continue
            role = decision.selected_role
            request_id = identity + "-" + role.value
            slot.update(role=role.value, work_item_ids=list(decision.work_item_ids), reason=decision.rationale)
            completed = next((r for r in finished if r.request_id == request_id), None)
            if completed is None:
                # A durable decision can be resumed, but never retargeted to changed state.
                selected = Selection(state_revision=decision.state_revision, selected_role=role,
                    work_item_ids=decision.work_item_ids, objective=decision.objective,
                    completion_criteria=decision.completion_criteria, rationale=decision.rationale)
                validate_selection(selected, state, candidates)
                stopped = self._finish_error(state, None)
                if stopped:
                    slot.update(status="not_dispatched", state_after=state.state_revision)
                    self._save(journal, path, slot)
                    return stopped
                request = role_request(self.executor, role, request_id, decision.work_item_ids,
                    objective=decision.objective, completion_criteria=decision.completion_criteria)
                call_folder = self.store.directory / "calls" / request_id
                write_json(call_folder / "state_before.json", state.model_dump(mode="json"))
                write_json(call_folder / "request.json", request.model_dump(mode="json"))
                self._save(journal, path, slot)
                completed = await self.executor.execute(request, self.adapters[role])
            state = self.executor.state
            self._save_result(slot, request_id, completed, state)
            self._save(journal, path, slot)
            if state.ledger.termination:
                return state
            if completion_ready(state):
                return self.executor.finish("completed", "Common current-game readiness checks passed.")
            stopped = self._finish_error(state, completed.error)
            if stopped:
                return stopped
            role_results = [e.payload.result for e in state.ledger.events if isinstance(e.payload, RoleCallFinished)
                            and e.payload.result.role == role]
            if self._trailing(role_results, lambda r: r.status == "failed") >= self.config.max_consecutive_failures:
                return self.executor.finish("failed", f"{role.value} reached the consecutive invocation failure limit.")
        # The last decision can fail without another loop iteration to examine it.
        state = self.executor.state
        decisions = [e.payload for e in state.ledger.events if isinstance(e.payload, SchedulerDecision)]
        if self._trailing(decisions, lambda d: d.error is not None) >= self.config.max_consecutive_failures:
            return self.executor.finish("failed", "Scheduler reached the consecutive decision failure limit.")
        return self.executor.finish("iteration_limit", "The configured decision limit was reached; the latest committed game is retained without a readiness claim.")
