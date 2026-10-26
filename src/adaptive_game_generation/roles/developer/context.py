"""Host-selected context windows over verified public data, with explicit reads."""
import json
import re

from ...runtime import InvalidSubmission
from .edits import source_sha256


def serialized(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def stage_for(files, attempts, brief, inspection, *, forced_deep=False, inspections_exhausted=False):
    if forced_deep:
        return "deep", "Requested escalation or a repair outside the lightweight scope"
    if not attempts:
        return "deep", "Initial implementation or a new invocation requires full reasoning"
    diagnostics = brief.get("diagnostics", ())
    if attempts[-1].status == "failed":
        if (diagnostics and all(re.search(r"parse error:\s*expected\s+[\"']", d["message"], re.I)
                                and d["attempts_with_same_error"] == 1 and d["locations"]
                                and all(p["path"] in files for p in d["locations"]) for d in diagnostics)
                and len({p["path"] for d in diagnostics for p in d["locations"]}) == 1):
            return "local_repair", "One newly observed, located parse error; bounded local repair only"
        return "deep", "Runtime, unknown, multi-file or repeated failure requires deep reasoning"
    if (attempts[-1].status == "passed" and inspection and inspection.records
            and inspection.records[-1].model_turn >= (attempts[-1].model_turn or 0)):
        current = [r for r in inspection.records if r.project_sha256 == inspection.records[-1].project_sha256]
        if all(r.result.get("status") == "passed" and r.visual_review and r.visual_review.get("status") != "invalid"
               for r in current):
            if all(not r.visual_review.get("findings") for r in current):
                return "finalize", "Passing build and current clean visual feedback; decide finish or request more evidence"
            if inspections_exhausted:
                return "finalize", "Current preview reviewed and inspection allowance exhausted; retain unresolved findings or escalate for code changes"
    return "deep", "Gameplay, visual integration or unresolved evidence requires deep reasoning"


class ContextWindow:
    def __init__(self, config):
        self.config = config
        self.pending = ()
        self.read_count = 0
        self.resources = {}
        self.visible_full_files = set()
        self.auto_paths = ()

    def _read_part(self, read):
        if read.resource_id not in self.resources:
            raise InvalidSubmission("Unknown context resource ID; choose only from the public resource catalog")
        text = self.resources[read.resource_id]
        if read.start_line is not None:
            lines = text.splitlines(keepends=True)
            if read.end_line > len(lines):
                raise InvalidSubmission("Context line range exceeds resource bounds")
            text = "".join(lines[read.start_line - 1:read.end_line])
        return text

    def prepare(self, inputs, files, attempts, brief, feedback, rejections, rejected, stage):
        documents = {"input/" + k: v for k, v in inputs.items() if k not in
                     ("task", "objective", "completion_criteria", "execution_paths")}
        documents.update({"build_history": [a.model_dump(mode="json") for a in attempts],
                          "inspection_history": feedback, "proposal_history": [r.model_dump(mode="json") for r in rejections],
                          "rejected_proposal": rejected})
        self.resources = {"source/" + p: text for p, text in files.items()}
        self.resources.update({key: serialized(value) for key, value in documents.items() if value is not None})
        from ..capabilities import DEVELOPER_SKILLS
        self.resources.update(DEVELOPER_SKILLS)
        # Retain a bounded working set across reads AND source patches. Always
        # resolve against this turn's verified snapshot, never cached old text.
        retained, evicted, retained_bytes = [], [], 0
        for read in reversed(self.pending):
            try:
                size = len(self._read_part(read).encode("utf-8"))
            except InvalidSubmission:
                evicted.append({"resource_id": read.resource_id, "reason": "Resource removed or line range no longer valid"})
                continue
            if retained_bytes + size > self.config.max_context_response_bytes:
                evicted.append({"resource_id": read.resource_id, "reason": "Updated working set exceeds retained byte budget"})
                continue
            retained.append(read)
            retained_bytes += size
        self.pending = tuple(reversed(retained))
        index = [{"resource_id": key, "sha256": source_sha256(text), "bytes": len(text.encode("utf-8")),
                  "lines": len(text.splitlines())} for key, text in sorted(self.resources.items())]
        source_index = []
        for path, text in files.items():
            source_index.append({"path": path, "sha256": source_sha256(text), "bytes": len(text.encode("utf-8")),
                "symbols": re.findall(r"(?m)^(?:class_name|func|static func|class)\s+([^\n:]+)", text)[:32]})
        self.visible_full_files = set()
        selected, used = {}, 0
        paths = list(brief.get("source_excerpts", {})) + ["project.godot", "Main.tscn", *self.auto_paths]
        if not attempts:
            paths += sorted(files, key=lambda p: (p.endswith(".uid"), len(files[p])))
        for path in dict.fromkeys(paths):
            if path not in files:
                continue
            size = len(files[path].encode("utf-8"))
            if used + size <= self.config.auto_source_bytes:
                selected[path] = files[path]
                self.visible_full_files.add(path)
                used += size
        self.auto_paths = tuple(selected)
        results, requested_bytes = [], 0
        for read in self.pending:
            # Resolve again against the current verified snapshot, never a file path.
            if read.resource_id not in self.resources:
                raise InvalidSubmission("Requested context resource is no longer available")
            text = self.resources[read.resource_id]
            if read.start_line is not None:
                lines = text.splitlines(keepends=True)
                if read.end_line > len(lines):
                    raise InvalidSubmission("Context line range exceeds resource bounds")
                part = "".join(lines[read.start_line - 1:read.end_line])
            else:
                part = text
            requested_bytes += len(part.encode("utf-8"))
            if requested_bytes > self.config.max_context_response_bytes:
                raise InvalidSubmission("Context response exceeds the configured byte budget; request narrower ranges")
            if read.resource_id.startswith("source/") and read.start_line is None:
                path = read.resource_id[len("source/"):]
                selected[path] = text
                self.visible_full_files.add(path)
                results.append({"resource_id": read.resource_id, "sha256": source_sha256(text),
                                "location": "current_project_files", "complete": True})
            else:
                results.append({"resource_id": read.resource_id, "sha256": source_sha256(text),
                    "start_line": read.start_line, "end_line": read.end_line, "text": part,
                    "complete": read.start_line is None})
        # Repeated requests accumulate; unrelated reads must not erase a file
        # already acquired at the cost of a model turn and context allowance.
        compact = {k: v for k, v in inputs.items() if k not in
                   ("design", "art_manifest", "previous_report", "requirements", "attachments", "evidence", "asset_packages")}
        compact["requirements"] = [{k: v for k, v in r.items() if k not in ("verification", "schema_version")}
                                   for r in inputs["requirements"]]
        compact["design"] = inputs["design"] if not files else {"resource_id": "input/design", "omitted": True}
        compact["art_manifest"] = {"resource_id": "input/art_manifest", "assets": [
            {"asset_id": a["record"]["asset_id"], "purpose": a["record"]["purpose"],
             "filter_mode": a["request"]["filter_mode"], "integration_notes": a["record"]["integration_notes"],
             "is_mock": a["is_mock"]} for a in (inputs.get("art_manifest") or {}).get("assets", ())]}
        compact["asset_packages"] = [{k: v for k, v in p.items() if k in
            ("asset_id", "source_target_path", "status", "source_only", "layout", "frames", "quality", "limitations")}
            for p in inputs.get("asset_packages", ())]
        prior = inputs.get("previous_report") or {}
        compact["previous_report"] = {"resource_id": "input/previous_report",
            **{k: prior[k] for k in ("game_revision_id", "limitations", "controls", "scenarios", "self_review_completed") if k in prior}}
        compact["attachments"] = [{k: v for k, v in a.items() if k != "text"} for a in inputs.get("attachments", ())]
        compact["evidence"] = [{k: v for k, v in e.items() if k not in ("observation", "artifact_refs")} for e in inputs.get("evidence", ())]
        catalog = {"source_index": source_index, "resources": index, "read_results": results,
                   "reads_left": self.config.max_context_reads - self.read_count,
                   "retained_bytes": retained_bytes, "retained_byte_limit": self.config.max_context_response_bytes,
                   "evicted_resources": evicted,
                   "meaning": "Full verified resources remain available through status=context. Omitted content is not evidence of absence."}
        latest_feedback = None
        if feedback:
            latest_feedback = {"inspections_left": feedback["inspections_left"], "inspections": [
                {k: v for k, v in r.items() if k in ("model_turn", "project_sha256", "request", "visual_review", "screenshot_ref")}
                | {"inspection_status": r["result"].get("status")} for r in feedback["inspections"][-3:]]}
        return compact, selected, catalog, latest_feedback

    def request(self, decision):
        if any(key not in self.resources for key in decision.drop_resources):
            raise InvalidSubmission("Unknown context resource ID to release")
        proposed = [r for r in self.pending if r.resource_id not in decision.drop_resources]
        for read in decision.reads:
            self._read_part(read)
            if read.start_line is None:
                if read in proposed:
                    continue
                proposed = [r for r in proposed if r.resource_id != read.resource_id]
            elif any(r.resource_id == read.resource_id and (r.start_line is None or r == read) for r in proposed):
                continue
            proposed.append(read)
        total = sum(len(self._read_part(r).encode("utf-8")) for r in proposed)
        if total > self.config.max_context_response_bytes:
            raise InvalidSubmission("Retained context exceeds byte budget; release unused drop_resources or request narrower ranges")
        changed = tuple(proposed) != self.pending
        if changed and decision.reads and self.read_count >= self.config.max_context_reads:
            raise InvalidSubmission("Context request allowance exhausted; retained reads are still available. Use them or report remaining work")
        self.pending = tuple(proposed)
        if (changed and decision.reads) or decision.request_deep_reasoning:
            self.read_count = min(self.config.max_context_reads, self.read_count + 1)

    def validate_scope(self, plan, files, stage):
        for file in plan.files:
            if file.path in files and file.path not in self.visible_full_files:
                raise InvalidSubmission(f"Full rewrite requires reading the complete current source: source/{file.path}; otherwise use an exact local edit")
        if stage == "finalize":
            raise InvalidSubmission("Finalization profile cannot change code; request deep reasoning before further implementation")
        if stage == "local_repair":
            size = sum(len(f.content.encode("utf-8")) for f in plan.files)
            size += sum(len(r.old_text.encode("utf-8")) + len(r.new_text.encode("utf-8")) for e in plan.edits for r in e.replacements)
            if plan.delete_paths or len(plan.files) + len(plan.edits) > 2 or size > 8192 or any(f.path not in files for f in plan.files):
                raise InvalidSubmission("Repair exceeds the local profile scope; escalate to deep reasoning")
