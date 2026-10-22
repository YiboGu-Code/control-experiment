"""Mounted library boundary tests using tiny local files, never downloaded assets."""

import asyncio
import os

import pytest
from PIL import Image

from adaptive_game_generation.runtime import InvalidSubmission, PermissionDenied
from adaptive_game_generation.runtime.asset_libraries import MountedAssetLibraries


def make_library(tmp_path, *, limit=20000):
    root = tmp_path / "library"
    (root / "pack").mkdir(parents=True)
    (root / "pack/LICENSE.txt").write_text("Synthetic fixture license; retain attribution.", encoding="utf-8")
    for i in range(3):
        Image.new("RGB", (16, 16), (i, 0, 0)).save(root / f"pack/player{i}.png")
    return MountedAssetLibraries(kenney=root, opengameart=tmp_path / "absent", max_entries=limit), root


def test_search_pagination_and_missing_mount(tmp_path):
    library, root = make_library(tmp_path)
    first = asyncio.run(library.search("kenney", "player", limit=2))
    assert len(first.assets) == 2 and first.next_offset == 2
    second = asyncio.run(library.search("kenney", "player", offset=first.next_offset, limit=2))
    assert len(second.assets) == 1 and second.next_offset is None
    assert len({a.path for a in (*first.assets, *second.assets)}) == 3
    assert all(str(root) not in a.model_dump_json() for a in first.assets)
    assert not asyncio.run(library.search("opengameart", "")).assets


@pytest.mark.parametrize("change", ["asset", "license"])
def test_source_drift_between_search_and_import_is_rejected(tmp_path, change):
    library, root = make_library(tmp_path)
    asset = asyncio.run(library.search("kenney", "player")).assets[0]
    if change == "asset":
        Image.new("RGB", (16, 16), (255, 0, 0)).save(root / asset.path)
    else:
        (root / asset.license_path).write_text("Changed license", encoding="utf-8")
    with pytest.raises(InvalidSubmission, match="changed"):
        asyncio.run(library.read(asset, max_bytes=10000))


def test_private_paths_missing_license_and_unsupported_formats_are_excluded(tmp_path):
    library, root = make_library(tmp_path)
    (root / "tests").mkdir()
    Image.new("RGB", (16, 16)).save(root / "tests/hidden.png")
    (root / "unlicensed").mkdir()
    Image.new("RGB", (16, 16)).save(root / "unlicensed/item.png")
    (root / "pack/evil.gd").write_text("hidden executable")
    (root / "pack/fake.png").write_text("not an image")
    found = asyncio.run(library.search("kenney", ""))
    assert len(found.assets) == 3
    selected = found.assets[0]
    for path in ("../secret.png", "/absolute.png", "pack\\player0.png", "tests/hidden.png", "pack/evil.gd"):
        with pytest.raises(PermissionDenied):
            asyncio.run(library.read(selected.revised(path=path), max_bytes=10000))


def test_hardlink_and_task_root_are_rejected(tmp_path):
    library, root = make_library(tmp_path)
    os.link(root / "pack/player0.png", root / "pack/linked.png")
    with pytest.raises(PermissionDenied, match="Hardlinked"):
        asyncio.run(library.search("kenney", ""))
    (root / "instruction.md").write_text("Task")
    with pytest.raises(PermissionDenied, match="benchmark task"):
        asyncio.run(library.availability())


def test_bounded_scan_reports_incomplete_inventory(tmp_path):
    library, _ = make_library(tmp_path, limit=1)
    assert asyncio.run(library.search("kenney", "")).scan_truncated
    with pytest.raises(InvalidSubmission):
        asyncio.run(library.search("kenney", "x" * 257))


def test_oga_requires_entry_license_instead_of_library_wide_license(tmp_path):
    root = tmp_path / "oga"
    (root / "entry").mkdir(parents=True)
    (root / "LICENSE.txt").write_text("This only describes the catalog, not every entry.")
    Image.new("RGB", (16, 16)).save(root / "entry/asset.png")
    library = MountedAssetLibraries(opengameart=root, kenney=tmp_path / "absent")
    assert not asyncio.run(library.search("opengameart", "")).assets
    (root / "entry/LICENSE.txt").write_text("Actual entry license")
    assert len(asyncio.run(library.search("opengameart", "")).assets) == 1


@pytest.mark.parametrize("location", ["inside", "ancestor"])
def test_mounts_cannot_overlap_hidden_benchmark_roots(tmp_path, location):
    forbidden = tmp_path / "benchmark"
    mount = forbidden / "task" if location == "inside" else tmp_path
    with pytest.raises(PermissionDenied, match="separate"):
        MountedAssetLibraries(kenney=mount, forbidden_roots=(forbidden,))


def test_catalog_identity_tracks_host_configuration_without_leaking_paths(tmp_path):
    library, root = make_library(tmp_path)
    same = MountedAssetLibraries(kenney=root, opengameart=tmp_path / "absent")
    changed = MountedAssetLibraries(kenney=root, opengameart=tmp_path / "absent", snapshot_id="snapshot-2")
    assert library.configuration_id == same.configuration_id != changed.configuration_id
    assert str(root) not in library.configuration_id


def test_env_mount_overrides_and_absent_defaults(tmp_path):
    pytest.importorskip("dotenv")
    library, root = make_library(tmp_path)
    env = tmp_path / ".env"
    env.write_text(f"ART_KENNEY_ROOT={root.as_posix()}\nART_OGA_ROOT=/absent-oga\n", encoding="utf-8")
    configured = MountedAssetLibraries.from_env(env, environ={})
    assert asyncio.run(configured.availability())[0]["available"]
    overridden = MountedAssetLibraries.from_env(env, environ={"ART_KENNEY_ROOT": str(tmp_path / "missing")})
    assert not asyncio.run(overridden.availability())[0]["available"]
