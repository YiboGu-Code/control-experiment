"""Read-only review of game evidence; only QA-owned state and reports are published."""

import json

from pydantic import ValidationError

from ...models import ArtifactRef, Blocker, ProposedUpdates, ReadinessReview, RoleResult, Verification
from ...runtime import InvalidSubmission, PermissionDenied
from ..player.role import strict_json
from .contracts import QAConfig, QAProposal, QAReport
from .evidence import EvidenceReader, check, sha
from .prompt import build_prompt


def validate_criterion(criterion, reader):
    kinds = {"static": {"project_file", "inventory", "build_log", "art_manifest"},
             "runtime": {"runtime_snapshot", "trace", "session"}, "replay": {"replay_comparison"}}
    if getattr(reader, "visual_source_ids", ()):
        kinds["visual"] = {"screenshot"}
    rid = criterion.requirement_id
    check(not set(criterion.evidence_ids) - reader.evidence.keys(), "Criterion references unknown or stale evidence")
    check(not set(criterion.source_ids) - reader.sources.keys(), "Criterion references unknown inspected sources")
    check(all(rid in reader.evidence[i].requirement_ids for i in criterion.evidence_ids),
          "Criterion evidence does not cover its requirement: " + rid)
    if criterion.verdict in ("pass", "fail"):
        check(bool(criterion.evidence_ids) and bool(criterion.source_ids), "Acceptance verdicts require evidence and inspected sources")
        check(criterion.basis in kinds, "Text-only QA cannot establish visual or specification acceptance")
        valid = [s for s in (reader.sources[i] for i in criterion.source_ids) if s["kind"] in kinds[criterion.basis]
                 and (s["kind"] != "session" or s["content"]["status"] == "passed")
                 and set(s["evidence_ids"]).intersection(criterion.evidence_ids)]
        check(bool(valid), "Acceptance basis lacks a corresponding inspected source: " + rid)
        if criterion.basis == "visual":
            check(all(i in reader.visual_source_ids for i in criterion.source_ids
                      if reader.sources[i]["kind"] == "screenshot"),
                  "Visual acceptance requires pixels actually supplied for this review")
        allowed = {"build_check"} if criterion.basis == "static" else {"playthrough", "replay"} if criterion.basis in ("runtime", "visual") else {"replay"}
        check(all(reader.evidence[i].kind in allowed for i in criterion.evidence_ids),
              "Evidence kind cannot establish the requested acceptance basis: " + rid)
        if criterion.basis == "replay":
            wanted = "passed" if criterion.verdict == "pass" else "failed"
            check(any(s["content"]["status"] == wanted for s in valid), "Replay verdict contradicts measured comparison")
    if criterion.verdict == "design_conflict":
        check("instruction" in criterion.source_ids, "Design conflict must cite the original instruction")


