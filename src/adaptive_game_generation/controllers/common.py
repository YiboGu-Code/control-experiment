"""Shared work objectives, request construction and controller stopping rules."""

from ..models import Role, RoleCallFinished, RoleCallStarted
from ..models.base import utc_now

FIXED_ORDER = (Role.DESIGNER, Role.ART_PRODUCER, Role.DEVELOPER, Role.PLAYER, Role.QA_REVIEWER)


OBJECTIVES = {
    Role.DESIGNER: "Maintain a grounded, implementable design for the original game instruction; address assigned design issues.",
    Role.ART_PRODUCER: "Produce or update the assets required by the current design; address assigned art issues and preserve provenance.",
    Role.DEVELOPER: "Implement or repair the current game using the current design and art; address assigned issues and assemble captured demo traces.",
    Role.PLAYER: "Play the current game, capture actual inputs, and check fresh deterministic replays; investigate assigned play and evidence gaps.",
    Role.QA_REVIEWER: "Review every original requirement against current-game evidence, update issues, and decide delivery readiness honestly.",
}
CRITERIA = {
    Role.DESIGNER: ("Ground requirements in exact English instruction excerpts.", "Retain unchanged requirements and address selected design issues."),
    Role.ART_PRODUCER: ("Use permitted library interfaces or image generation and preserve licenses.", "Mark mock images honestly and retain usable unchanged assets."),
    Role.DEVELOPER: ("Deliver a runnable Godot project with the specified gameplay and current assets.", "Fix actual engine and QA findings; protect captured demo inputs from model edits."),
    Role.PLAYER: ("Exercise ordinary menu/play and supported scenarios using actual input.", "Replay every packaged demo and record raw frame evidence; report gaps without inventing success."),
    Role.QA_REVIEWER: ("Use current-game, checksum-verified evidence for acceptance.", "Keep mock art, missing traces, failed replay and unsupported claims visible as readiness blockers."),
}



def budget_stop(state):
    usage, limits = state.ledger.usage, state.ledger.budget
    if limits.max_wall_seconds is not None and (utc_now() - state.ledger.started_at).total_seconds() >= limits.max_wall_seconds:
        detail = (" Token usage from an interrupted provider call remains incomplete; reserved tokens are not confirmed usage."
                  if not usage.token_usage_complete else "")
        return "timeout", "The shared run wall-clock deadline was reached." + detail
    if not usage.token_usage_complete:
        return "failed", "Token usage is incomplete; reconcile the recorded provider failure before another run."
    for name in ("model_calls", "input_tokens", "output_tokens", "tool_calls", "tool_seconds"):
        limit = getattr(limits, "max_" + name)
        if limit is not None and getattr(usage, name) >= limit:
            return "budget_exhausted", "Shared resource exhausted: " + name
    return None


def role_limit_reached(state, limit):
    return limit is not None and sum(isinstance(e.payload, RoleCallStarted) for e in state.ledger.events) >= limit


def role_request(executor, role, identity, work_item_ids, *, objective=None, completion_criteria=()):
    state = executor.state
    attachments = []
    for name in ("qa-handoff", "player-handoff"):
        refs = [r for r in state.artifacts if r.artifact_id == name]
        if refs:
            attachments.append(max(refs, key=lambda r: r.version))
    prior = [e.payload.result for e in state.ledger.events if isinstance(e.payload, RoleCallFinished)
             and e.payload.result.role == role]
    goal = OBJECTIVES[role]
    if objective:
        goal += " Selected work objective: " + objective
    if prior and prior[-1].status == "failed" and prior[-1].error:
        goal += " The previous invocation was rejected without accepted changes: " + prior[-1].error.message
    return executor.request(role, goal, tuple(dict.fromkeys((*CRITERIA[role], *completion_criteria))),
        work_item_ids=tuple(work_item_ids), artifact_refs=tuple(attachments), request_id=identity)
