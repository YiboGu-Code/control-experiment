"""Atomic replacement remains safe under transient Windows file sharing errors."""

import errno

import pytest

from adaptive_game_generation.runtime import store


def windows_denial(code=5):
    error = PermissionError(errno.EACCES, "Fixture replacement denied")
    error.winerror = code
    return error


def test_transient_windows_replace_does_not_repeat_file_write(tmp_path, monkeypatch):
    path = tmp_path / "journal.json"
    path.write_text("old")
    original, attempts, delays = store.os.replace, [], []
    def replace(source, destination):
        attempts.append(source)
        if len(attempts) < 3:
            assert path.read_text() == "old"
            raise windows_denial()
        original(source, destination)
    monkeypatch.setattr(store.os, "replace", replace)
    monkeypatch.setattr(store.time, "sleep", delays.append)
    store.atomic_text(path, "new")
    assert path.read_text() == "new" and len(set(attempts)) == 1
    assert delays == [0.01, 0.02]
    assert not list(tmp_path.glob(".pending-*"))


def test_persistent_windows_denial_keeps_original_and_fails(tmp_path, monkeypatch):
    path = tmp_path / "journal.json"
    path.write_text("old")
    attempts = []
    def replace(*args):
        attempts.append(args)
        raise windows_denial()
    monkeypatch.setattr(store.os, "replace", replace)
    monkeypatch.setattr(store.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        store.atomic_text(path, "new")
    assert len(attempts) == 6 and path.read_text() == "old"
    assert not list(tmp_path.glob(".pending-*"))


def test_unrelated_permission_error_is_not_retried(tmp_path, monkeypatch):
    attempts = []
    def replace(*args):
        attempts.append(args)
        raise PermissionError(errno.EACCES, "Real permissions failure")
    monkeypatch.setattr(store.os, "replace", replace)
    with pytest.raises(PermissionError):
        store.atomic_text(tmp_path / "journal.json", "new")
    assert len(attempts) == 1


def test_artifact_directory_publish_retries_only_the_atomic_rename(tmp_path, monkeypatch):
    from adaptive_game_generation.runtime.workspace import Workspace
    root = tmp_path / "workspace"
    for name in ("public", ".private", ".staging"):
        (root / name).mkdir(parents=True)
    workspace = Workspace(root)
    invocation = workspace.begin("qa_reviewer", "qa-publish-fixture")
    (invocation.staging / "report.json").write_text('{"ready":false}')
    original, attempts = store.os.replace, []
    def replace(source, target):
        attempts.append((source, target))
        if len(attempts) == 1:
            raise windows_denial(32)
        return original(source, target)
    monkeypatch.setattr(store.os, "replace", replace)
    monkeypatch.setattr(store.time, "sleep", lambda _: None)
    workspace.publish(invocation)
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert workspace.read_public(invocation.prefix + "/report.json").read_text() == '{"ready":false}'
