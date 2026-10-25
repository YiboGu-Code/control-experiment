"""Formatting-tolerant matching that always returns a literal instruction span.

No semantic similarity, case folding, word deletion or invented requirement text.
The offset map restores the original Markdown before the shared-state checks.
"""
import re


def normalized(text):
    ignored = set()
    code = set()
    for match in re.finditer(r"(`+)([\s\S]*?)\1", text):
        code.update(range(match.start(2), match.end(2)))
    # Formatting delimiters only; preserve standalone '*' operators and '_' in IDs.
    for match in re.finditer(r"`+", text):
        ignored.update(range(match.start(), match.end()))
    for match in re.finditer(r"(?<!\w)(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1(?!\w)", text):
        if any(i in code for i in range(match.start(), match.end())):
            continue  # Inside code, these may be operators, globs or identifier characters.
        size = len(match[1])
        ignored.update(range(match.start(), match.start() + size))
        ignored.update(range(match.end() - size, match.end()))
    chars, spans = [], []
    for i, char in enumerate(text):
        if i in ignored:
            continue
        if char.isspace():
            if chars and chars[-1] == " ":
                spans[-1] = (spans[-1][0], i + 1)
                continue
            char = " "
        chars.append(char)
        spans.append((i, i + 1))
    return "".join(chars), spans


def instruction_span(excerpt, instruction, *, preferred=None):
    """Return canonical exact grounding, or None for a meaning/content mismatch."""
    wanted, _ = normalized(excerpt)
    wanted = wanted.strip()
    if not wanted:
        return None
    if preferred is not None and preferred in instruction and normalized(preferred)[0].strip() == wanted:
        return preferred
    if excerpt in instruction:
        return excerpt
    text, spans = normalized(instruction)
    start = text.find(wanted)
    if start < 0:
        return None
    return instruction[spans[start][0]:spans[start + len(wanted) - 1][1]]


def canonicalize(proposal, instruction, existing):
    requirements, corrections, errors = [], [], []
    for draft in proposal.requirements:
        prior = existing.get(draft.requirement_id)
        excerpt = instruction_span(draft.source_excerpt, instruction,
                                   preferred=prior.source_excerpt if prior else None)
        if excerpt is None:
            errors.append({"requirement_id": draft.requirement_id, "description": draft.description,
                           "source_excerpt": draft.source_excerpt,
                           "error": "No contiguous instruction match after Markdown/whitespace normalization"})
            requirements.append(draft)
        else:
            requirements.append(draft.revised(source_excerpt=excerpt))
            if excerpt != draft.source_excerpt:
                corrections.append({"requirement_id": draft.requirement_id,
                    "submitted_excerpt": draft.source_excerpt, "canonical_excerpt": excerpt})
    return proposal.revised(requirements=tuple(requirements)), corrections, errors
