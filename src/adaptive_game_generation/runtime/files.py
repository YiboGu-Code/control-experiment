"""Bounded atomic rename retry for transient Windows reader/indexer locks."""

import os
import time


def replace_atomic(source, target):
    """Callers validate paths first. Never retry the operation producing the files."""
    for attempt in range(6):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 5:
                raise
            time.sleep(0.01 * (2 ** attempt))
