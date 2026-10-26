"""Small, immutable-scope citation repairs; valid review rows never get rewritten."""
import json

from pydantic import Field

from ...models.base import Model
from ...runtime import InvalidSubmission
from .contracts import CriterionReview, IssueReview, QAProposal


class GroundingPatch(Model):
    criteria: tuple[CriterionReview, ...] = Field(default=(), max_length=256)
    issues: tuple[IssueReview, ...] = Field(default=(), max_length=128)


def scope(notes):
    return ({(n["requirement_id"], n["criterion_index"]) for n in notes if n["kind"] == "criterion_abstention"},
            {n["issue_id"] for n in notes if n["kind"] == "issue_update_withheld"})


def repair_prompt(inputs, proposal, notes, *, max_source_bytes=96000):
    keys, ids = scope(notes)
    criteria = [c for c in proposal.criteria if (c.requirement_id, c.criterion_index) in keys]
    issues = [i for i in proposal.issues if i.issue.issue_id in ids]
    rids = {r for r, _ in keys} | {r for i in issues for r in i.issue.requirement_ids}
    requested = {s for row in [*criteria, *issues] for s in row.source_ids}
    evidence = [e for e in inputs["evidence"] if rids.intersection(e["requirement_ids"])]
    eids = {e["evidence_id"] for e in evidence}
    selected, omitted, size = [], [], 0
    # Previously cited sources first, then alternative sources for the same evidence.
    sources = inputs["sources"]
    sources = list(sources.values()) if isinstance(sources, dict) else sources
    for source in sorted(sources, key=lambda s: (s["source_id"] not in requested, s["source_id"])):
        if source["source_id"] not in requested and not eids.intersection(source.get("evidence_ids", [])):
            continue
        length = len(json.dumps(source, ensure_ascii=False).encode())
        if size + length > max_source_bytes:
            omitted.append(source["source_id"])
        else:
            selected.append(source)
            size += length
    payload = {"protocol": "qa-grounding-patch.v1", "instructions":
        "Return English JSON matching output_schema. Repair ONLY the listed criteria/issues. "
        "Valid review rows are locked. This is a citation repair, not a new full review. "
        "Use only supplied current evidence and inspected sources. No image pixels are attached to this repair: "
        "do not introduce visual pass/fail. Pass/fail requires both mapped evidence_ids and matching source_ids. "
        "Static uses build_check evidence; runtime uses playthrough/replay evidence covering this requirement; "
        "replay uses measured replay_comparison sources. If necessary evidence/source content is omitted, "
        "use insufficient, never invent a citation or assume the missing content. "
        "An omitted patch entry retains its original proposal and is conservatively checked by the host. "
        "Do not add unrelated issues or change the ready recommendation.",
        "errors": notes, "criteria": [c.model_dump(mode="json") for c in criteria],
        "issues": [i.model_dump(mode="json") for i in issues],
        "instruction": inputs["task"]["instruction"],
        "requirements": [r for r in inputs["requirements"] if r["requirement_id"] in rids],
        "evidence": evidence, "sources": selected, "omitted_source_ids": omitted,
        "output_schema": GroundingPatch.model_json_schema()}
    return json.dumps(payload, ensure_ascii=False)


def merge_patch(original, data, notes, *, allowed_sources=None):
    if not isinstance(data, dict):
        raise InvalidSubmission("A grounding patch must be a JSON object")
    keys, ids = scope(notes)
    if "summary" in data:
        # Compatibility with a model that returns the old full-review format.
        full = QAProposal.model_validate(data)
        if full.revised(criteria=original.criteria, issues=original.issues) != original:
            raise InvalidSubmission("A grounding repair cannot rewrite review-level decisions")
        if ([c for c in full.criteria if (c.requirement_id, c.criterion_index) not in keys] !=
                [c for c in original.criteria if (c.requirement_id, c.criterion_index) not in keys] or
                [i for i in full.issues if i.issue.issue_id not in ids] !=
                [i for i in original.issues if i.issue.issue_id not in ids]):
            raise InvalidSubmission("A grounding repair cannot rewrite valid review entries")
        patch = GroundingPatch(criteria=tuple(c for c in full.criteria if (c.requirement_id, c.criterion_index) in keys),
                               issues=tuple(i for i in full.issues if i.issue.issue_id in ids))
    else:
        patch = GroundingPatch.model_validate(data)
    ckeys = [(c.requirement_id, c.criterion_index) for c in patch.criteria]
    ikeys = [i.issue.issue_id for i in patch.issues]
    if (len(ckeys) != len(set(ckeys)) or len(ikeys) != len(set(ikeys)) or
            set(ckeys) - keys or set(ikeys) - ids):
        raise InvalidSubmission("Grounding patch contains duplicate or out-of-scope entries")
    if any(c.basis == "visual" and c.verdict in ("pass", "fail") for c in patch.criteria):
        raise InvalidSubmission("Citation-only repair cannot establish new visual acceptance")
    if allowed_sources is not None and any(set(row.source_ids) - set(allowed_sources) for row in (*patch.criteria, *patch.issues)):
        raise InvalidSubmission("Grounding patch cites source content not supplied for this repair")
    updated_c = dict(zip(ckeys, patch.criteria))
    updated_i = dict(zip(ikeys, patch.issues))
    return original.revised(criteria=tuple(updated_c.get((c.requirement_id, c.criterion_index), c) for c in original.criteria),
                            issues=tuple(updated_i.get(i.issue.issue_id, i) for i in original.issues))
