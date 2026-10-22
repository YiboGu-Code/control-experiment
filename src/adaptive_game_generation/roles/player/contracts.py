"""Public trace protocol and bounded Player decisions; no model-authored evidence."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from ...models import ArtifactRef, Blocker
from ...models.base import Identifier, Model, PositiveCount, Seconds, Text, require_unique

Frame = Annotated[int, Field(strict=True, ge=0, lt=600)]
Scenario = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Key = Annotated[str, StringConstraints(pattern=r"^(?:[A-Z0-9]|ESCAPE|ENTER|SPACE|TAB|BACKSPACE|DELETE|SHIFT|CTRL|ALT|UP|DOWN|LEFT|RIGHT)$")]


class TraceEvent(Model):
    frame: Frame
    type: Literal["mouse_click", "mouse_down", "mouse_up", "mouse_move", "key_press", "key_down", "key_up", "wait"]
    button: Literal["left", "right"] | None = None
    x: Annotated[int, Field(strict=True, ge=0, lt=1280)] | None = None
    y: Annotated[int, Field(strict=True, ge=0, lt=720)] | None = None
    keycode: Key | None = None

    @model_validator(mode="after")
    def fields_for_event(self):
        mouse = self.type.startswith("mouse_")
        key = self.type.startswith("key_")
        if mouse != (self.x is not None and self.y is not None):
            raise ValueError("Mouse events require x and y; other events cannot carry coordinates")
        if not mouse and (self.x is not None or self.y is not None):
            raise ValueError("Unexpected mouse coordinate")
        if (mouse and self.type != "mouse_move") != (self.button is not None):
            raise ValueError("Only mouse button events require button")
        if key != (self.keycode is not None):
            raise ValueError("Only keyboard events require keycode")
        return self


class InputTrace(Model):
    scenario: Scenario | None = None
    duration_frames: Annotated[int, Field(strict=True, ge=1, le=600)]
    events: Annotated[tuple[TraceEvent, ...], Field(max_length=300)] = ()

    @model_validator(mode="after")
    def ordered(self):
        if any(e.frame >= self.duration_frames for e in self.events):
            raise ValueError("Events must occur before duration_frames")
        if [e.frame for e in self.events] != sorted(e.frame for e in self.events):
            raise ValueError("Events must be ordered by frame")
        return self

    def wire(self):
        return self.model_dump(mode="json", exclude_none=True)


def normalize_trace(trace: InputTrace) -> InputTrace:
    """Expand taps into one-frame down/up pairs and close held inputs in the trace.

    Captured traces contain these explicit events, avoiding ambiguous tap timing
    when replaying with another harness. No release occurs outside the recording.
    """
    events = []
    for event in trace.events:
        if event.type in ("key_press", "mouse_click"):
            if event.frame + 1 >= trace.duration_frames:
                raise ValueError("A tap needs a following release frame")
            down, up = ("key_down", "key_up") if event.type == "key_press" else ("mouse_down", "mouse_up")
            events.extend((event.revised(type=down), event.revised(type=up, frame=event.frame + 1)))
        else:
            events.append(event)
    events.sort(key=lambda e: e.frame)
    held = {}
    pointer = (0, 0)
    for event in events:
        if event.x is not None:
            pointer = (event.x, event.y)
        identity = ("key", event.keycode) if event.keycode else ("mouse", event.button)
        if event.type in ("key_down", "mouse_down"):
            if identity in held:
                raise ValueError("Repeated down without release")
            held[identity] = event
        if event.type in ("key_up", "mouse_up"):
            if identity not in held:
                raise ValueError("Release without a matching down")
            del held[identity]
    for event in held.values():
        if event.frame >= trace.duration_frames - 1:
            raise ValueError("Held input needs a release frame inside the recording")
        changes = {"type": "key_up" if event.keycode else "mouse_up", "frame": trace.duration_frames - 1}
        if not event.keycode:
            changes.update(x=pointer[0], y=pointer[1])
        events.append(event.revised(**changes))
    events.sort(key=lambda e: e.frame)
    return trace.revised(events=tuple(events))


class PlayerConfig(Model):
    prompt_version: Literal["player.v1", "player.v2"] = "player.v1"
    harness_version: Literal["godot-player.v1"] = "godot-player.v1"
    max_output_tokens: PositiveCount = 8192
    max_model_turns: Annotated[int, Field(strict=True, ge=2, le=6)] = 4
    max_traces: Annotated[int, Field(strict=True, ge=1, le=10)] = 3
    replay_runs: Annotated[int, Field(strict=True, ge=1, le=3)] = 2
    probe_frames: Annotated[int, Field(strict=True, ge=2, le=60)] = 6
    session_timeout_seconds: Seconds = Field(default=180.0, gt=0)
    max_prompt_bytes: PositiveCount = 768 * 1024
    max_response_bytes: PositiveCount = 128 * 1024
    max_checkpoints: Annotated[int, Field(strict=True, ge=2, le=8)] = 6
    max_visual_images: Annotated[int, Field(strict=True, ge=0, le=8)] = 0
    driver_executable: Text = "python3"
    driver_path: Text = "/opt/gamecraft/player_driver.py"
    seed: Annotated[int, Field(strict=True, ge=0, le=2147483647)] = 1337


class TrialPlan(Model):
    trace_id: Scenario
    purpose: Text
    requirement_ids: tuple[Identifier, ...] = ()
    trace: InputTrace | None = None
    source_trace_ref: ArtifactRef | None = None
    checkpoint_frames: tuple[Frame, ...] = ()

    @model_validator(mode="after")
    def source(self):
        if (self.trace is None) == (self.source_trace_ref is None):
            raise ValueError("Provide a new trace or an existing registered trace reference")
        if self.source_trace_ref and self.source_trace_ref.kind != "input_trace":
            raise ValueError("Replay source must be an input trace")
        require_unique(self.requirement_ids, "requirement IDs")
        require_unique(self.checkpoint_frames, "checkpoint frames")
        return self


class PlayerFinding(Model):
    category: Literal["observed_behavior", "gameplay_concern", "runtime_error", "trace_issue", "tool_error", "unexplored"]
    description: Text
    session_ids: tuple[Identifier, ...] = ()
    requirement_ids: tuple[Identifier, ...] = ()
    suggested_role: Literal["developer", "art_producer", "player", "qa_reviewer"] | None = None
    uncertainty: Text


class PlayerDecision(Model):
    action: Literal["play", "finish", "blocked"]
    summary: Text
    trials: tuple[TrialPlan, ...] = ()
    findings: tuple[PlayerFinding, ...] = ()
    limitations: tuple[Text, ...] = ()
    blockers: tuple[Blocker, ...] = ()

    @model_validator(mode="after")
    def consistent(self):
        if (self.action == "play") != bool(self.trials):
            raise ValueError("Only play decisions require trial plans")
        if (self.action == "blocked") != bool(self.blockers):
            raise ValueError("Only blocked decisions require blockers")
        require_unique((p.trace_id for p in self.trials), "trace IDs")
        return self


class SessionRecord(Model):
    session_id: Identifier
    mode: Literal["probe", "capture", "replay"]
    trace_id: Scenario | None = None
    scenario: Scenario | None = None
    status: Literal["passed", "failed", "not_run"]
    reason: Text
    completed_frames: int = 0
    log_ref: ArtifactRef
    trace_ref: ArtifactRef | None = None
    executed_trace: InputTrace | None = None
    screenshot_refs: tuple[ArtifactRef, ...] = ()
    observation_refs: tuple[ArtifactRef, ...] = ()
    state_digests: tuple[str, ...] = ()
    pixel_digests: tuple[str, ...] = ()
    # Selected actual scene observations, not an image-model interpretation.
    observations: tuple[dict, ...] = ()


class ReplayComparison(Model):
    trace_id: Scenario
    capture_session_id: Identifier
    replay_session_id: Identifier
    status: Literal["passed", "failed", "not_run"]
    state_match: bool | None = None
    pixel_match: bool | None = None
    first_state_difference: int | None = None
    first_pixel_difference: int | None = None
    reason: Text


class PlayerReport(Model):
    schema_version: Literal["player.1"] = "player.1"
    config: PlayerConfig
    task_id: Identifier
    game_revision_id: Identifier
    project_ref: ArtifactRef
    developer_report_ref: ArtifactRef
    sessions: tuple[SessionRecord, ...]
    comparisons: tuple[ReplayComparison, ...]
    decisions: tuple[PlayerDecision, ...]
    protocol_errors: tuple[dict, ...] = ()
    limitations: tuple[Text, ...]
