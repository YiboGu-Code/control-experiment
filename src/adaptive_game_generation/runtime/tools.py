"""Metered capability interface exposed by trusted role adapters to their models."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from dataclasses import dataclass
from typing import Protocol

from ..models import ArtifactRef, ResourceUsage, Role
from .budget import BudgetMeter
from .asset_libraries import AssetLibraryProvider, LibraryAsset
from .media import ImageServiceInfo
from .errors import BudgetExceeded, PermissionDenied, RuntimeViolation
from .policy import RolePolicy
from .sandbox import DisabledSandbox, ProcessResult, SandboxBackend
from .store import StateStore
from .workspace import InvocationWorkspace, inspect_tree, logical_path
from .godot_mcp import GodotMCPBackend, GodotInspectionRequest, MCP_TOOL_CALLS


@dataclass(frozen=True)
class TextReply:
    text: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0


@dataclass(frozen=True)
class ModelImage:
    """Host-checked public bytes; never a model-supplied remote URL."""
    source_id: str
    data: bytes
    media_type: str


class TextProvider(Protocol):
    """Trusted adapter must bound the COMPLETE outgoing request, including its system prompt."""

    def input_token_upper_bound(self, prompt: str, *, images: tuple[ModelImage, ...] = ()) -> int: ...
    async def generate(self, prompt: str, *, max_output_tokens: int, timeout: float,
                       images: tuple[ModelImage, ...] = ()) -> TextReply: ...


class ImageProvider(Protocol):
    async def generate(self, prompt: str, *, timeout: float) -> bytes: ...


@dataclass(frozen=True)
class Services:
    text: TextProvider | None = None
    image: ImageProvider | None = None
    sandbox: SandboxBackend | None = None
    libraries: AssetLibraryProvider | None = None
    image_info: ImageServiceInfo | None = None
    godot_mcp: GodotMCPBackend | None = None
    vision: TextProvider | None = None


class RoleTools:
    def __init__(
        self, actor: Role | str, invocation_id: str, workspace: InvocationWorkspace,
        meter: BudgetMeter, store: StateStore, policy: RolePolicy, services: Services,
        *, max_file_bytes: int, max_output_bytes: int, tool_timeout: float,
    ):
        self.actor, self.invocation_id = actor, invocation_id
        self._workspace, self._meter, self._store = workspace, meter, store
        self._policy, self._services = policy, services
        self._max_file_bytes, self._max_output_bytes = max_file_bytes, max_output_bytes
        self._tool_timeout = tool_timeout
        self._closed = False
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._mcp_count = 0

    @property
    def output_prefix(self) -> str:
        return self._workspace.prefix

    def output_path(self, relative: str) -> str:
        return self._workspace.logical_output(relative)

    def close(self) -> None:
        self._closed = True

    async def aclose(self) -> None:
        self.close()
        tasks = [task for task in self._tasks if task is not asyncio.current_task() and not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _audit(self, tool: str, outcome: str, code: str | None = None) -> None:
        self._store.audit({
            "invocation_id": self.invocation_id, "actor": self.actor,
            "tool": tool, "outcome": outcome, "code": code,
            "usage": self._meter.snapshot().model_dump(mode="json"),
        })

    async def _operate(self, tool, operation, *, cost=None, timeout=None, uncertain_tokens=False, tool_calls=1):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            return await self._operate_tracked(tool, operation, cost=cost, timeout=timeout,
                                               uncertain_tokens=uncertain_tokens, tool_calls=tool_calls)
        finally:
            self._tasks.discard(task)

    async def _operate_tracked(self, tool, operation, *, cost=None, timeout=None, uncertain_tokens=False, tool_calls=1):
        if self._closed:
            raise PermissionDenied("This invocation's tool capabilities have expired")
        try:
            self._policy.allow_tool(self.actor, tool)
        except RuntimeViolation as exc:
            self._audit(tool, "denied", exc.code)
            raise
        async with self._lock:
            if self._closed:
                raise PermissionDenied("This invocation has ended")
            try:
                timeout = self._meter.tool_timeout(self._tool_timeout if timeout is None else timeout)
                bound = (cost or ResourceUsage()).revised(tool_calls=tool_calls, tool_seconds=timeout)
                self._meter.reserve(bound)
            except BudgetExceeded as exc:
                self._audit(tool, "denied", exc.code)
                raise
            started = time.monotonic()
            outcome, code = "completed", None
            operation_error = None
            actual = bound.revised(token_usage_complete=not uncertain_tokens)
            try:
                value, measured = await asyncio.wait_for(operation(timeout), timeout)
                actual = measured.revised(tool_calls=measured.tool_calls if tool_calls != 1 else 1)
                return value
            except BaseException as exc:
                operation_error = exc
                outcome = "failed"
                code = exc.code if isinstance(exc, RuntimeViolation) else type(exc).__name__
                raise
            finally:
                actual = actual.revised(tool_seconds=max(0.0, time.monotonic() - started), wall_seconds=0.0)
                try:
                    try:
                        self._meter.settle(actual)
                    except BudgetExceeded:
                        if operation_error is None:
                            raise
                finally:
                    self._audit(tool, outcome, code)

    async def read_bytes(self, relative: str) -> bytes:
        async def operation(_):
            path = self._workspace.read_path(relative)
            if path.stat().st_size > self._max_file_bytes:
                raise PermissionDenied("File exceeds the read limit")
            with path.open("rb") as stream:
                data = stream.read(self._max_file_bytes + 1)
            if len(data) > self._max_file_bytes:
                raise PermissionDenied("File exceeds the read limit")
            return data, ResourceUsage()
        return await self._operate("read", operation)

    async def read_text(self, relative: str) -> str:
        return (await self.read_bytes(relative)).decode("utf-8")

    async def list_files(self, relative: str = "input") -> tuple[str, ...]:
        async def operation(_):
            path = self._workspace.read_path(relative)
            inspect_tree(path)
            files = []
            for item in path.rglob("*"):
                if item.is_file():
                    files.append(f"{logical_path(relative)}/{item.relative_to(path).as_posix()}")
                    if len(files) > 10000:
                        raise PermissionDenied("Directory listing exceeds the item limit")
            return tuple(sorted(files)), ResourceUsage()
        return await self._operate("list", operation)

    async def write_bytes(self, relative: str, data: bytes) -> str:
        async def operation(_):
            if len(data) > self._max_file_bytes:
                raise PermissionDenied("File exceeds the write limit")
            path = self._workspace.output_path(relative)
            current = inspect_tree(self._workspace.staging)
            previous_size = path.stat().st_size if path.is_file() else 0
            if current - previous_size + len(data) > self._max_output_bytes:
                raise PermissionDenied("Invocation output limit exceeded")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return self.output_path(relative), ResourceUsage()
        return await self._operate("write", operation)

    async def write_text(self, relative: str, text: str) -> str:
        return await self.write_bytes(relative, text.encode("utf-8"))

    async def copy(self, source: str, destination: str) -> str:
        async def operation(_):
            original = self._workspace.read_path(source)
            target = self._workspace.output_path(destination)
            if target.exists():
                raise PermissionDenied("Copy destination must be new")
            if target.resolve().is_relative_to(original.resolve()):
                raise PermissionDenied("Cannot copy a directory into itself")
            size = inspect_tree(original, max_bytes=self._max_output_bytes)
            if inspect_tree(self._workspace.staging) + size > self._max_output_bytes:
                raise PermissionDenied("Invocation output limit exceeded")
            target.parent.mkdir(parents=True, exist_ok=True)
            if original.is_dir():
                shutil.copytree(original, target, copy_function=shutil.copyfile)
            else:
                shutil.copyfile(original, target)
            return self.output_path(destination), ResourceUsage()
        return await self._operate("copy", operation)

    async def asset_sources(self) -> dict:
        async def operation(_):
            libraries = await self._services.libraries.availability() if self._services.libraries else tuple(
                {"library": name, "available": False} for name in ("kenney", "opengameart"))
            info = self._services.image_info if self._services.image else None
            return {"libraries": libraries, "image_service": info.model_dump(mode="json") if info else None}, ResourceUsage()
        return await self._operate("library", operation)

    async def fingerprint(self, relative: str) -> str:
        """Hash an allowed file/project with the same algorithm used at commit."""
        async def operation(_):
            from .commit import digest_path
            path = self._workspace.read_path(relative)
            inspect_tree(path, max_bytes=self._max_output_bytes)
            if not path.exists():
                raise PermissionDenied("Cannot hash a missing artifact")
            return digest_path(path), ResourceUsage()
        return await self._operate("read", operation)

    async def remove_output(self, relative: str) -> None:
        """Remove only this invocation's staged output, never published history."""
        async def operation(_):
            path = self._workspace.output_path(relative)
            if path.resolve() == self._workspace.staging.resolve() or not path.resolve().is_relative_to(self._workspace.staging.resolve()):
                raise PermissionDenied("Removal must stay inside this invocation's output")
            if not path.exists():
                raise PermissionDenied("Cannot remove a missing output")
            inspect_tree(path, max_bytes=self._max_output_bytes)
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            return None, ResourceUsage()
        return await self._operate("remove", operation)

    async def search_assets(self, library: str, query: str, *, offset: int = 0, limit: int = 12):
        async def operation(_):
            if self._services.libraries is None:
                raise RuntimeViolation("No asset libraries configured")
            return await self._services.libraries.search(library, query, offset=offset, limit=limit), ResourceUsage()
        return await self._operate("library", operation)

    async def import_asset(self, asset: LibraryAsset, destination: str) -> str:
        async def operation(_):
            if self._services.libraries is None:
                raise RuntimeViolation("No asset libraries configured")
            data = await self._services.libraries.read(asset, max_bytes=self._max_file_bytes)
            if len(data) != asset.size_bytes or hashlib.sha256(data).hexdigest() != asset.sha256:
                raise PermissionDenied("Asset library returned bytes inconsistent with its catalog")
            target = self._workspace.output_path(destination)
            if target.exists() or len(data) > self._max_file_bytes:
                raise PermissionDenied("Asset destination exists or exceeds the byte limit")
            if inspect_tree(self._workspace.staging) + len(data) > self._max_output_bytes:
                raise PermissionDenied("Invocation output limit exceeded")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return self.output_path(destination), ResourceUsage()
        return await self._operate("library", operation)

    async def generate_text(self, prompt: str, *, max_output_tokens: int,
                            image_refs: tuple[ArtifactRef, ...] = (), use_vision: bool = False) -> str:
        if use_vision:
            self._policy.allow_tool(self.actor, "vision")
        provider = self._services.vision if use_vision else self._services.text
        if provider is None:
            raise RuntimeViolation("No text provider configured")
        images = []
        if len(image_refs) > 8:
            raise PermissionDenied("At most eight public images may be attached to a model call")
        for ref in image_refs:
            if ref.kind not in ("screenshot", "asset") or not ref.sha256:
                raise PermissionDenied("Vision inputs require hashed public image references")
            data = await self.read_bytes(ref.path)
            if hashlib.sha256(data).hexdigest() != ref.sha256:
                raise PermissionDenied("Vision input checksum mismatch")
            from .media import raster_info
            info = raster_info(data)
            images.append(ModelImage(f"image:{ref.artifact_id}:v{ref.version}", data, info["media_type"]))
        if sum(len(i.data) for i in images) > 8 * 1024 * 1024:
            raise PermissionDenied("Vision inputs exceed the per-call byte limit")
        kwargs = {"images": tuple(images)} if images else {}
        cost = ResourceUsage(
            model_calls=1, input_tokens=provider.input_token_upper_bound(prompt, **kwargs), output_tokens=max_output_tokens,
        )
        if max_output_tokens == 0:
            raise BudgetExceeded("Output token allowance must be positive")
        async def operation(timeout):
            reply = await provider.generate(prompt, max_output_tokens=max_output_tokens, timeout=timeout, **kwargs)
            usage = ResourceUsage(
                model_calls=1, input_tokens=reply.input_tokens,
                cached_input_tokens=reply.cached_input_tokens, output_tokens=reply.output_tokens,
            )
            return reply.text, usage
        return await self._operate("vision" if use_vision else "model", operation, cost=cost, uncertain_tokens=True)

    async def inspect_godot(self, project_ref: ArtifactRef, request: GodotInspectionRequest,
                            *, timeout: float = 180.0) -> dict:
        """Fresh read-only inspection; only Developer/Player may start the engine.

        Every dispatched MCP tools/call is charged. Failed/incomplete sessions
        retain the seven-call reservation when exact accounting is unavailable.
        """
        async def operation(allowed_timeout):
            from .commit import digest_path
            from .media import raster_info
            import json
            backend = self._services.godot_mcp
            if backend is None:
                raise RuntimeViolation("No isolated Godot MCP backend configured")
            request_checked = GodotInspectionRequest.model_validate(request)
            if project_ref.kind != "game_project" or project_ref.created_by != "developer" or not project_ref.sha256:
                raise PermissionDenied("Godot inspection requires a hashed Developer project")
            staged = project_ref.path == self.output_path("game")
            if not staged and not (project_ref.path.startswith("artifacts/developer/") and project_ref.path.endswith("/game")):
                raise PermissionDenied("Inspection requires a Developer game project path")
            if staged and self.actor != Role.DEVELOPER:
                raise PermissionDenied("Only Developer may inspect its staged game")
            path = self._workspace.read_path(project_ref.path)
            inspect_tree(path)
            if not path.is_dir() or digest_path(path) != project_ref.sha256:
                raise PermissionDenied("Godot inspection project checksum mismatch")
            logical_path(request_checked.scene_path)
            scene = path / request_checked.scene_path
            if not scene.is_file():
                raise RuntimeViolation("Requested inspection scene does not exist")
            self._mcp_count += 1
            destination = f"godot_mcp/{self._mcp_count:03}"
            result = await backend.inspect(self._workspace, project_ref.path, destination,
                {**request_checked.model_dump(), "project_sha256": project_ref.sha256,
                 "execution_authorized": True}, allowed_timeout)
            inspect_tree(self._workspace.staging, max_bytes=self._max_output_bytes)
            if digest_path(path) != project_ref.sha256 or result.get("project_sha256") != project_ref.sha256:
                raise PermissionDenied("Godot MCP inspection changed or misidentified the project")
            if (result.get("original_project_unchanged") is not True or result.get("network") != "none" or
                    result.get("fresh_process") is not True or result.get("fresh_user_data") is not True or
                    result.get("acceptance_evidence") is not False or result.get("deterministic_replay") is not False):
                raise PermissionDenied("Godot MCP isolation receipt is invalid")
            calls = result.get("mcp_tool_calls")
            if type(calls) is not int or not 0 <= calls <= MCP_TOOL_CALLS:
                raise PermissionDenied("Invalid MCP usage receipt")
            refs, screenshot = [], None
            for file in sorted(self._workspace.output_path(destination).rglob("*")):
                if not file.is_file():
                    continue
                if file.stat().st_size > self._max_file_bytes:
                    raise PermissionDenied("MCP artifact exceeds file limit")
                relative = file.relative_to(self._workspace.staging).as_posix()
                digest = digest_path(file)
                is_picture = relative.endswith("/session/preview.png")
                if is_picture:
                    if digest != result.get("screenshot_sha256") or raster_info(file.read_bytes())["media_type"] != "image/png":
                        raise PermissionDenied("MCP preview image checksum or format mismatch")
                identity = "mcp-" + hashlib.sha256((self.invocation_id + relative).encode()).hexdigest()[:24]
                ref = ArtifactRef(artifact_id=identity, kind="screenshot" if is_picture else "log",
                    created_by=self.actor, path=self.output_path(relative), sha256=digest,
                    media_type="image/png" if is_picture else "application/json" if file.suffix == ".json" else "text/plain")
                refs.append(ref)
                if is_picture:
                    screenshot = ref
            if result.get("status") == "passed" and screenshot is None:
                raise PermissionDenied("Successful preview has no image")
            return {"result": result, "artifact_refs": tuple(refs), "screenshot_ref": screenshot}, ResourceUsage(tool_calls=calls)
        return await self._operate("godot_inspect", operation, timeout=timeout, tool_calls=MCP_TOOL_CALLS)

    async def generate_image(self, prompt: str, destination: str) -> str:
        provider = self._services.image
        if provider is None:
            raise RuntimeViolation("No image provider configured")
        async def operation(timeout):
            data = await provider.generate(prompt, timeout=timeout)
            if len(data) > self._max_file_bytes or inspect_tree(self._workspace.staging) + len(data) > self._max_output_bytes:
                raise PermissionDenied("Generated image exceeds output limits")
            path = self._workspace.output_path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return self.output_path(destination), ResourceUsage(image_calls=1)
        return await self._operate("image", operation, cost=ResourceUsage(image_calls=1))

    async def run(self, argv: tuple[str, ...], *, timeout: float | None = None) -> ProcessResult:
        async def operation(allowed_timeout):
            backend = self._services.sandbox or DisabledSandbox()
            result = await backend.run(self._workspace, argv, allowed_timeout)
            inspect_tree(self._workspace.staging, max_bytes=self._max_output_bytes)
            return result, ResourceUsage()
        return await self._operate("sandbox", operation, timeout=timeout)
