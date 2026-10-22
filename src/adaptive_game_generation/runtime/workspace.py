"""An allowlisted public workspace, private control data, and staged role output."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from ..models import ArtifactRef, Role, TaskSpec
from .errors import InvalidSubmission, PermissionDenied

_PRIVATE_NAMES = {".git", ".aws", ".codex", ".private", "tests", "solution", "solutions", "rubric.json"}
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def logical_path(value: str) -> str:
    try:
        ArtifactRef.portable_relative_path(value)
    except ValueError as exc:
        raise PermissionDenied("Only normalized relative paths are allowed") from exc
    if not value.strip() or any(part.split(".")[0].lower() in _WINDOWS_DEVICES for part in value.split("/")):
        raise PermissionDenied("Invalid portable path")
    return value


def no_links(path: Path) -> None:
    """Reject links, junctions/reparse points, hardlinks and special files."""
    for part in (*reversed(path.parents), path):
        if not os.path.lexists(part):
            continue
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PermissionDenied("Links and reparse points are not allowed")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise PermissionDenied("Hardlinked files are not allowed")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise PermissionDenied("Special files are not allowed")


def safe_path(root: Path, relative: str) -> Path:
    relative = logical_path(relative)
    path = root.joinpath(*relative.split("/"))
    no_links(path)
    if not path.resolve().is_relative_to(root.resolve()):
        raise PermissionDenied("Path escapes the workspace")
    return path


def inspect_tree(root: Path, *, public_input: bool = False, max_bytes: int | None = None) -> int:
    no_links(root)
    paths = [root]
    size = 0
    while paths:
        path = paths.pop()
        no_links(path)
        if public_input:
            parts = (root.name, *path.relative_to(root).parts)
            if any(p.lower() in _PRIVATE_NAMES or p.lower().startswith(".env") for p in parts):
                raise PermissionDenied("Private evaluation or credential material in public input")
        if path.is_dir():
            paths.extend(path.iterdir())
        elif path.is_file():
            size += path.stat().st_size
            if max_bytes is not None and size > max_bytes:
                raise InvalidSubmission("Output exceeds the configured byte limit")
    return size


def copy_public(source: Path, destination: Path) -> None:
    inspect_tree(source, public_input=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, copy_function=shutil.copyfile)
    else:
        shutil.copyfile(source, destination)


@dataclass(frozen=True)
class PublicResource:
    """Trusted host configuration, never populated by a role/model response."""

    name: str
    source: Path


@dataclass(frozen=True)
class InvocationWorkspace:
    public: Path
    staging: Path
    prefix: str

    def output_path(self, relative: str) -> Path:
        return safe_path(self.staging, relative)

    def read_path(self, relative: str) -> Path:
        relative = logical_path(relative)
        if relative.startswith(self.prefix + "/"):
            return safe_path(self.staging, relative[len(self.prefix) + 1:])
        return safe_path(self.public, relative)

    def logical_output(self, relative: str) -> str:
        return f"{self.prefix}/{logical_path(relative)}"


class Workspace:
    def __init__(self, root: Path):
        root = Path(root).absolute()
        no_links(root)
        self.root = root.resolve()
        self.public = self.root / "public"
        self.private = self.root / ".private"
        self.staging = self.root / ".staging"
        for directory in (self.public, self.private, self.staging):
            no_links(directory)
            if not directory.is_dir():
                raise ValueError("Workspace has not been prepared")

    @classmethod
    def prepare(
        cls, root: Path, task_directory: Path, *, task_id: str,
        resources: tuple[PublicResource, ...] = (), benchmark_snapshot: str | None = None,
    ) -> tuple[Workspace, TaskSpec]:
        root, task_directory = Path(root).absolute(), Path(task_directory).absolute()
        no_links(root)
        no_links(task_directory)
        if root.exists():
            raise ValueError("Destination must be a new workspace directory")
        if root.resolve().is_relative_to(task_directory.resolve()) or task_directory.resolve().is_relative_to(root.resolve()):
            raise PermissionDenied("Generation workspace must be separate from the task source")
        instruction = safe_path(task_directory, "instruction.md")
        instruction_bytes = instruction.read_bytes()
        text = instruction_bytes.decode("utf-8")
        # Preflight explicit public sources; the task root itself is never copied.
        starter = safe_path(task_directory, "workspace")
        if starter.exists():
            inspect_tree(starter, public_input=True)
        names: set[str] = set()
        for resource in resources:
            name = logical_path(resource.name)
            if "/" in name or name.casefold() in names:
                raise PermissionDenied("Resource names must be unique single path components")
            names.add(name.casefold())
            source = Path(resource.source).absolute()
            if any(p.lower() in _PRIVATE_NAMES or p.lower().startswith(".env") for p in source.parts):
                raise PermissionDenied("Private source cannot be imported as a resource")
            if source == task_directory or source in task_directory.parents:
                raise PermissionDenied("Cannot expose the benchmark source tree")
            inspect_tree(source, public_input=True)
        task = TaskSpec(task_id=task_id, instruction=text, benchmark_snapshot=benchmark_snapshot)
        for name in ("public/input/resources", ".private", ".staging"):
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / "public/input/instruction.md").write_bytes(instruction_bytes)
        if starter.exists():
            copy_public(starter, root / "public/input/starter")
        for resource in resources:
            copy_public(Path(resource.source).absolute(), root / "public/input/resources" / resource.name)
        return cls(root), task

    def begin(self, role: Role | str, invocation_id: str) -> InvocationWorkspace:
        role_name = Role(role).value if role != "scheduler" else "scheduler"
        key = hashlib.sha256(invocation_id.encode("utf-8")).hexdigest()[:24]
        stage = safe_path(self.staging, key)
        stage.mkdir()  # Never reuse output from a previous invocation.
        stage.chmod(0o777)  # Only this invocation's output is container-writable.
        prefix = f"artifacts/{role_name}/{key}"
        return InvocationWorkspace(self.public, stage, prefix)

    def publish(self, invocation: InvocationWorkspace) -> None:
        from .files import replace_atomic
        inspect_tree(invocation.staging)
        target = safe_path(self.public, invocation.prefix)
        if target.exists():
            raise InvalidSubmission("Artifact namespace already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        replace_atomic(invocation.staging, target)

    def read_public(self, relative: str) -> Path:
        return safe_path(self.public, relative)