def validate_issue(review, state, reader, statuses, previous, requirement_ids):
    issue = review.issue
    check(issue.issue_id in previous or issue.issue_id.startswith("QA-"), "New issue IDs must begin with QA-")
    check(not set(issue.requirement_ids) - requirement_ids, "Issue references unknown requirements")
    check(not set(issue.evidence_ids) - reader.evidence.keys(), "Issue references unknown or stale evidence")
    check(not set(review.source_ids) - reader.sources.keys(), "Issue references unknown inspected sources")
    check(not review.instruction_excerpt or review.instruction_excerpt in state.task.instruction,
          "Issue instruction excerpt is not verbatim")
    check(bool(issue.reproduction_steps), "Issues need reproduction steps or a specific requested test")
    if issue.issue_type not in ("evidence_gap", "design"):
        check(bool(issue.evidence_ids) and bool(review.source_ids), "Observed defects need evidence and sources")
    if issue.issue_type == "design":
        check(bool(review.instruction_excerpt) and "instruction" in review.source_ids,
              "Design issues need an exact original instruction excerpt")
    if issue.issue_id in previous:
        old = previous[issue.issue_id]
        check(set(old.requirement_ids).issubset(issue.requirement_ids), "Issue update cannot drop affected requirements")
    if issue.status == "closed" and (issue.issue_id not in previous or previous[issue.issue_id].status != "closed"):
        check(issue.issue_id in previous, "Cannot create an already closed issue")
        old = previous[issue.issue_id]
        new = set(issue.evidence_ids) - set(old.evidence_ids)
        check(bool(new) and all(reader.evidence[i].outcome == "passed" for i in new),
              "Issue closure requires new successful current evidence")
        known = {e.evidence_id: e for e in state.evidence}
        times = [known[i].recorded_at for i in old.evidence_ids if i in known]
        if reader.previous_review_time is not None:
            times.append(reader.previous_review_time)
        check(not times or all(reader.evidence[i].recorded_at > max(times) for i in new),
              "Issue closure evidence must be recorded after the previous review or finding")
        check(bool(issue.requirement_ids) and all(statuses[r] == "verified_pass" for r in issue.requirement_ids),
              "Issue closure requires verified affected requirements")
        check(all(any(r in reader.evidence[i].requirement_ids for i in new) for r in issue.requirement_ids),
              "New closure evidence must cover all affected requirements")
        check(all(any(i in reader.sources[s]["evidence_ids"] for s in review.source_ids) for i in new),
              "Closure evidence needs inspected source citations")
        if issue.issue_type in ("runtime", "gameplay", "trace"):
            check(all(reader.evidence[i].kind in ("playthrough", "replay") for i in new), "Runtime issue cannot close on a build")


def derive_requirements(criteria, state, reader):
    by_requirement = {r.requirement_id: [] for r in state.requirements}
    for criterion in criteria:
        by_requirement[criterion.requirement_id].append(criterion)
    statuses, requirements = {}, []
    for requirement in state.requirements:
        reviews = by_requirement[requirement.requirement_id]
        verdicts = {c.verdict for c in reviews}
        # A contradictory Designer specification cannot be certified or silently amended.
        verdict = "verified_pass" if verdicts == {"pass"} else "verified_fail" if "fail" in verdicts and "design_conflict" not in verdicts else "implemented_unverified"
        statuses[requirement.requirement_id] = verdict
    for _ in state.requirements:
        for requirement in state.requirements:
            if statuses[requirement.requirement_id] == "verified_pass" and any(statuses[d] != "verified_pass" for d in requirement.dependency_ids):
                statuses[requirement.requirement_id] = "implemented_unverified"
    for requirement in state.requirements:
        status = statuses[requirement.requirement_id]
        reviews = by_requirement[requirement.requirement_id]
        verification = None
        if status in ("verified_pass", "verified_fail"):
            selected = [c for c in reviews if c.verdict == ("pass" if status == "verified_pass" else "fail")]
            ids = tuple(sorted({e for c in selected for e in c.evidence_ids}))
            verification = Verification(game_revision_id=reader.game.revision_id, evidence_ids=ids,
                rationale="; ".join(f"Criterion {c.criterion_index}: {c.rationale}" for c in selected))
        elif requirement.status == "not_implemented":
            status = "not_implemented"
            statuses[requirement.requirement_id] = status
        requirements.append(requirement.revised(status=status, verification=verification))
    return tuple(requirements), statuses


