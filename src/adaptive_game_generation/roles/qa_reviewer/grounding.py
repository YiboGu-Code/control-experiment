"""Conservative host abstention: unsupported model claims never become verified facts."""

from ...models import IssueRecord
from ...runtime import InvalidSubmission
from .contracts import IssueReview
from .evidence import check
from .role import derive_requirements, validate_criterion, validate_issue


def ground_proposal(proposal, state, reader):
    required = {(r.requirement_id, i) for r in state.requirements for i in range(len(r.acceptance_criteria))}
    check({(c.requirement_id, c.criterion_index) for c in proposal.criteria} == required,
          "Review must cover every acceptance criterion exactly once")
    notes, criteria, issues = [], [], []
    for criterion in proposal.criteria:
        try:
            validate_criterion(criterion, reader)
            criteria.append(criterion)
        except InvalidSubmission as exc:
            notes.append({"kind": "criterion_abstention", "requirement_id": criterion.requirement_id,
                "criterion_index": criterion.criterion_index, "model_verdict": criterion.verdict, "reason": str(exc)})
            criteria.append(criterion.revised(verdict="insufficient",
                rationale=f"Host withheld the model's '{criterion.verdict}' decision: {exc}. Obtain valid current evidence before acceptance; the original rationale is retained in the model proposal.",
                evidence_ids=tuple(i for i in criterion.evidence_ids if i in reader.evidence and criterion.requirement_id in reader.evidence[i].requirement_ids),
                source_ids=tuple(i for i in criterion.source_ids if i in reader.sources)))
    _, statuses = derive_requirements(criteria, state, reader)
    previous = {i.issue_id: i for i in state.issues}
    requirement_ids = {r.requirement_id for r in state.requirements}
    for item in proposal.issues:
        try:
            validate_issue(item, state, reader, statuses, previous, requirement_ids)
            issues.append(item)
        except InvalidSubmission as exc:
            notes.append({"kind": "issue_update_withheld", "issue_id": item.issue.issue_id, "reason": str(exc)})
    merged = {**previous, **{i.issue.issue_id: i.issue for i in issues}}
    groups = {"insufficient": [], "design_conflict": [], "fail": []}
    for criterion in criteria:
        if criterion.verdict == "pass":
            continue
        expected = {"insufficient": "evidence_gap", "design_conflict": "design"}.get(criterion.verdict)
        if not any(i.status != "closed" and criterion.requirement_id in i.requirement_ids and
                   (expected is None or i.issue_type == expected) for i in merged.values()):
            groups[criterion.verdict].append(criterion)
    for verdict, rows in groups.items():
        if not rows:
            continue
        issue_id = {"insufficient": "QA-host-evidence-gaps", "design_conflict": "QA-host-design-review", "fail": "QA-host-failed-acceptance"}[verdict]
        existing = merged.get(issue_id)
        ids = tuple(sorted({c.requirement_id for c in rows} | (set(existing.requirement_ids) if existing else set())))
        evidence = tuple(sorted({i for c in rows for i in c.evidence_ids}))
        source_ids = tuple(sorted({i for c in rows for i in c.source_ids} | ({"instruction"} if verdict == "design_conflict" else set())))
        instruction_excerpt = next(r.source_excerpt for r in state.requirements if r.requirement_id == ids[0]) if verdict == "design_conflict" else ""
        issue = IssueRecord(issue_id=issue_id,
            issue_type={"insufficient": "evidence_gap", "design_conflict": "design", "fail": "requirement_gap"}[verdict],
            severity="major", suggested_role={"insufficient": "player", "design_conflict": "designer", "fail": "developer"}[verdict],
            description={"insufficient": "Acceptance remains unverified. Collect or correctly map current evidence for the listed criteria; no missing test is claimed as a gameplay defect.",
                "design_conflict": "Resolve the model's proposed specification conflicts against the unchanged original instruction before acceptance. These are review requests, not edits to the specification.",
                "fail": "Address the failed acceptance criteria using their current cited evidence, then collect fresh verification."}[verdict],
            requirement_ids=ids, evidence_ids=evidence,
            reproduction_steps=tuple(f"Review {c.requirement_id} criterion {c.criterion_index}: {c.rationale}" for c in rows))
        issues = [item for item in issues if item.issue.issue_id != issue_id]
        issues.append(IssueReview(issue=issue, source_ids=source_ids, instruction_excerpt=instruction_excerpt))
    rejected = [n for n in notes if n["kind"] == "issue_update_withheld"]
    if rejected:
        issue_id = "QA-host-rejected-issues"
        issues = [item for item in issues if item.issue.issue_id != issue_id]
        issues.append(IssueReview(issue=IssueRecord(issue_id=issue_id, issue_type="evidence_gap", severity="major",
            description="Some proposed model issue updates lack valid grounding. Existing issues remain unchanged unless a valid update was accepted.",
            suggested_role="qa_reviewer", requirement_ids=previous[issue_id].requirement_ids if issue_id in previous else (),
            reproduction_steps=tuple(f"Reassess model issue {n['issue_id']} using inspected current sources: {n['reason']}" for n in rejected))))
    abstentions = sum(n["kind"] == "criterion_abstention" for n in notes)
    summary = (f"Reviewed {len(criteria)} acceptance criteria. Host withheld {abstentions} unsupported criterion decisions "
               f"and {len(rejected)} invalid issue updates. See the retained model proposal and effective coverage for details.")
    effective = proposal.revised(criteria=tuple(criteria), issues=tuple(issues), ready=proposal.ready and not notes,
        summary=summary, readiness_rationale=proposal.readiness_rationale +
        (f" Host abstained on {abstentions} criterion decisions and withheld {len(rejected)} issue updates." if notes else ""))
    return effective, tuple(notes)
