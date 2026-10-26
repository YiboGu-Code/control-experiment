"""Relevance annotations on existing captures, never acceptance or new observations."""


def validate_mappings(mappings, sessions, known_requirements):
    captures = {s.session_id: s for s in sessions if s.mode == "capture" and s.status == "passed" and s.trace_ref}
    for mapping in mappings:
        session = captures.get(mapping.session_id)
        if session is None:
            raise ValueError("Evidence mapping requires an existing successful capture session, not a probe, replay or proposed trial")
        if mapping.frame not in {o["frame"] for o in session.observations}:
            raise ValueError("Evidence mapping must cite an actually captured checkpoint frame")
        if set(mapping.requirement_ids) - known_requirements:
            raise ValueError("Evidence mapping references unknown requirements")


def apply_mappings(mappings, sessions, bindings):
    lookup = {s.session_id: s for s in sessions}
    for mapping in mappings:
        trace_id = lookup[mapping.session_id].trace_id
        bindings[trace_id] = tuple(sorted(set(bindings[trace_id]) | set(mapping.requirement_ids)))