def evaluate(proposal, state, reader):
    """Validate citations and derive statuses/gates; never trust a model's ready flag alone."""
    required = {(r.requirement_id, i) for r in state.requirements for i in range(len(r.acceptance_criteria))}
    check({(c.requirement_id, c.criterion_index) for c in proposal.criteria} == required,
          "Review must cover every acceptance criterion exactly once")
    by_requirement = {r.requirement_id: [] for r in state.requirements}
    errors = []
    for criterion in proposal.criteria:
        try:
            validate_criterion(criterion, reader)
        except InvalidSubmission as exc:
            errors.append(f"{criterion.requirement_id}:{criterion.criterion_index}: {exc}")
        by_requirement[criterion.requirement_id].append(criterion)
    check(not errors, "Invalid criterion citations: " + " | ".join(errors[:50]))
    requirements, statuses = derive_requirements(proposal.criteria, state, reader)
    previous = {i.issue_id: i for i in state.issues}
    issues = dict(previous)
    errors = []
    for review in proposal.issues:
        try:
            validate_issue(review, state, reader, statuses, previous, by_requirement.keys())
        except InvalidSubmission as exc:
            errors.append(f"{review.issue.issue_id}: {exc}")
        issues[review.issue.issue_id] = review.issue
    for criterion in proposal.criteria:
        if criterion.verdict == "pass":
            continue
        matching = [i for i in issues.values() if i.status != "closed" and criterion.requirement_id in i.requirement_ids]
        expected = {"insufficient": "evidence_gap", "design_conflict": "design"}.get(criterion.verdict)
        if not any(expected is None or i.issue_type == expected for i in matching):
            errors.append("Non-pass criterion needs an actionable open issue: " + criterion.requirement_id + ":" + str(criterion.criterion_index))
    check(not errors, "Invalid issues: " + " | ".join(errors[:50]))
    blockers = tuple(sorted(i.issue_id for i in issues.values() if i.status != "closed" and i.severity in ("major", "blocker")))
    gates = {**reader.gates, "all_requirements_verified": bool(requirements) and all(r.status == "verified_pass" for r in requirements),
             "no_blocking_issues": not blockers,
             "no_open_design_or_evidence_gaps": not any(i.status != "closed" and i.issue_type in ("design", "evidence_gap") for i in issues.values())}
    ready = proposal.ready and all(gates.values())
    failed = [name for name, passed in gates.items() if not passed]
    rationale = proposal.readiness_rationale + (" Host gate failures: " + ", ".join(failed) + "." if failed else "")
    ids = tuple(sorted({i for c in proposal.criteria for i in c.evidence_ids} | set(reader.game.build_evidence_ids)))
    readiness = ReadinessReview(game_revision_id=reader.game.revision_id, ready=ready, rationale=rationale,
        evidence_ids=ids, blocking_issue_ids=blockers)
    return tuple(requirements), tuple(issues.values()), readiness, gates, statuses


