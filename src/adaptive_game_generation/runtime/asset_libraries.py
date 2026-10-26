"""Read-only, host-selected Kenney and OpenGameArt mounts. No downloading."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal, Mapping, Protocol

from pydantic import Field

from ..models.base import Model, PositiveCount, Text
from ..models.asset_pipeline import AtlasMetadata
from .asset_normalization import parse_atlas_metadata
from .errors import InvalidSubmission, PermissionDenied
from .media import raster_info
from .workspace import logical_path, no_links, safe_path

LibraryId = Literal["kenney", "opengameart"]
_PRIVATE = {".git", ".aws", ".codex", ".private", "tests", "solution", "solutions", "rubric.json"}
_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
          ".ttf": "font/ttf", ".otf": "font/otf", ".ogg": "audio/ogg", ".wav": "audio/wav"}


class LibraryAsset(Model):
    library: LibraryId
    path: Text
    sha256: Text
    size_bytes: PositiveCount
    media_type: Text
    width: PositiveCount | None = None
    height: PositiveCount | None = None
    has_alpha: bool = False
    license_path: Text
    license_text: Text
    license_sha256: Text
    atlas_metadata: AtlasMetadata | None = None


class LibrarySearch(Model):
    library: LibraryId
    query: str
    offset: int = Field(ge=0)
    assets: tuple[LibraryAsset, ...] = ()
    next_offset: int | None = None
    scan_truncated: bool = False


class AssetLibraryProvider(Protocol):
    async def availability(self) -> tuple[dict, ...]: ...
    async def search(self, library: LibraryId, query: str, *, offset: int, limit: int) -> LibrarySearch: ...
    async def read(self, asset: LibraryAsset, *, max_bytes: int) -> bytes: ...


class MountedAssetLibraries:
    """Roots are trusted host configuration, never model-supplied paths.

    Each search scans at most max_entries paths and returns at most 20 candidates.
    A truncated scan is explicit, not a claim that the remaining library is empty.
    Both libraries require a nearby LICENSE.txt (Kenney also accepts License.txt
    and license files with a .md extension). Original license text is preserved.
    """

    def __init__(self, *, kenney: Path | None = None, opengameart: Path | None = None,
                 max_entries: int = 20000, max_asset_bytes: int = 16 * 1024 * 1024,
                 forbidden_roots: tuple[Path, ...] = (), snapshot_id: str = "unversioned"):
        self._roots = {"kenney": Path(kenney or "/workspace/assets/library").absolute(),
                       "opengameart": Path(opengameart or "/workspace/assets/library-oga").absolute()}
        if max_entries < 1 or max_asset_bytes < 1:
            raise ValueError("Library limits must be positive")
        self.max_entries, self.max_asset_bytes = max_entries, max_asset_bytes
        self.snapshot_id = snapshot_id
        for root in self._roots.values():
            self._public_path(root)
            no_links(root)
            if any(root.resolve().is_relative_to(Path(blocked).resolve()) or
                   Path(blocked).resolve().is_relative_to(root.resolve()) for blocked in forbidden_roots):
                raise PermissionDenied("Asset mounts must be separate from benchmark and execution roots")

    @property
    def configuration_id(self):
        payload = {"roots": {key: str(path) for key, path in self._roots.items()},
                   "max_entries": self.max_entries, "max_asset_bytes": self.max_asset_bytes,
                   "snapshot_id": self.snapshot_id}
        return "mounted-assets-" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    @classmethod
    def from_env(cls, env_file: Path, *, environ: Mapping[str, str] | None = None,
                 forbidden_roots: tuple[Path, ...] = ()):
        from dotenv import dotenv_values
        values = {**dotenv_values(env_file, interpolate=False), **(os.environ if environ is None else environ)}
        return cls(kenney=values.get("ART_KENNEY_ROOT"), opengameart=values.get("ART_OGA_ROOT"),
                   forbidden_roots=forbidden_roots, snapshot_id=values.get("ART_LIBRARY_SNAPSHOT") or "unversioned")

    @staticmethod
    def _public_path(path: Path):
        if any(part.lower() in _PRIVATE or part.lower().startswith(".env") for part in path.parts):
            raise PermissionDenied("Private material is not an asset library")

    def _root(self, library):
        if library not in self._roots:
            raise PermissionDenied("Unknown asset library")
        root = self._roots[library]
        no_links(root)
        # Do not allow a benchmark task root to become a library mount.
        if (root / "instruction.md").exists():
            raise PermissionDenied("A benchmark task cannot be mounted as an asset library")
        return root

    async def availability(self):
        return tuple({"library": name, "available": self._root(name).is_dir(),
                      "supported_extensions": sorted(_TYPES),
                      "license_policy": "Original nearby LICENSE.txt required; preserve and review its terms"}
                     for name in self._roots)

    def mount_diagnostics(self):
        """Host-only mount paths for preflight, never included in role catalogs."""
        return [{"library": name, "root": str(self._root(name)),
                 "available": self._root(name).is_dir()} for name in self._roots]

    def _license(self, root, path, library):
        parent = path.parent
        while True:
            if library == "opengameart" and parent == root:
                raise InvalidSubmission("OpenGameArt assets require an entry-specific LICENSE.txt")
            names = ("LICENSE.txt", "License.txt", "license.txt")
            if library == "kenney":
                names += ("LICENSE.md", "License.md", "license.md")
            for name in names:
                candidate = parent / name
                no_links(candidate)
                if candidate.is_file():
                    if candidate.stat().st_size > 32768:
                        raise InvalidSubmission("Asset license exceeds the text limit")
                    data = candidate.read_bytes()
                    text = data.decode("utf-8-sig")
                    if not text.strip():
                        raise InvalidSubmission("Asset license is empty")
                    return candidate.relative_to(root).as_posix(), text, hashlib.sha256(data).hexdigest()
            if parent == root:
                raise InvalidSubmission("Asset has no required license file")
            parent = parent.parent

    def _inspect(self, library, relative, max_bytes):
        root = self._root(library)
        logical_path(relative)
        path = safe_path(root, relative)
        self._public_path(path)
        if path.suffix.lower() not in _TYPES or not path.is_file():
            raise PermissionDenied("Unsupported asset file")
        if path.stat().st_size > min(max_bytes, self.max_asset_bytes):
            raise InvalidSubmission("Library asset exceeds the byte limit")
        with path.open("rb") as stream:
            data = stream.read(min(max_bytes, self.max_asset_bytes) + 1)
        if not data or len(data) > min(max_bytes, self.max_asset_bytes):
            raise InvalidSubmission("Empty or oversized library asset")
        license_path, license_text, license_sha = self._license(root, path, library)
        media = {"media_type": _TYPES[path.suffix.lower()]}
        metadata = None
        if media["media_type"].startswith("image/"):
            media = raster_info(data)
            if media["media_type"] != _TYPES[path.suffix.lower()]:
                raise InvalidSubmission("Asset extension does not match raster bytes")
            for suffix in (".json", ".xml"):
                sidecar = safe_path(root, path.with_suffix(suffix).relative_to(root).as_posix())
                if sidecar.is_file() and sidecar.stat().st_size <= 256 * 1024:
                    metadata = parse_atlas_metadata(sidecar.read_bytes(), sidecar.relative_to(root).as_posix(), path.name)
                    break
        return data, LibraryAsset(library=library, path=relative,
            sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), **media,
            license_path=license_path, license_text=license_text, license_sha256=license_sha,
            atlas_metadata=metadata)

    async def search(self, library, query, *, offset=0, limit=12):
        if not isinstance(query, str) or len(query) > 256 or not 0 <= offset <= self.max_entries or not 1 <= limit <= 20:
            raise InvalidSubmission("Invalid bounded asset search")
        root = self._root(library)
        if not root.is_dir():
            return LibrarySearch(library=library, query=query, offset=offset)
        terms = re.findall(r"[\w-]+", query.lower())
        # Generic words such as "sprite" used to match every Sprites/ directory,
        # letting the first unrelated atlas hide actual player images.
        specific = [t for t in terms if t not in {"sprite", "sprites", "asset", "assets", "pack", "packs", "png", "image", "images"}]
        terms = specific or terms
        candidates, assets, matches, visited, truncated = [], [], 0, 0, False
        # scandir iteration bounds memory even when a mount contains huge directories.
        stack = [root]
        while stack:
            directory = stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > self.max_entries:
                        truncated = True
                        break
                    if visited % 32 == 0:
                        await asyncio.sleep(0)
                    path = Path(entry.path)
                    if any(p.lower() in _PRIVATE or p.lower().startswith(".env") for p in path.parts):
                        continue
                    # Fail closed on links rather than following them into other mounts.
                    no_links(path)
                    if path.is_dir():
                        stack.append(path)
                        continue
                    relative = path.relative_to(root).as_posix()
                    if path.suffix.lower() not in _TYPES or (terms and not any(t in relative.lower() for t in terms)):
                        continue
                    name = path.stem.lower()
                    name_hits = sum(t in name for t in terms)
                    path_hits = sum(t in relative.lower() for t in terms)
                    sheet = any(t in relative.lower() for t in ("spritesheet", "tilesheet", "atlas", "preview", "sample"))
                    candidates.append(((-name_hits, -path_hits, sheet, relative.casefold()), relative))
            if truncated:
                break
        # Rank paths first; only then read/hash eligible files and license text.
        # The bounded scan and every returned truncation flag remain explicit.
        for _, relative in sorted(candidates):
            await asyncio.sleep(0)
            try:
                _, asset = self._inspect(library, relative, self.max_asset_bytes)
            except (InvalidSubmission, UnicodeError):
                continue
            matches += 1
            if matches <= offset:
                continue
            assets.append(asset)
            if len(assets) > limit:
                return LibrarySearch(library=library, query=query, offset=offset,
                    assets=tuple(assets[:limit]), next_offset=offset + limit, scan_truncated=truncated)
        return LibrarySearch(library=library, query=query, offset=offset,
                             assets=tuple(assets), scan_truncated=truncated)

    async def read(self, asset: LibraryAsset, *, max_bytes: int):
        data, current = self._inspect(asset.library, asset.path, max_bytes)
        if current != asset:
            raise InvalidSubmission("Library asset or license changed after selection")
        return data
