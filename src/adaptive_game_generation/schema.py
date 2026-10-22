"""Export versioned JSON schemas without importing model providers or engines."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import ArtifactRef, LedgerEvent, RoleRequest, RoleResult, RunLedger, SharedState


def export_schemas(directory: Path) -> tuple[Path, ...]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, model in (
        ("artifact_ref", ArtifactRef), ("shared_state", SharedState),
        ("role_request", RoleRequest), ("role_result", RoleResult),
        ("ledger_event", LedgerEvent), ("run_ledger", RunLedger),
    ):
        path = directory / f"{name}.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths.append(path)
    return tuple(paths)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for path in export_schemas(args.output):
        print(path)


if __name__ == "__main__":
    main()
