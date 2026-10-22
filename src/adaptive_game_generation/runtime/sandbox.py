"""Optional Docker process isolation. Never falls back to host execution."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from .errors import PermissionDenied, SandboxCleanupFailed, SandboxUnavailable
from .workspace import InvocationWorkspace, inspect_tree


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class SandboxBackend(Protocol):
    """Trusted adapter: must enforce isolation and terminate on cancellation."""

    async def run(self, workspace: InvocationWorkspace, argv: tuple[str, ...], timeout: float) -> ProcessResult: ...


class DisabledSandbox:
    async def run(self, workspace: InvocationWorkspace, argv: tuple[str, ...], timeout: float) -> ProcessResult:
        raise SandboxUnavailable("Configure an isolated backend before executing generated code")


@dataclass(frozen=True)
class DockerSandbox:
    image: str
    executable: str = "docker"
    memory_mb: int = 1024
    cpus: float = 1.0
    pids: int = 128
    tmpfs_mb: int = 256
    output_limit_bytes: int = 1024 * 1024
    user: str = "65534:65534"
    _cleanup_failed: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self):
        if not self.image or self.image.startswith("-") or any(c.isspace() for c in self.image):
            raise ValueError("A trusted container image reference is required")
        if min(self.memory_mb, self.cpus, self.pids, self.tmpfs_mb, self.output_limit_bytes) <= 0:
            raise ValueError("Sandbox resource limits must be positive")
        if not all(part.isdigit() and int(part) > 0 for part in self.user.split(":")):
            raise ValueError("Sandbox must use a non-root numeric user/group")

    def command(self, workspace: InvocationWorkspace, argv: tuple[str, ...], name: str) -> list[str]:
        if not argv or not argv[0] or any("\x00" in arg for arg in argv):
            raise PermissionDenied("Invalid sandbox command")
        for directory in (workspace.public, workspace.staging):
            inspect_tree(directory)
            if "," in str(directory):
                raise PermissionDenied("Sandbox mount paths cannot contain commas")
        return [
            self.executable, "run", "--rm", "--pull=never", "--name", name,
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit", str(self.pids),
            "--memory", f"{self.memory_mb}m", "--memory-swap", f"{self.memory_mb}m",
            "--cpus", str(self.cpus), "--user", self.user, "--init", "--no-healthcheck",
            "--log-driver=none", "--ulimit", "nofile=256:256",
            "--tmpfs", f"/tmp:rw,nosuid,nodev,size={self.tmpfs_mb}m,mode=1777",
            "--env", "HOME=/tmp", "--env", "XDG_CACHE_HOME=/tmp/cache",
            "--mount", f"type=bind,src={workspace.public},dst=/workspace,readonly",
            "--mount", f"type=bind,src={workspace.staging},dst=/output",
            "--workdir", "/output", "--entrypoint", argv[0], self.image, *argv[1:],
        ]

    async def run(self, workspace: InvocationWorkspace, argv: tuple[str, ...], timeout: float) -> ProcessResult:
        if self._cleanup_failed:
            raise SandboxCleanupFailed("Previous container cleanup failed; this backend is disabled")
        name = "gamecraft-" + uuid.uuid4().hex
        command = self.command(workspace, argv, name)
        # Credentials are never forwarded to the container. These variables are
        # only for locating the trusted Docker client and its local configuration.
        environment = {key: value for key, value in os.environ.items() if key.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
        }}
        async def read_bounded(stream):
            chunks, size = [], 0
            while chunk := await stream.read(65536):
                size += len(chunk)
                if size > self.output_limit_bytes:
                    raise PermissionDenied("Sandbox output limit exceeded")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace")

        process, readers, spawned = None, [], False
        try:
            launch = asyncio.create_task(asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL, env=environment,
            ))
            try:
                try:
                    process = await asyncio.shield(launch)
                except asyncio.CancelledError:
                    # Finish acquiring the process handle before cleanup. Without
                    # this, cancellation during spawn can orphan a Docker client.
                    process = await launch
                    spawned = True
                    raise
                spawned = True
            except OSError as exc:
                raise SandboxUnavailable("Container runtime is unavailable") from exc
            readers = [asyncio.create_task(read_bounded(process.stdout)), asyncio.create_task(read_bounded(process.stderr))]
            async def collect():
                stdout, stderr = await asyncio.gather(*readers)
                return ProcessResult(await process.wait(), stdout, stderr)
            result = await asyncio.wait_for(collect(), timeout)
            if result.returncode == 125:
                raise SandboxUnavailable("Container could not start; check runtime and preloaded image")
            return result
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            # Killing the CLI is insufficient: remove the container by its unique
            # runtime-owned name as well, including after cancellation/timeouts.
            if spawned:
                await self._remove(name, environment)

    async def _remove(self, name: str, environment: dict[str, str]) -> None:
        try:
            cleanup = await asyncio.create_subprocess_exec(
                self.executable, "rm", "--force", name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            try:
                _, cleanup_error = await asyncio.wait_for(cleanup.communicate(), 10.0)
                if cleanup.returncode != 0 and b"no such container" not in cleanup_error.lower():
                    object.__setattr__(self, "_cleanup_failed", True)
                    raise SandboxCleanupFailed("Container cleanup could not be confirmed; backend disabled")
            except asyncio.TimeoutError:
                cleanup.kill()
                await cleanup.wait()
                object.__setattr__(self, "_cleanup_failed", True)
                raise SandboxCleanupFailed("Container cleanup timed out; backend disabled")
        except OSError as exc:
            object.__setattr__(self, "_cleanup_failed", True)
            raise SandboxCleanupFailed("Container cleanup is unavailable; backend disabled") from exc
