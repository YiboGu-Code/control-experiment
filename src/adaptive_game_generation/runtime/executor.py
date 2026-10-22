"""Common serial runtime; control strategy and role implementations are injected."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable
from types import MappingProxyType

from pydantic import Field

from ..models import (
    ArtifactRef, ControllerKind, ErrorInfo, LedgerEvent, ResourceLimits, ResourceUsage,
    Role, RoleCallFinished, RoleCallStarted, RoleRequest, RoleResult, RunEnded, RunLedger,
    SchedulerDecision, SharedState, StateCommitted, TaskSpec, TerminationReason,
)
from ..models.base import Identifier, Model, PositiveCount, Seconds, Text, utc_now
from .budget import BudgetMeter
from .commit import merge_updates, validate_artifacts
from .errors import InvalidSubmission, PermissionDenied, RuntimeViolation, SandboxCleanupFailed
from .policy import RolePolicy
from .store import StateStore, atomic_text
from .tools import RoleTools, Services
from .workspace import Workspace, safe_path


class RuntimeConfig(Model):
    role_limits: ResourceLimits = Field(default_factory=lambda: ResourceLimits(
        max_model_calls=4, max_input_tokens=100000, max_output_tokens=16000,
        max_tool_calls=100, max_tool_seconds=240.0, max_wall_seconds=300.0,
    ))
    role_limits_by_role: dict[Role, ResourceLimits] = Field(default_factory=dict)
    scheduler_limits: ResourceLimits = Field(default_factory=lambda: ResourceLimits(
        max_model_calls=1, max_input_tokens=32000, max_output_tokens=2000,
        max_tool_calls=10, max_tool_seconds=60.0, max_wall_seconds=90.0,
    ))
    max_file_bytes: PositiveCount = 32 * 1024 * 1024
    max_output_bytes: PositiveCount = 256 * 1024 * 1024
    tool_timeout_seconds: Seconds = Field(default=60.0, gt=0)
    invocation_timeout_seconds: Seconds = Field(default=300.0, gt=0)
    # Public experiment identifier, never provider credentials or raw client config.
    service_configuration_id: Text = "unconfigured"

    def limits_for(self, role: Role | str) -> ResourceLimits:
        return self.role_limits_by_role.get(Role(role), self.role_limits)


@dataclass(frozen=True)
class RoleContext:
    request: RoleRequest
    state: SharedState
    tools: RoleTools


@dataclass(frozen=True)
class SchedulerContext:
    state: SharedState
    tools: RoleTools


class DecisionProposal(Model):
    selected_role: Role | None = None
    work_item_ids: tuple[Identifier, ...] = ()
    objective: Text | None = None
    completion_criteria: tuple[Text, ...] = ()
    rationale: Text


def error_info(exc: BaseException) -> ErrorInfo:
    if isinstance(exc, RuntimeViolation):
        return ErrorInfo(code=exc.code, message=str(exc))
    if isinstance(exc, asyncio.TimeoutError):
        return ErrorInfo(code="timeout", message="Invocation deadline exceeded")
    if isinstance(exc, asyncio.CancelledError):
        return ErrorInfo(code="cancelled", message="Invocation was cancelled")
    # Provider and host exception text may contain credentials or private paths.
    return ErrorInfo(code="execution_error", message=f"Invocation failed ({type(exc).__name__})")


class Executor:
    def __init__(self, workspace: Workspace, *, config: RuntimeConfig, services: Services = Services(),
                 services_by_role: dict[Role, Services] | None = None):
        self.workspace, self.config, self.services = workspace, config, services
        self.services_by_role = MappingProxyType({Role(k): v for k, v in (services_by_role or {}).items()})
        self.store, self.policy = StateStore(workspace.private), RolePolicy()

    @classmethod
    def create(
        cls, workspace: Workspace, task: TaskSpec, *, run_id: str,
        controller: ControllerKind | str, budget: ResourceLimits,
        config: RuntimeConfig | None = None, services: Services = Services(),
        services_by_role: dict[Role, Services] | None = None,
    ) -> Executor:
        instance = cls(workspace, config=config or RuntimeConfig(), services=services, services_by_role=services_by_role)
        with instance.store.locked():
            if instance.store.path.exists():
                raise ValueError("Run already exists; use Executor.open")
            if workspace.read_public("input/instruction.md").read_bytes().decode("utf-8") != task.instruction:
                raise InvalidSubmission("Task must match the imported instruction")
            config_ref = ArtifactRef(
                artifact_id="runtime-config", kind="config", path="input/runtime_config.json", created_by="system",
            )
            atomic_text(workspace.read_public(config_ref.path), instance.config.model_dump_json(indent=2))
            state = SharedState(
                task=task,
                ledger=RunLedger(run_id=run_id, task_id=task.task_id, controller=controller, budget=budget, config_ref=config_ref),
                artifacts=(*task.resource_refs, config_ref),
            )
            instance.store.save(state)
        return instance

    @classmethod
    def open(cls, workspace: Workspace, *, services: Services = Services(), config: RuntimeConfig | None = None,
             services_by_role: dict[Role, Services] | None = None) -> Executor:
        recorded = RuntimeConfig.model_validate_json(workspace.read_public("input/runtime_config.json").read_text(encoding="utf-8"))
        if config is not None and config != recorded:
            raise InvalidSubmission("Runtime configuration cannot change within a run")
        instance = cls(workspace, config=recorded, services=services, services_by_role=services_by_role)
        with instance.store.locked():
            instance._recover(instance.store.load())
        return instance

    @property
    def state(self) -> SharedState:
        with self.store.locked():
            return self.store.load()

    def request(self, role: Role | str, objective: str, completion_criteria: tuple[str, ...], *, work_item_ids=(), artifact_refs=(), request_id=None) -> RoleRequest:
        state = self.state
        return RoleRequest(
            request_id=request_id or uuid.uuid4().hex, run_id=state.ledger.run_id,
            task_id=state.task.task_id, state_revision=state.state_revision,
            role=role, objective=objective, completion_criteria=completion_criteria,
            work_item_ids=work_item_ids, artifact_refs=artifact_refs, limits=self.config.limits_for(role),
        )

    @staticmethod
    def _event(ledger: RunLedger, payload) -> RunLedger:
        timestamp = max(utc_now(), ledger.events[-1].recorded_at if ledger.events else ledger.started_at)
        return ledger.append(LedgerEvent(
            event_id=uuid.uuid4().hex, sequence=len(ledger.events) + 1, recorded_at=timestamp, payload=payload,
        ))

    def _meter(self, state: SharedState, invocation_id: str, limits: ResourceLimits) -> BudgetMeter:
        return BudgetMeter(
            state.ledger.budget, limits, state.ledger.usage, state.ledger.started_at,
            lambda usage: self.store.journal(invocation_id, usage),
            fallback_timeout=self.config.invocation_timeout_seconds,
        )

    def _tools(self, actor, invocation_id, work, meter):
        return RoleTools(
            actor, invocation_id, work, meter, self.store, self.policy,
            self.services_by_role.get(actor, self.services),
            max_file_bytes=self.config.max_file_bytes, max_output_bytes=self.config.max_output_bytes,
            tool_timeout=self.config.tool_timeout_seconds,
        )

    def _quarantine_orphan(self, actor: str, invocation_id: str) -> None:
        key = hashlib.sha256(invocation_id.encode()).hexdigest()[:24]
        path = safe_path(self.workspace.public, f"artifacts/{actor}/{key}")
        if path.exists():
            target = safe_path(self.workspace.private, f"quarantine/{uuid.uuid4().hex}")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, target)

    def _recover(self, state: SharedState) -> SharedState:
        finished = {
            event.payload.result.request_id for event in state.ledger.events
            if isinstance(event.payload, RoleCallFinished)
        }
        for event in state.ledger.events:
            if isinstance(event.payload, RoleCallStarted) and event.payload.request.request_id not in finished:
                request = event.payload.request
                usage = self.store.read_journal(request.request_id)
                usage = usage.revised(wall_seconds=max(usage.wall_seconds, (utc_now() - event.recorded_at).total_seconds()))
                result = RoleResult(
                    request_id=request.request_id, role=request.role, state_revision=request.state_revision,
                    status="failed", summary="Recovered interrupted invocation",
                    error=ErrorInfo(code="interrupted", message="Previous executor stopped before recording a result"),
                )
                self._quarantine_orphan(request.role.value, request.request_id)
                state = state.revised(ledger=self._event(state.ledger, RoleCallFinished(result=result, usage=usage)))
                self.store.save(state)
        pending = self.store.pending()
        if pending and pending["actor"] == "scheduler" and not any(
            isinstance(event.payload, SchedulerDecision) and event.payload.decision_id == pending["invocation_id"]
            for event in state.ledger.events
        ):
            usage = self.store.read_journal(pending["invocation_id"])
            usage = usage.revised(wall_seconds=max(usage.wall_seconds, (utc_now() - datetime.fromisoformat(pending["started_at"])).total_seconds()))
            decision = SchedulerDecision(
                decision_id=pending["invocation_id"], state_revision=state.state_revision,
                rationale="Recovered interrupted scheduler invocation", usage=usage,
                error=ErrorInfo(code="interrupted", message="Scheduler stopped before recording its decision"),
            )
            state = state.revised(ledger=self._event(state.ledger, decision))
            self.store.save(state)
        return state

    async def execute(self, request: RoleRequest, adapter: Callable[[RoleContext], Awaitable[RoleResult]]) -> RoleResult:
        with self.store.locked():
            state = self._recover(self.store.load())
            state.validate_request(request)
            if request.limits != self.config.limits_for(request.role):
                raise PermissionDenied("Per-call limits are injected by the runtime and cannot be overridden")
            self.store.start_journal(request.request_id, request.role.value, state.state_revision, utc_now().isoformat())
            started = self._event(state.ledger, RoleCallStarted(request=request))
            state = state.revised(ledger=started)
            self.store.save(state)
            meter = self._meter(state, request.request_id, request.limits)
            tools, work, result, failure = None, None, None, None
            try:
                work = self.workspace.begin(request.role, request.request_id)
                tools = self._tools(request.role, request.request_id, work, meter)
                meter.check_ready()
                result = await asyncio.wait_for(adapter(RoleContext(request, state, tools)), meter.remaining_time())
                result = RoleResult.model_validate(result)
                result.validate_for(request)
                meter.check_ready()
            except BaseException as exc:
                failure = exc
            finally:
                if tools is not None:
                    await tools.aclose()
            if failure is None:
                try:
                    self.policy.validate_result(state, result)
                    if result.status not in ("completed", "partial") and (result.artifact_refs or not result.proposed_updates.is_empty):
                        raise InvalidSubmission("Only completed/partial results may publish changes")
                    if work is not None:
                        validate_artifacts(state, result, work, self.config.max_output_bytes)
                    meter.check_ready()
                    fields = merge_updates(state, result, self.policy)
                    ledger = self._event(state.ledger, RoleCallFinished(result=result, usage=meter.snapshot()))
                    changed = any(getattr(state, name) != value for name, value in fields.items())
                    if changed:
                        ledger = self._event(ledger, StateCommitted(
                            previous_revision=state.state_revision, state_revision=state.state_revision + 1,
                            request_id=request.request_id, summary=result.summary,
                        ))
                    candidate = state.revised(**fields, ledger=ledger, state_revision=ledger.state_revision)
                    if result.artifact_refs and any(ref.key not in {old.key for old in state.artifacts} for ref in result.artifact_refs):
                        self.workspace.publish(work)
                    self.store.save(candidate)
                    return result
                except BaseException as exc:
                    failure = exc
            result = RoleResult(
                request_id=request.request_id, role=request.role, state_revision=request.state_revision,
                status="failed", summary="Invocation failed; no state changes were accepted", error=error_info(failure),
            )
            self._quarantine_orphan(request.role.value, request.request_id)
            failed = state.revised(ledger=self._event(state.ledger, RoleCallFinished(result=result, usage=meter.snapshot())))
            if isinstance(failure, SandboxCleanupFailed):
                failed = failed.revised(ledger=self._event(failed.ledger, RunEnded(
                    reason="failed", summary="Isolated process cleanup failed; this run cannot continue",
                    final_game_revision_id=failed.current_game_revision_id,
                    unresolved_issue_ids=tuple(i.issue_id for i in failed.issues if i.status != "closed"),
                )))
            self.store.save(failed)
            if isinstance(failure, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise failure
            return result

    async def decide(self, decision_id: str, adapter: Callable[[SchedulerContext], Awaitable[DecisionProposal]]) -> SchedulerDecision:
        """Meter scheduler decisions through the same capability boundary."""
        with self.store.locked():
            state = self._recover(self.store.load())
            if state.ledger.termination is not None or state.ledger.controller != ControllerKind.ADAPTIVE_SCHEDULER:
                raise PermissionDenied("This run cannot invoke an adaptive scheduler")
            if any(isinstance(e.payload, SchedulerDecision) and e.payload.decision_id == decision_id for e in state.ledger.events):
                raise InvalidSubmission("Decision ID has already been used")
            # Validate ID before creating a durable journal.
            SchedulerDecision(decision_id=decision_id, state_revision=state.state_revision, rationale="pending", usage=ResourceUsage())
            self.store.start_journal(decision_id, "scheduler", state.state_revision, utc_now().isoformat())
            meter = self._meter(state, decision_id, self.config.scheduler_limits)
            tools, failure, proposal = None, None, None
            try:
                work = self.workspace.begin("scheduler", decision_id)
                tools = self._tools("scheduler", decision_id, work, meter)
                meter.check_ready()
                proposal = DecisionProposal.model_validate(await asyncio.wait_for(adapter(SchedulerContext(state, tools)), meter.remaining_time()))
                meter.check_ready()
                known = {item.requirement_id for item in state.requirements} | {item.issue_id for item in state.issues}
                if set(proposal.work_item_ids) - known:
                    raise InvalidSubmission("Scheduler selected unknown work items")
            except BaseException as exc:
                failure = exc
            finally:
                if tools is not None:
                    await tools.aclose()
            decision = SchedulerDecision(
                decision_id=decision_id, state_revision=state.state_revision, usage=meter.snapshot(),
                selected_role=None if failure else proposal.selected_role,
                work_item_ids=() if failure else proposal.work_item_ids,
                objective=None if failure else proposal.objective,
                completion_criteria=() if failure else proposal.completion_criteria,
                rationale="Scheduler invocation failed" if failure else proposal.rationale,
                error=error_info(failure) if failure else None,
            )
            self.store.save(state.revised(ledger=self._event(state.ledger, decision)))
            if isinstance(failure, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise failure
            return decision

    def finish(self, reason: TerminationReason | str, summary: str) -> SharedState:
        with self.store.locked():
            state = self._recover(self.store.load())
            if state.ledger.termination is not None:
                raise InvalidSubmission("Run has already ended")
            if reason == TerminationReason.COMPLETED:
                if not completion_ready(state):
                    raise InvalidSubmission("Common completion checks have not passed")
            ledger = self._event(state.ledger, RunEnded(
                reason=reason, summary=summary, final_game_revision_id=state.current_game_revision_id,
                unresolved_issue_ids=tuple(i.issue_id for i in state.issues if i.status != "closed"),
            ))
            state = state.revised(ledger=ledger)
            self.store.save(state)
            return state


def completion_ready(state: SharedState) -> bool:
    """One completion predicate for every controller and Executor.finish."""
    game = next((g for g in state.game_revisions if g.revision_id == state.current_game_revision_id), None)
    replayed = any(e.kind == "replay" and e.outcome == "passed" and
                   e.game_revision_id == state.current_game_revision_id for e in state.evidence)
    return bool(
        game and game.build_status == "passed" and replayed and state.requirements
        and all(r.status == "verified_pass" for r in state.requirements)
        and state.readiness and state.readiness.ready
        and state.readiness.game_revision_id == state.current_game_revision_id
        and not any(i.status != "closed" and i.severity in ("blocker", "major") for i in state.issues)
    )
