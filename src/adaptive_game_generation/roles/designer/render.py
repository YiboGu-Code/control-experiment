"""Deterministic human-readable view of the authoritative JSON specification."""

from .contracts import DesignDocument, SECTION_TITLES


def render_design(document: DesignDocument) -> str:
    lines = [
        f"# Game design specification: {document.task_id}", "",
        f"Design version: {document.version}; prompt version: {document.designer_config.prompt_version}.", "",
        "This is a design and acceptance plan, not a claim of implementation or verification.", "",
        "## Changes in this revision", "", document.change_summary, "",
        "Feedback issues: " + (", ".join(document.feedback_issue_ids) or "None"), "",
        "Changed requirements: " + (", ".join(document.changed_requirement_ids) or "None"), "",
        "Changed sections: " + (", ".join(document.changed_section_ids) or "None"), "",
        "## Explicit requirements and source grounding", "",
    ]
    for req in document.requirements:
        lines.extend([
            f"### {req.requirement_id} — {req.description}", "",
            "Source excerpt:", "", *("> " + line for line in req.source_excerpt.splitlines()), "",
            "Acceptance criteria:", "", *("- " + criterion for criterion in req.acceptance_criteria), "",
            "Dependencies: " + (", ".join(req.dependency_ids) or "None"), "",
        ])
    for section in document.sections:
        lines.extend([
            "## " + SECTION_TITLES[section.section_id], "",
            "Related requirements: " + ", ".join(section.requirement_ids), "", section.content, "",
        ])
    lines.extend(["## Assumptions (not explicit task requirements)", ""])
    lines.extend(["- " + item for item in document.assumptions] or ["None."])
    return "\n".join(lines) + "\n"
