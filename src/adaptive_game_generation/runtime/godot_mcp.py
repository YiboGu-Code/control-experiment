"""Bounded Godot MCP inspection service; preview output is not replay evidence."""
from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re
from typing import Annotated, Protocol

from pydantic import Field, field_validator

from ..models.base import Model
from .errors import InvalidSubmission
from .sandbox import DockerSandbox
from .workspace import InvocationWorkspace, logical_path

MCP_PACKAGE = "godot-mcp-runtime@3.8.1"
MCP_TOOL_CALLS = 7  # tree, run, bounded wait, screenshot, UI, logs, stop


class GodotInspectionRequest(Model):
    scene_path: str = "Main.tscn"
    scenario: str | None = None
    frames: Annotated[int, Field(strict=True, ge=1, le=180)] = 60

    @field_validator("scene_path")
    @classmethod
    def scene(cls, value):
        logical_path(value)
        if (PurePosixPath(value).suffix != ".tscn" or
                not re.fullmatch(r"[A-Za-z0-9_./ -]+\.tscn", value) or
                any(p.startswith(".") for p in value.split("/"))):
            raise ValueError("Inspection needs a project-relative scene file")
        return value

    @field_validator("scenario")
    @classmethod
    def known_shape(cls, value):
        if value is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
            raise ValueError("Invalid scenario identifier")
        return value


class GodotMCPBackend(Protocol):
    async def inspect(self, workspace, project, destination, job, timeout): ...


@dataclass(frozen=True)
class DockerGodotMCP:
    sandbox: DockerSandbox

    async def inspect(self, workspace, project, destination, job, timeout):
        directory = workspace.output_path(destination)
        directory.mkdir(parents=True)
        (directory / "job.json").write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
        # Expose only this project read-only, and this inspection's output folder.
        narrowed = InvocationWorkspace(workspace.read_path(project), directory, workspace.prefix)
        result = await self.sandbox.run(narrowed, (
            "xvfb-run", "-a", "--server-args=-screen 0 1280x720x24", "python3",
            "/opt/gamecraft/mcp_driver.py", "--job", "/output/job.json"), timeout)
        (directory / "process.json").write_text(json.dumps({"returncode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr}, indent=2) + "\n", encoding="utf-8")
        if result.returncode or not (directory / "session/result.json").is_file():
            raise InvalidSubmission("Godot MCP driver failed; inspect its saved process and transcript logs")
        return json.loads((directory / "session/result.json").read_text(encoding="utf-8"))
