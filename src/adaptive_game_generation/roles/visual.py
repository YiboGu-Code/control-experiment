"""Deterministic bounded screenshot selection, shared by the role adapters."""


def select_screenshots(sessions, limit):
    # Capture sessions only: replays should not spend vision budget on repeated
    # scenes. Round-robin endpoints and then intermediate frames across trials.
    groups = []
    for session in sessions:
        if session.mode != "capture" or session.status != "passed":
            continue
        refs = sorted(session.screenshot_refs, key=lambda r: r.path)
        if refs:
            groups.append([refs[-1], *refs[:-1]])
    selected, seen = [], set()
    for index in range(max((len(g) for g in groups), default=0)):
        for group in groups:
            if index >= len(group):
                continue
            ref = group[index]
            if ref.sha256 in seen:
                continue
            if len(selected) >= limit:
                return tuple(selected)
            selected.append(ref)
            seen.add(ref.sha256)
    return tuple(selected)
