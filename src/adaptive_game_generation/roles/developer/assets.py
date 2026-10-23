"""Source-only atlas guards shared by every Developer invocation and controller."""
from pathlib import PurePosixPath
import re


def asset_reference_findings(files, packages):
    findings = []
    for package in packages:
        if not package.source_only:
            continue
        alternatives = ", ".join(f.target_path for f in package.frames[:4]) or "no usable frames; ask Art Producer to correct or replace this source"
        pattern = re.compile(r"[\"'](?:res://)?" + re.escape(package.source_target_path) + r"[\"']", re.IGNORECASE)
        for path, content in files.items():
            if PurePosixPath(path).suffix not in {".gd", ".tscn", ".tres", ".gdshader"} and path != "project.godot":
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if line.lstrip().startswith(("#", ";", "//")):
                    continue
                if pattern.search(line):
                    findings.append({"path": path, "line": number, "severity": "error", "code": "source_atlas_reference",
                        "message": f"{package.source_target_path} is source-only and is not packaged. Use normalized frames: {alternatives}. Do not load the full atlas or add region slicing to it."})
    return findings


def asset_package_findings(file_hashes, packages):
    """Detect original filenames and renamed copies of known atlas bytes."""
    findings = []
    for package in packages:
        if not package.source_only:
            continue
        for path, digest in file_hashes.items():
            original_path = path.casefold() == package.source_target_path.casefold()
            atlas_copy = (package.layout.mode != "whole" or package.status == "rejected") and digest == package.source_ref.sha256
            if original_path or atlas_copy:
                findings.append(f"Source-only asset {package.asset_id} was included at {path}")
    return findings
