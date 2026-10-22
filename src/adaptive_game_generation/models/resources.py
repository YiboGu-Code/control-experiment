"""Resource units and accounting values, without enforcement or provider pricing."""

from __future__ import annotations

from typing import Iterable

from pydantic import StrictBool, model_validator

from .base import Count, Model, Seconds


class ResourceLimits(Model):
    """None means unspecified; zero means no allowance, never unlimited."""

    max_model_calls: Count | None = None
    max_input_tokens: Count | None = None
    max_output_tokens: Count | None = None
    max_image_calls: Count | None = None
    max_tool_calls: Count | None = None
    max_tool_seconds: Seconds | None = None
    max_wall_seconds: Seconds | None = None


class ResourceUsage(Model):
    # Input tokens INCLUDE cached tokens; cached_input_tokens is a subset.
    model_calls: Count = 0
    input_tokens: Count = 0
    cached_input_tokens: Count = 0
    output_tokens: Count = 0
    image_calls: Count = 0
    tool_calls: Count = 0
    tool_seconds: Seconds = 0.0
    wall_seconds: Seconds = 0.0
    # False means incomplete accounting: a known subtotal or a conservative
    # reservation after failure, never a measured/billable total.
    token_usage_complete: StrictBool = True

    @model_validator(mode="after")
    def cache_is_subset(self) -> ResourceUsage:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens")
        return self

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def aggregate(cls, usages: Iterable[ResourceUsage]) -> ResourceUsage:
        items = tuple(usages)
        counts = {
            name: sum(getattr(item, name) for item in items)
            for name in cls.model_fields
            if name != "token_usage_complete"
        }
        return cls(**counts, token_usage_complete=all(item.token_usage_complete for item in items))