class QAReviewer:
    def __init__(self, config=None):
        self.config = config or QAConfig()

    @staticmethod
    def result(context, status, summary, **fields):
        return RoleResult(role="qa_reviewer", request_id=context.request.request_id, state_revision=context.request.state_revision,
                          status=status, summary=summary, **fields)

    async def __call__(self, context):
        if context.request.role != "qa_reviewer":
            raise PermissionDenied("QA Reviewer requires a qa_reviewer request")
        game = next((g for g in context.state.game_revisions if g.revision_id == context.state.current_game_revision_id), None)
        if game is None or not context.state.requirements:
            return self.result(context, "blocked", "QA requires a Developer game and Designer requirements",
                blockers=(Blocker(description="Provide a current registered game and requirement specification"),))
        reader = EvidenceReader(context, self.config, game)
        previous = [r for r in context.state.artifacts if r.artifact_id == "qa-report" and r.created_by == "qa_reviewer"]
        if previous:
            report = QAReport.model_validate_json(await reader.read(max(previous, key=lambda r: r.version)))
            check(report.config == self.config, "QA configuration is frozen within a run")
            reader.previous_review_time = report.reviewed_at
        inputs = await reader.collect()
        rejections, proposal, previous_response = [], None, None
        grounding_repairs = 0
        for turn in range(self.config.max_model_turns):
            prompt = build_prompt(inputs, self.config, rejections, previous_response)
            check(len(prompt.encode()) <= self.config.max_prompt_bytes, "QA prompt exceeds its byte limit")
            raw = await context.tools.generate_text(prompt, max_output_tokens=self.config.max_output_tokens,
                                                   image_refs=reader.visual_refs)
            check(len(raw.encode()) <= self.config.max_response_bytes, "QA response exceeds its byte limit")
            try:
                candidate = QAProposal.model_validate(strict_json(raw))
                from .grounding import ground_proposal
                effective, grounding_notes = ground_proposal(candidate, context.state, reader)
                reviewed, issues, readiness, gates, statuses = evaluate(effective, context.state, reader)
                if (grounding_notes and grounding_repairs < self.config.max_grounding_repairs
                        and turn + 1 < self.config.max_model_turns):
                    # Let the model repair citations before publishing conservative
                    # abstentions. This shares the same bounded, metered turn budget.
                    grounding_repairs += 1
                    previous_response = raw
                    rejections.append({"model_turn": turn + 1, "kind": "grounding",
                        "errors": list(grounding_notes)})
                    continue
                proposal = candidate
                break
            except (ValueError, InvalidSubmission) as exc:
                previous_response = raw
                errors = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors(include_input=False, include_url=False)] if isinstance(exc, ValidationError) else [str(exc)]
                rejections.append({"model_turn": turn + 1, "errors": errors[:30]})
        check(proposal is not None, "QA model exhausted its bounded protocol repair attempts")
        check(await context.tools.fingerprint(game.project_ref.path) == game.project_ref.sha256, "Game changed during QA review")
        report = QAReport(config=self.config, task_id=context.state.task.task_id, game_revision_id=game.revision_id,
            proposal=proposal, effective_proposal=effective, grounding_notes=grounding_notes,
            requirement_statuses=statuses, readiness=readiness, gates=gates,
            evidence_integrity=reader.integrity, proposal_rejections=tuple(rejections))
        refs = []
        async def write(name, identity, value):
            raw = (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
            path = await context.tools.write_bytes(name, raw)
            refs.append(ArtifactRef(artifact_id=identity, version=max((r.version for r in context.state.artifacts if r.artifact_id == identity), default=0) + 1,
                kind="report", path=path, created_by="qa_reviewer", media_type="text/markdown" if name.endswith(".md") else "application/json", sha256=sha(raw)))
        await write("qa_report.json", "qa-report", report.model_dump(mode="json"))
        await write("coverage.json", "qa-coverage", [c.model_dump(mode="json") for c in effective.criteria])
        await write("issues.json", "qa-issues", [i.model_dump(mode="json") for i in issues])
        await write("readiness.json", "qa-readiness", readiness.model_dump(mode="json"))
        await write("evidence_catalog.json", "qa-evidence-catalog", inputs)
        lines = ["# Internal QA handoff", "", effective.summary, "", f"Delivery ready: **{readiness.ready}**.", "", readiness.rationale,
            "", "## Instruction audit", "", proposal.instruction_audit, "", "## Requirements", "", "| Requirement | Status |", "| --- | --- |"]
        lines += [f"| {r.requirement_id} | {r.status.value} |" for r in reviewed]
        lines += ["", "## Issues", ""]
        for issue in issues:
            lines += [f"### {issue.issue_id} ({issue.severity.value}, {issue.status.value})", "", issue.description,
                "", f"Suggested role: {issue.suggested_role.value}. Requirements: {', '.join(issue.requirement_ids)}.", "",
                *[f"- {step}" for step in issue.reproduction_steps], ""]
        lines += ["## Limits", "", *[f"- {limit}" for limit in proposal.limitations],
            "- Internal review only; no official Benchmark score. Screenshot integrity is verified. " +
            (f"The model received {len(reader.visual_refs)} selected screenshots; unseen scenes remain unverified."
             if reader.visual_refs else "This model received no image pixels."), ""]
        await write("qa_handoff.md", "qa-handoff", "\n".join(lines))
        changed_issues = tuple(i for i in issues if i not in context.state.issues)
        return self.result(context, "completed", effective.summary, artifact_refs=tuple(refs), evidence_refs=readiness.evidence_ids,
            proposed_updates=ProposedUpdates(requirements=reviewed, issues=changed_issues, readiness=readiness))
