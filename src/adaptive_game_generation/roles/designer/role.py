"""One metered model call, validated patch, and atomic executor-managed publication."""

from __future__ import annotations

import hashlib
import json

from pydantic import ValidationError

from ...models import ArtifactRef, DesignSpec, ProposedUpdates, Requirement, RoleResult
from ...runtime import InvalidSubmission, PermissionDenied, RoleContext
from .contracts import (
    DesignDocument, DesignerConfig, DesignerProposal, RequirementDraft, SECTION_TITLES,
)
from .prompt import build_prompt
from .render import render_design


def _json_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _invalid_constant(value):
    raise ValueError("nonfinite JSON number")


def _draft(requirement: Requirement) -> RequirementDraft:
    return RequirementDraft.model_validate(requirement.model_dump(exclude={"status", "verification"}))


class Designer:
    def __init__(self, config: DesignerConfig | None = None):
        self.config = config or DesignerConfig()

    @staticmethod
    def _result(context: RoleContext, status: str, summary: str, **fields) -> RoleResult:
        return RoleResult(
            request_id=context.request.request_id, role="designer",
            state_revision=context.request.state_revision, status=status, summary=summary, **fields,
        )

    async def _inputs(self, context: RoleContext) -> tuple[dict, DesignDocument | None]:
        state, request, tools = context.state, context.request, context.tools
        current, legacy = None, None
        if state.design is not None:
            raw = await tools.read_text(state.design.document_ref.path)
            if len(raw.encode("utf-8")) > self.config.max_prompt_bytes:
                raise InvalidSubmission("Existing design exceeds the Designer context limit")
            if state.design.document_ref.media_type == "application/json":
                try:
                    current = DesignDocument.model_validate_json(raw)
                except ValidationError:
                    raise InvalidSubmission("Existing Designer document has an invalid schema") from None
                if (current.task_id, current.version) != (state.task.task_id, state.design.version):
                    raise InvalidSubmission("Existing Designer document does not match shared state")
                if current.designer_config != self.config:
                    raise InvalidSubmission("Designer configuration cannot change within a design history")
            else:
                # Step-1/2 adapters wrote Markdown. Keep their entire design visible
                # for a full structured conversion instead of silently discarding it.
                legacy = raw
        selected = set(request.work_item_ids)
        issues = tuple(issue for issue in state.issues if (
            issue.issue_id in selected or bool(set(issue.requirement_ids) & selected) or
            (not selected and issue.status != "closed" and (
                issue.suggested_role == "designer" or issue.issue_type in ("design", "requirement_gap")
            ))
        ))
        attachments = []
        for ref in request.artifact_refs:
            if state.design and ref == state.design.document_ref:
                continue
            # Other artifact types remain references, never guessed as text files.
            item = {"reference": ref.model_dump(mode="json")}
            if ref.kind in ("document", "report", "log", "config"):
                item["text"] = await tools.read_text(ref.path)
            attachments.append(item)
        return {
            "task": state.task.model_dump(mode="json"),
            "request": request.model_dump(mode="json", exclude={"request_id", "run_id", "task_id", "role"}),
            "requirements": [r.model_dump(mode="json") for r in state.requirements],
            "current_design": current.model_dump(mode="json") if current else None,
            "legacy_design_markdown": legacy,
            "existing_assumptions": list(state.design.assumptions) if state.design else [],
            "issues": [issue.model_dump(mode="json") for issue in issues],
            "public_files": list(await tools.list_files("input")),
            "attachments": attachments,
        }, current

    def _parse(self, raw: str) -> DesignerProposal:
        if len(raw.encode("utf-8")) > self.config.max_response_bytes:
            raise InvalidSubmission("Designer response exceeds the configured size limit")
        try:
            data = json.loads(raw, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
            return DesignerProposal.model_validate(data)
        except (ValueError, RecursionError):
            # ValidationError text can echo arbitrary provider content; keep it out
            # of role feedback and the execution journal.
            raise InvalidSubmission("Designer response must match the JSON proposal schema") from None

    def _merge(self, context, proposal, current, feedback_ids) -> tuple[DesignDocument, tuple[Requirement, ...]]:
        state = context.state
        old = {r.requirement_id: r for r in state.requirements}
        merged = dict(old)
        changed = []
        for draft in proposal.requirements:
            prior = old.get(draft.requirement_id)
            if draft.source_excerpt not in state.task.instruction:
                raise InvalidSubmission("Designer requirement has no exact instruction grounding")
            if draft.requirement_id in {issue.issue_id for issue in state.issues}:
                raise InvalidSubmission("Designer requirement ID conflicts with an issue")
            if prior and draft.source_excerpt != prior.source_excerpt:
                raise InvalidSubmission("Designer cannot replace a requirement's original grounding")
            if prior is None and any(
                (draft.description, draft.source_excerpt) == (r.description, r.source_excerpt)
                for r in merged.values()
            ):
                raise InvalidSubmission("Designer cannot renumber an existing requirement")
            value = prior.revised(**draft.model_dump()) if prior else Requirement(**draft.model_dump())
            merged[draft.requirement_id] = value
            if prior != value:
                changed.append(value)
        sections = {s.section_id: s for s in current.sections} if current else {}
        changed_sections = tuple(s.section_id for s in proposal.sections if sections.get(s.section_id) != s)
        sections.update({s.section_id: s for s in proposal.sections})
        assumptions = proposal.assumptions
        if assumptions is None:
            assumptions = state.design.assumptions if state.design else ()
        try:
            document = DesignDocument(
                task_id=state.task.task_id, version=(state.design.version if state.design else 0) + 1,
                previous_version=state.design.version if state.design else None,
                designer_config=self.config, change_summary=proposal.summary,
                changed_requirement_ids=tuple(r.requirement_id for r in changed),
                changed_section_ids=tuple(key for key in SECTION_TITLES if key in changed_sections),
                feedback_issue_ids=feedback_ids,
                requirements=tuple(_draft(r) for r in merged.values()),
                sections=tuple(sections[key] for key in SECTION_TITLES if key in sections),
                assumptions=assumptions,
            )
        except ValidationError as exc:
            # These are host schema/graph checks. Include their messages, never
            # Pydantic's echoed input values, so a later invocation can correct
            # omitted sections or invalid links without losing the failure record.
            details = "; ".join(e["msg"] for e in exc.errors(include_input=False, include_url=False))
            raise InvalidSubmission("Designer design is incomplete or has invalid requirement links/dependencies: " + details[:1200]) from None
        return document, tuple(changed)

    async def __call__(self, context: RoleContext) -> RoleResult:
        if context.request.role != "designer":
            raise PermissionDenied("Designer can only handle Designer requests")
        inputs, current = await self._inputs(context)
        prompt = build_prompt(inputs, self.config)
        if len(prompt.encode("utf-8")) > self.config.max_prompt_bytes:
            raise InvalidSubmission("Designer context exceeds the configured limit; no content was truncated")
        raw = await context.tools.generate_text(prompt, max_output_tokens=self.config.max_output_tokens)
        proposal = self._parse(raw)
        if proposal.status == "blocked":
            known = {r.requirement_id for r in context.state.requirements} | {i.issue_id for i in context.state.issues}
            if any(set(b.dependency_ids) - known for b in proposal.blockers):
                raise InvalidSubmission("Designer blocker refers to unknown work items")
            return self._result(context, "blocked", proposal.summary, blockers=proposal.blockers)
        if proposal.status == "no_op":
            if context.state.design is None:
                raise InvalidSubmission("Initial Designer call cannot skip creating a design")
            return self._result(context, "no_op", proposal.summary)
        document, changed = self._merge(
            context, proposal, current, tuple(issue["issue_id"] for issue in inputs["issues"]),
        )
        if current and not changed and not document.changed_section_ids and document.assumptions == current.assumptions:
            return self._result(context, "no_op", "Design already matches the proposed specification")
        refs = []
        for artifact_id, name, media, content in (
            ("designer-specification", "design.json", "application/json", document.model_dump_json(indent=2) + "\n"),
            ("designer-readable", "design.md", "text/markdown", render_design(document)),
        ):
            path = await context.tools.write_text(name, content)
            version = max((r.version for r in context.state.artifacts if r.artifact_id == artifact_id), default=0) + 1
            refs.append(ArtifactRef(
                artifact_id=artifact_id, version=version, kind="document", path=path,
                created_by="designer", media_type=media, sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            ))
        return self._result(
            context, "completed", proposal.summary, artifact_refs=tuple(refs),
            proposed_updates=ProposedUpdates(
                requirements=changed, design=DesignSpec(
                    version=document.version, document_ref=refs[0],
                    change_summary=proposal.summary, assumptions=document.assumptions,
                ),
            ),
        )
