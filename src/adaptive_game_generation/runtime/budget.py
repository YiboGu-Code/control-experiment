"""Preflight reservations and post-call accounting shared by roles and scheduler."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Callable

from ..models import ResourceLimits, ResourceUsage
from .errors import BudgetExceeded

_DIMENSIONS = ("model_calls", "input_tokens", "output_tokens", "image_calls", "tool_calls", "tool_seconds")


class BudgetMeter:
    def __init__(
        self, total_limits: ResourceLimits, call_limits: ResourceLimits,
        previous: ResourceUsage, run_started_at: datetime,
        persist: Callable[[ResourceUsage], None], *, fallback_timeout: float = 300.0,
    ):
        self.total_limits, self.call_limits = total_limits, call_limits
        self.previous, self.run_started_at = previous, run_started_at
        self.persist = persist
        self.started = time.monotonic()
        self.used = ResourceUsage()
        self.reserved: ResourceUsage | None = None
        self.poisoned = False
        self.fallback_timeout = fallback_timeout

    def snapshot(self) -> ResourceUsage:
        return self.used.revised(wall_seconds=max(0.0, time.monotonic() - self.started))

    def check_ready(self) -> None:
        self.remaining_time()
        if self.poisoned:
            raise BudgetExceeded("Prior operation exceeded its accounting contract")
        self._check(self.used)

    def remaining_time(self) -> float:
        elapsed = time.monotonic() - self.started
        remaining = self.fallback_timeout - elapsed
        if self.call_limits.max_wall_seconds is not None:
            remaining = min(remaining, self.call_limits.max_wall_seconds - elapsed)
        if self.total_limits.max_wall_seconds is not None:
            total_elapsed = (datetime.now(timezone.utc) - self.run_started_at).total_seconds()
            remaining = min(remaining, self.total_limits.max_wall_seconds - total_elapsed)
        if remaining <= 0:
            raise BudgetExceeded("Wall-clock deadline reached")
        return remaining

    def tool_timeout(self, requested: float) -> float:
        if not math.isfinite(requested) or requested <= 0:
            raise BudgetExceeded("Tool timeout must be positive and finite")
        remaining = min(requested, self.remaining_time())
        for limits, usage in ((self.call_limits, self.used), (self.total_limits, ResourceUsage.aggregate((self.previous, self.used)))):
            if limits.max_tool_seconds is not None:
                remaining = min(remaining, limits.max_tool_seconds - usage.tool_seconds)
        if remaining <= 0:
            raise BudgetExceeded("Tool time budget exhausted")
        return remaining

    def _check(self, proposed: ResourceUsage) -> None:
        for limits, usage in (
            (self.call_limits, proposed),
            (self.total_limits, ResourceUsage.aggregate((self.previous, proposed))),
        ):
            for name in _DIMENSIONS:
                limit = getattr(limits, "max_" + name)
                if limit is not None and getattr(usage, name) > limit:
                    raise BudgetExceeded(f"Budget exhausted: {name}")
            if not usage.token_usage_complete and (
                limits.max_input_tokens is not None or limits.max_output_tokens is not None
            ):
                raise BudgetExceeded("Unknown token usage must be reconciled before continuing")

    def reserve(self, upper_bound: ResourceUsage) -> None:
        if self.poisoned:
            raise BudgetExceeded("Prior operation exceeded its accounting contract")
        if self.reserved is not None:
            raise BudgetExceeded("Concurrent tool calls are not supported")
        self.remaining_time()
        combined = ResourceUsage.aggregate((self.used, upper_bound))
        self._check(combined)
        # Persist the upper bound BEFORE dispatch, so a crash cannot erase costs.
        self.persist(combined.revised(
            wall_seconds=self.snapshot().wall_seconds,
            token_usage_complete=combined.token_usage_complete and upper_bound.model_calls == 0,
        ))
        self.reserved = upper_bound

    def settle(self, actual: ResourceUsage) -> None:
        if self.reserved is None:
            raise RuntimeError("No active reservation")
        bound = self.reserved
        self.reserved = None
        self.used = ResourceUsage.aggregate((self.used, actual))
        self.persist(self.snapshot())
        try:
            if any(getattr(actual, name) > getattr(bound, name) for name in ("model_calls", "input_tokens", "output_tokens", "image_calls", "tool_calls")):
                raise BudgetExceeded("Provider exceeded the reserved resource bound")
            self._check(self.used)
        except BudgetExceeded:
            self.poisoned = True
            raise
