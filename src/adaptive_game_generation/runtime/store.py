"""Single-writer, atomic state files and durable in-flight accounting."""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from ..models import ResourceUsage, SharedState
from .errors import RunBusy
from .files import replace_atomic
from .workspace import no_links


def atomic_text(path: Path, text: str) -> None:
    no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers/indexers can briefly deny replacement of the journal.
        # Retry only that atomic rename, never the paid/tool operation that led here.
        # Persistent ACL errors still propagate; the previous file stays intact.
        replace_atomic(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StateStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        no_links(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.json"

    @contextmanager
    def locked(self):
        path = self.directory / "writer.lock"
        no_links(path)
        with path.open("a+b") as stream:
            if path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RunBusy("Another executor is using this run") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def load(self) -> SharedState:
        no_links(self.path)
        return SharedState.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, state: SharedState) -> None:
        state = SharedState.model_validate(state)
        text = state.model_dump_json(indent=2) + "\n"
        # State is the authoritative commit pointer. Historical files may exist
        # ahead of it after a crash, but are never implicitly loaded as current.
        history = self.directory / "history" / f"{len(state.ledger.events):08d}.json"
        atomic_text(history, text)
        atomic_text(self.path, text)

    def journal(self, invocation_id: str, usage: ResourceUsage) -> None:
        data = self.pending()
        if data is None or data["invocation_id"] != invocation_id:
            raise RuntimeError("Accounting journal has no matching invocation")
        data["usage"] = usage.model_dump(mode="json")
        atomic_text(self.directory / "inflight.json", json.dumps(data))

    def start_journal(self, invocation_id: str, actor: str, state_revision: int, started_at: str) -> None:
        atomic_text(self.directory / "inflight.json", json.dumps({
            "invocation_id": invocation_id, "actor": actor, "state_revision": state_revision,
            "started_at": started_at, "usage": ResourceUsage().model_dump(mode="json"),
        }))

    def pending(self) -> dict | None:
        path = self.directory / "inflight.json"
        no_links(path)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def read_journal(self, invocation_id: str) -> ResourceUsage:
        path = self.directory / "inflight.json"
        no_links(path)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["invocation_id"] == invocation_id:
                return ResourceUsage.model_validate(data["usage"])
        # An interrupted call without its journal cannot be assumed free.
        return ResourceUsage(token_usage_complete=False)

    def audit(self, entry: dict) -> None:
        path = self.directory / "tools.jsonl"
        no_links(path)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
