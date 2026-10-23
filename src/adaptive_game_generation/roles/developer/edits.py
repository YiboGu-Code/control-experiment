"""Exact, atomic in-memory edits. No fuzzy matches, filesystem access or scripts."""
import hashlib

from ...runtime import InvalidSubmission


def source_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_edits(files, edits):
    result = dict(files)
    for edit in edits:
        if edit.path not in files:
            raise InvalidSubmission(f"Local edit source does not exist: {edit.path}")
        original = files[edit.path]
        if source_sha256(original) != edit.expected_sha256:
            raise InvalidSubmission(f"Stale source digest: {edit.path}. Request the current source before retrying.")
        changes = []
        for replacement in edit.replacements:
            old, new = replacement.old_text, replacement.new_text
            # Count overlapping matches as well, e.g. 'aa' inside 'aaa'.
            start = original.find(old)
            if start < 0 or original.find(old, start + 1) >= 0:
                raise InvalidSubmission(f"Local edit must match exactly once in {edit.path}; request a larger source excerpt.")
            if old == new:
                raise InvalidSubmission(f"Local edit makes no change in {edit.path}")
            changes.append((start, start + len(old), new))
        changes.sort()
        if any(a[1] > b[0] for a, b in zip(changes, changes[1:])):
            raise InvalidSubmission(f"Overlapping replacements in {edit.path}; all matches use the original source.")
        text = original
        for start, end, new in reversed(changes):
            text = text[:start] + new + text[end:]
        result[edit.path] = text
    return result
