"""Typed append-only event values, including scheduler and failed-call usage."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from .artifacts import ArtifactKind, ArtifactRef
from .base import (
    ControllerKind, Count, Identifier, Model, PositiveCount, Role, Text,
    VersionedModel, require_unique, utc_now,
)
from .contracts import ErrorInfo, RoleRequest, RoleResult
from .resources import ResourceLimits, ResourceUsage


class RoleCallStarted(Model):
    kind: Literal["role_call_started"] = "role_call_started"
    request: RoleRequest


class RoleCallFinished(Model):
    kind: Literal["role_call_finished"] = "role_call_finished"
    result: RoleResult
    # Measured by the executor, not trusted from the role's response.
    usage: ResourceUsage


class SchedulerDecision(Model):
    kind: Literal["scheduler_decision"] = "scheduler_decision"
    decision_id: Identifier
    state_revision: Count
    selected_role: Role | None = None
    work_item_ids: tuple[Identifier, ...] = ()
    objective: Text | None = None
    completion_criteria: tuple[Text, ...] = ()
    rationale: Text
    usage: ResourceUsage
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def failed_decision_selects_nothing(self) -> SchedulerDecision:
        if self.error is not None and self.selected_role is not None:
            raise ValueError("failed scheduler decision cannot select a role")
        return self


class StateCommitted(Model):
    kind: Literal["state_committed"] = "state_committed"
    previous_revision: Count
    state_revision: PositiveCount
    request_id: Identifier | None = None
    summary: Text

    @model_validator(mode="after")
    def increments_once(self) -> StateCommitted:
        if self.state_revision != self.previous_revision + 1:
            raise ValueError("state commit must increment the revision by one")
        return self


class TerminationReason(str, Enum):
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TIMEOUT = "timeout"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ITERATION_LIMIT = "iteration_limit"
    STALLED = "stalled"


class RunEnded(Model):
    kind: Literal["run_ended"] = "run_ended"
    reason: TerminationReason
    summary: Text
    final_game_revision_id: Identifier | None = None
    unresolved_issue_ids: tuple[Identifier, ...] = ()


EventPayload = Annotated[
    RoleCallStarted | RoleCallFinished | SchedulerDecision | StateCommitted | RunEnded,
    Field(discriminator="kind"),
]


class LedgerEvent(VersionedModel):
    event_id: Identifier
    sequence: PositiveCount
    recorded_at: AwareDatetime = Field(default_factory=utc_now)
    payload: EventPayload


class RunLedger(VersionedModel):
    run_id: Identifier
    task_id: Identifier
    controller: ControllerKind
    budget: ResourceLimits
    config_ref: ArtifactRef | None = None
    started_at: AwareDatetime = Field(default_factory=utc_now)
    events: tuple[LedgerEvent, ...] = ()

    @model_validator(mode="after")
    def validate_history(self) -> RunLedger:
        if self.config_ref is not None and self.config_ref.kind != ArtifactKind.CONFIG:
            raise ValueError("run configuration must reference a config artifact")
        require_unique((event.event_id for event in self.events), "event IDs")
        requests: dict[str, RoleRequest] = {}
        finished: set[str] = set()
        decisions: set[str] = set()
        committed_requests: set[str] = set()
        revision = 0
        previous_time = self.started_at
        ended = False
        for sequence, event in enumerate(self.events, start=1):
            if ended:
                raise ValueError("no events may follow run_ended")
            if event.sequence != sequence:
                raise ValueError("event sequence must be contiguous and start at 1")
            if event.recorded_at < previous_time:
                raise ValueError("event timestamps must be chronological")
            previous_time = event.recorded_at
            payload = event.payload
            if isinstance(payload, RoleCallStarted):
                request = payload.request
                if request.run_id != self.run_id or request.task_id != self.task_id:
                    raise ValueError("request belongs to a different run or task")
                if request.request_id in requests:
                    raise ValueError("duplicate request ID")
                if requests.keys() - finished:
                    raise ValueError("serial execution permits only one active role call")
                if request.state_revision != revision:
                    raise ValueError("request refers to a stale state revision")
                requests[request.request_id] = request
            elif isinstance(payload, RoleCallFinished):
                request_id = payload.result.request_id
                if request_id not in requests or request_id in finished:
                    raise ValueError("result must finish an active request exactly once")
                payload.result.validate_for(requests[request_id])
                finished.add(request_id)
            elif isinstance(payload, SchedulerDecision):
                if self.controller != ControllerKind.ADAPTIVE_SCHEDULER:
                    raise ValueError("fixed loop cannot contain adaptive scheduler decisions")
                if payload.decision_id in decisions:
                    raise ValueError("duplicate scheduler decision ID")
                if payload.state_revision != revision or requests.keys() - finished:
                    raise ValueError("scheduler decision requires current state and no active role")
                decisions.add(payload.decision_id)
            elif isinstance(payload, StateCommitted):
                if payload.previous_revision != revision or requests.keys() - finished:
                    raise ValueError("state commit requires current state and no active role")
                if payload.request_id is not None:
                    if payload.request_id not in finished or payload.request_id in committed_requests:
                        raise ValueError("state commit requires an uncommitted finished request")
                    if requests[payload.request_id].state_revision != revision:
                        raise ValueError("cannot commit a result based on stale state")
                    committed_requests.add(payload.request_id)
                revision = payload.state_revision
            elif isinstance(payload, RunEnded):
                if requests.keys() - finished:
                    raise ValueError("finish active calls with partial/failed results before ending")
                ended = True
        return self

    @property
    def usage(self) -> ResourceUsage:
        return ResourceUsage.aggregate(
            event.payload.usage for event in self.events
            if isinstance(event.payload, (RoleCallFinished, SchedulerDecision))
        )

    @property
    def state_revision(self) -> int:
        return max(
            (event.payload.state_revision for event in self.events
             if isinstance(event.payload, StateCommitted)), default=0,
        )

    @property
    def termination(self) -> RunEnded | None:
        if self.events and isinstance(self.events[-1].payload, RunEnded):
            return self.events[-1].payload
        return None

    def append(self, event: LedgerEvent) -> RunLedger:
        """Return a new validated ledger; storage and concurrency are out of scope."""
        return self.revised(events=(*self.events, event))
