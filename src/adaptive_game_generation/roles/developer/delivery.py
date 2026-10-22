"""Host-selected actual Player captures, assembled inside a metered Developer call."""

import json

from ...runtime import InvalidSubmission
from .contracts import PackagedTrace


async def captured_demos(developer, context, current, prior):
    # Local import avoids the Player -> Developer handoff import cycle.
    from ..player.contracts import InputTrace, PlayerReport, normalize_trace

    if not current:
        return {}, (), False
    refs = sorted((r for r in context.state.artifacts if r.artifact_id == "player-report" and
                   r.created_by == "player"), key=lambda r: r.version, reverse=True)
    report_ref, report = None, None
    for ref in refs:
        candidate = PlayerReport.model_validate_json(await developer._read_ref(context, ref))
        if candidate.game_revision_id == current.revision_id:
            if candidate.project_ref != current.project_ref or candidate.task_id != context.state.task.task_id:
                raise InvalidSubmission("Player capture source identity mismatch")
            report_ref, report = ref, candidate
            break
    copies, records = {}, []
    if report:
        # Captures are candidates, not accepted replay verdicts. Repairs can make
        # formerly divergent inputs deterministic. Player must rerun the new bytes.
        captures = [s for s in report.sessions if s.mode == "capture" and s.status == "passed" and s.trace_ref]
        previous_hashes = {p.trace_ref.sha256 for p in prior.packaged_traces} if prior else set()
        # Prefer newly explored input. Within that group reserve one slot for
        # ordinary entry and one for a named scenario when both exist, rather
        # than exhausting the package with the first two menu captures.
        # This deterministic diversity rule is shared by both controllers;
        # it is not a claim about trace quality or gameplay success.
        captures.sort(key=lambda s: s.trace_ref.sha256 in previous_hashes)
        diverse = []
        for reused in (False, True):
            group = [s for s in captures if (s.trace_ref.sha256 in previous_hashes) == reused]
            ordinary = next((s for s in group if s.scenario is None), None)
            scenario = next((s for s in group if s.scenario is not None), None)
            first = [s for s in (ordinary, scenario) if s is not None]
            diverse.extend(first + [s for s in group if s not in first])
        captures = diverse
        seen = set()
        for session in captures:
            raw = await developer._read_ref(context, session.trace_ref)
            trace = InputTrace.model_validate_json(raw)
            if trace != session.executed_trace or normalize_trace(trace) != trace:
                raise InvalidSubmission("Exported Player capture is not the actual canonical executed input")
            identity = json.dumps(trace.wire(), sort_keys=True)
            if identity in seen:
                continue
            seen.add(identity)
            target = f"demo_outputs/demo_{len(records) + 1:02}.json"
            linked = sorted({r for e in context.state.evidence if e.game_revision_id == current.revision_id and
                             e.kind == "playthrough" and session.trace_ref in e.artifact_refs for r in e.requirement_ids})
            records.append(PackagedTrace(target_path=target, source_game_revision_id=current.revision_id,
                player_report_ref=report_ref, trace_ref=session.trace_ref, requirement_ids=tuple(linked)))
            copies[target] = raw
            if len(records) >= developer.config.max_packaged_traces:
                break
    # No new successful capture: retain already assembled provenance and bytes.
    if not records and prior:
        for record in prior.packaged_traces:
            copies[record.target_path] = await developer._read_ref(context, record.trace_ref)
            records.append(record)
    existing = {}
    for path in await context.tools.list_files(current.project_ref.path):
        relative = path.removeprefix(current.project_ref.path + "/")
        if relative.startswith("demo_outputs/"):
            existing[relative] = await context.tools.read_bytes(path)
    return copies, tuple(records), copies != existing
