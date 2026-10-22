"""Exercise process lifecycle paths without requiring a local container service."""

import asyncio
from pathlib import Path
import os
import stat

import pytest

from adaptive_game_generation.runtime import DockerSandbox, PermissionDenied, Workspace
from adaptive_game_generation.runtime.errors import SandboxCleanupFailed, SandboxFilesystemError
from adaptive_game_generation.runtime.sandbox import OUTPUT_BOOTSTRAP, prepare_output_tree


class Process:
    def __init__(self, *, stdout=b"", stderr=b"", returncode=0):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stderr.feed_data(stderr)
        self.returncode = returncode
        self.error = stderr
        self.killed = False
        if returncode is not None:
            self.stdout.feed_eof()
            self.stderr.feed_eof()

    async def wait(self):
        return self.returncode

    async def communicate(self):
        return None, self.error

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()


@pytest.fixture
def work(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    (task / "instruction.md").write_text("Task")
    workspace, _ = Workspace.prepare(tmp_path / "run", task, task_id="task")
    return workspace.begin("developer", "invocation")


def test_timeout_kills_client_and_removes_container(monkeypatch, work):
    commands, processes = [], []
    async def spawn(*command, **kwargs):
        commands.append(command)
        process = Process(returncode=None if command[1] == "run" else 0)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    backend = DockerSandbox(image="local-image")
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(backend.run(work, ("python",), 0.01))
    assert processes[0].killed
    assert commands[1][1:3] == ("rm", "--force")
    name = commands[0][commands[0].index("--name") + 1]
    assert commands[1][-1] == name


def test_excess_output_is_rejected_and_container_removed(monkeypatch, work):
    commands = []
    async def spawn(*command, **kwargs):
        commands.append(command)
        return Process(stdout=b"too much output" if command[1] == "run" else b"")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(PermissionDenied):
        asyncio.run(DockerSandbox(image="local-image", output_limit_bytes=4).run(work, ("python",), 1.0))
    assert commands[-1][1] == "rm"


def test_cleanup_failure_disables_backend(monkeypatch, work):
    commands = []
    async def spawn(*command, **kwargs):
        commands.append(command)
        return Process(returncode=1, stderr=b"daemon inaccessible") if command[1] == "rm" else Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    backend = DockerSandbox(image="local-image")
    with pytest.raises(SandboxCleanupFailed):
        asyncio.run(backend.run(work, ("python",), 1.0))
    with pytest.raises(SandboxCleanupFailed):
        asyncio.run(backend.run(work, ("python",), 1.0))
    assert len(commands) == 2


def test_cancellation_during_spawn_still_acquires_handle_and_cleans_up(monkeypatch, work):
    commands, processes = [], []
    started = asyncio.Event()
    async def spawn(*command, **kwargs):
        commands.append(command)
        if command[1] == "run":
            started.set()
            await asyncio.sleep(0.01)
            process = Process(returncode=None)
        else:
            process = Process()
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    async def cancel():
        task = asyncio.create_task(DockerSandbox(image="local-image").run(work, ("python",), 1.0))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    assert processes[0].killed
    assert commands[-1][1] == "rm"


def test_prepare_output_covers_nested_directories_and_existing_import_files(monkeypatch, work):
    nested = work.staging / "game/assets/sprites"
    nested.mkdir(parents=True)
    existing = nested / "hero.png.import"
    existing.write_text("Existing import metadata")
    changed = {}
    original = Path.chmod
    def record(path, mode, **kwargs):
        changed[path] = mode
        return original(path, mode, **kwargs)
    monkeypatch.setattr(Path, "chmod", record)
    prepare_output_tree(work.staging)
    assert all(path.is_relative_to(work.staging) for path in changed)
    assert changed.get(nested, stat.S_IMODE(nested.stat().st_mode)) == 0o777
    assert changed.get(existing, stat.S_IMODE(existing.stat().st_mode)) & 0o666 == 0o666
    assert existing.read_text() == "Existing import metadata"
    if os.name == "posix":
        assert stat.S_IMODE(nested.stat().st_mode) == 0o777
        assert stat.S_IMODE(existing.stat().st_mode) == 0o666


def test_links_are_rejected_before_any_permission_mutation(monkeypatch, work, tmp_path):
    target = tmp_path / "external.txt"
    target.write_text("Private host data")
    # Hard links also work on Windows without symlink privileges.
    os.link(target, work.staging / "linked.txt")
    changed = []
    monkeypatch.setattr(Path, "chmod", lambda *args, **kwargs: changed.append(args))
    with pytest.raises(PermissionDenied, match="Hardlinked"):
        prepare_output_tree(work.staging)
    assert not changed and target.read_text() == "Private host data"


def test_permission_preparation_failure_does_not_start_container(monkeypatch, work):
    (work.staging / "game").mkdir()
    existing = work.staging / "game/existing.import"
    existing.write_text("Existing metadata")
    existing.chmod(0o444)
    def denied(*args, **kwargs):
        raise PermissionError("Host-only details must not reach the role")
    monkeypatch.setattr(Path, "chmod", denied)
    with pytest.raises(SandboxFilesystemError, match="host ownership") as error:
        asyncio.run(DockerSandbox("local-image").run(work, ("godot",), 1))
    assert "Host-only details" not in str(error.value)


def test_container_write_probe_failure_is_terminal_and_cleans_up(monkeypatch, work):
    commands = []
    async def spawn(*command, **kwargs):
        commands.append(command)
        return Process(returncode=73, stderr=b"GAMECRAFT_OUTPUT_NOT_WRITABLE\n") if command[1] == "run" else Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(SandboxFilesystemError, match="bind-mount"):
        asyncio.run(DockerSandbox("local-image").run(work, ("godot",), 1))
    assert commands[-1][1] == "rm"


def test_wrapper_forwards_arguments_without_shell_interpolation(work):
    argv = ("program", "literal $(touch /output/should-not-exist)", "has space", "'")
    command = DockerSandbox("local-image").command(work, argv, "probe")
    assert command[-len(argv):] == list(argv)
    assert command[command.index("--entrypoint") + 1] == "/bin/sh"
    assert '\n"$@"\n' in OUTPUT_BOOTSTRAP and "umask 000" in OUTPUT_BOOTSTRAP
