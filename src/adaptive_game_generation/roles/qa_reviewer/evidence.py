"""Metered public-artifact reads and independently checked Player recordings."""

import hashlib
import io
import re
from pathlib import PurePosixPath

from PIL import Image

from ...models import ArtifactRef
from ...runtime import InvalidSubmission
from ..art_producer.contracts import ArtManifest
from ..developer import DeveloperReport
from ..player import PlayerReport
from ..player.contracts import InputTrace
from ..player.role import compare_sessions, strict_json


def check(condition, message):
    if not condition:
        raise InvalidSubmission(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class EvidenceReader:
    def __init__(self, context, config, game):
        self.context, self.config, self.game = context, config, game
        self.cache, self.sources, self.bytes_read = {}, {}, 0
        self.evidence = {e.evidence_id: e for e in context.state.evidence if e.game_revision_id == game.revision_id}
        self.integrity = {"sessions": 0, "frames": 0, "screenshots": 0, "replay_matches": 0, "replay_mismatches": 0}
        self.captures, self.comparisons = [], []
        self.previous_review_time = None
        self.snapshot_content = {}
        self.visual_refs = ()
        self.visual_source_ids = set()

    async def read(self, ref):
        check(ref in self.context.state.artifacts, "Unregistered QA input artifact")
        if ref.key not in self.cache:
            check(len(self.cache) < self.config.max_artifacts, "QA artifact read limit exceeded")
            raw = await self.context.tools.read_bytes(ref.path)
            self.bytes_read += len(raw)
            check(self.bytes_read <= self.config.max_input_bytes, "QA input byte limit exceeded")
            check(bool(ref.sha256) and sha(raw) == ref.sha256, "QA input checksum mismatch")
            self.cache[ref.key] = raw
        return self.cache[ref.key]

    def linked(self, ref, kind=None):
        return [e.evidence_id for e in self.evidence.values() if ref in e.artifact_refs and (kind is None or e.kind == kind)]

    def add(self, identity, kind, content, ref=None, evidence_ids=()):
        check(identity not in self.sources, "Duplicate QA source identity")
        if kind == "runtime_snapshot":
            import json
            key = sha(json.dumps({k: v for k, v in content.items() if k != "frame"}, sort_keys=True).encode())
            if key in self.snapshot_content:
                content = {"frame": content["frame"], "identical_snapshot_content_source_id": self.snapshot_content[key]}
            else:
                self.snapshot_content[key] = identity
        self.sources[identity] = {"source_id": identity, "kind": kind, "content": content,
            "artifact_ref": ref.model_dump(mode="json") if ref else None, "evidence_ids": list(evidence_ids)}

    async def report(self, identity, model, *, optional=False):
        # Resolve by embedded revision, not simply by the newest unrelated report.
        creator = "developer" if identity == "developer-report" else "player"
        refs = sorted((r for r in self.context.state.artifacts if r.artifact_id == identity and r.created_by == creator), key=lambda r: r.version, reverse=True)
        for ref in refs:
            report = model.model_validate_json(await self.read(ref))
            if report.game_revision_id == self.game.revision_id:
                check(report.project_ref == self.game.project_ref and report.task_id == self.context.state.task.task_id,
                      "QA report identity mismatch")
                return ref, report
        check(optional, "Current Developer handoff is required")
        return None, None

    async def session(self, session, player):
        log = strict_json(await self.read(session.log_ref))
        check(log["session_id"] == session.session_id and log["mode"] == session.mode and log["status"] == session.status,
              "Player session/log mismatch")
        linked = self.linked(session.log_ref)
        self.add("session:" + session.session_id, "session", {
            "session_id": session.session_id, "mode": session.mode, "scenario": session.scenario,
            "status": session.status, "reason": session.reason, "process": log["process"],
        }, session.log_ref, linked)
        if session.status != "passed":
            return
        files = {}
        refs = {}
        for item in log["runtime_files"]:
            ref = ArtifactRef.model_validate(item)
            name = PurePosixPath(ref.path).name
            check(name not in files, "Duplicate Player runtime filename")
            files[name], refs[name] = await self.read(ref), ref
        driver, receipt = strict_json(files["driver.json"]), strict_json(files["receipt.json"])
        check(driver == log["driver"] and log["process"]["returncode"] == 0 and driver["status"] == "passed",
              "Player process/driver did not succeed")
        check(driver["project_sha256"] == self.game.project_ref.sha256 and
              all(driver[k] is True for k in ("fresh_process", "fresh_user_data", "authored_files_unchanged", "original_project_unchanged"))
              and driver["network"] == "none", "Player recording isolation or project identity mismatch")
        check(receipt["harness_version"] == player.config.harness_version and
              (receipt["width"], receipt["height"], receipt["fps"]) == (1280, 720, 30), "Player recording protocol mismatch")
        frames = [strict_json(line) for line in files["frames.jsonl"].splitlines()]
        trace = InputTrace.model_validate(strict_json(files["executed_trace.json"]))
        events = [strict_json(line) for line in files["events.jsonl"].splitlines()]
        check(trace == session.executed_trace and trace.scenario == session.scenario and events == trace.wire()["events"]
              and receipt["event_count"] == len(events), "Player executed input mismatch")
        check(trace.duration_frames == session.completed_frames == receipt["completed_frames"] == len(frames)
              and [f["frame"] for f in frames] == list(range(len(frames))), "Player frame recording incomplete")
        check(tuple(f["state_sha256"] for f in frames) == session.state_digests and
              tuple(f["pixels_sha256"] for f in frames) == session.pixel_digests, "Player report/journal digest mismatch")
        check(all(re.fullmatch(r"[a-f0-9]{64}", f[k]) for f in frames for k in ("state_sha256", "pixels_sha256")),
              "Player frame journal contains an invalid digest")
        check(bool(session.observation_refs) and len(session.observation_refs) == len(session.screenshot_refs),
              "Player checkpoints are missing")
        check({PurePosixPath(r.path).stem for r in session.observation_refs} == {PurePosixPath(r.path).stem for r in session.screenshot_refs},
              "Player state/screenshot checkpoint frames differ")
        for ref in (*session.observation_refs, *session.screenshot_refs):
            check(refs.get(PurePosixPath(ref.path).name) == ref, "Player checkpoint not registered in its session")
        for ref in session.observation_refs:
            name = PurePosixPath(ref.path).name
            frame = int(name[6:10])
            check(0 <= frame < len(frames) and sha(files[name]) == frames[frame]["state_sha256"],
                  "Player state checkpoint/journal mismatch")
            snap = strict_json(files[name])
            # Deterministic projection of raw engine state, not Player's narrative.
            nodes = [n for n in snap["nodes"] if n.get("scripted") or n["class"] in
                     ("Button", "Label", "RichTextLabel", "ProgressBar", "CharacterBody2D", "AnimatedSprite2D")]
            self.add("snapshot:" + session.session_id + ":" + str(frame), "runtime_snapshot",
                {"frame": frame, "nodes": nodes[:100], "truncations": snap["truncations"] + max(0, len(nodes) - 100)}, ref, linked)
        for ref in session.screenshot_refs:
            name = PurePosixPath(ref.path).name
            frame = int(name[6:10])
            check(0 <= frame < len(frames), "Player screenshot frame outside journal")
            with Image.open(io.BytesIO(files[name])) as picture:
                check(picture.format == "PNG" and picture.size == (1280, 720), "Player screenshot format mismatch")
                check(sha(picture.convert("RGBA").tobytes()) == frames[frame]["pixels_sha256"], "Player pixel/journal mismatch")
            self.integrity["screenshots"] += 1
            if session.mode == "capture" and self.config.max_visual_images:
                self.add(f"image:{ref.artifact_id}:v{ref.version}", "screenshot",
                         {"session_id": session.session_id, "frame": frame,
                          "pixels_supplied_to_model": False}, ref, linked)
        self.add("trace:" + session.session_id, "trace", trace.wire(), refs["executed_trace.json"], linked)
        if session.mode == "capture":
            check(session.trace_ref is not None, "Capture has no exported trace")
            check(InputTrace.model_validate(strict_json(await self.read(session.trace_ref))) == trace, "Exported trace differs from executed input")
            self.captures.append(session)
        self.integrity["sessions"] += 1
        self.integrity["frames"] += len(frames)

    async def collect(self):
        state, game, tools = self.context.state, self.game, self.context.tools
        check(bool(game.project_ref.sha256) and await tools.fingerprint(game.project_ref.path) == game.project_ref.sha256,
              "Current game checksum mismatch")
        self.add("instruction", "specification", state.task.instruction)
        if state.design:
            design = strict_json(await self.read(state.design.document_ref))
            self.add("design", "specification", {k: design[k] for k in ("sections", "assumptions", "change_summary") if k in design}, state.design.document_ref)
        developer_ref, developer = await self.report("developer-report", DeveloperReport)
        self.add("developer-claims", "interpretation", {"summaries": developer.summaries, "limitations": developer.limitations,
            "controls": developer.controls, "scenarios": [s.model_dump(mode="json") for s in developer.scenarios]}, developer_ref)
        for index, record in enumerate(developer.mcp_inspections):
            result_ref = next((r for r in record.artifact_refs if r.path.endswith("/session/result.json")), None)
            check(result_ref is not None, "Developer MCP diagnostic has no result artifact")
            check(strict_json(await self.read(result_ref)) == record.result, "Developer MCP report/log mismatch")
            self.add(f"developer-mcp:{index}", "interpretation", {
                "current_project": record.project_sha256 == game.project_ref.sha256,
                "request": record.request.model_dump(mode="json"), "result": record.result,
                "visual_interpretation": record.visual_review, "acceptance_evidence": False,
                "meaning": "Development diagnostic only. Independently reproduce using Player or inspected current source."}, result_ref)
        build_ids = [i for i in game.build_evidence_ids if i in self.evidence and self.evidence[i].kind == "build_check"]
        build_verified = False
        for identity in build_ids:
            ev = self.evidence[identity]
            for ref in ev.artifact_refs:
                if ref.artifact_id == "developer-build-log":
                    attempts = strict_json(await self.read(ref))
                    check(attempts == [a.model_dump(mode="json") for a in developer.build_attempts], "Developer build report/log mismatch")
                    self.add("build:" + identity, "build_log", attempts, ref, (identity,))
                    build_verified = bool(attempts) and bool(attempts[-1]["steps"]) and attempts[-1]["status"] == "passed" and all(
                        s["status"] == "passed" and s["returncode"] == 0 for s in attempts[-1]["steps"])
        inventory, source_bytes, packaged = [], 0, []
        paths = await tools.list_files(game.project_ref.path)
        check(len(paths) <= self.config.max_project_files, "QA project file limit exceeded")
        for index, path in enumerate(paths):
            relative = path.removeprefix(game.project_ref.path + "/")
            inventory.append(relative)
            if relative == "project.godot" or PurePosixPath(relative).suffix in (".gd", ".tscn", ".tres", ".json", ".md", ".txt"):
                raw = await tools.read_bytes(path)
                source_bytes += len(raw)
                check(source_bytes <= self.config.max_source_bytes, "QA source text byte limit exceeded")
                self.add("file:" + str(index), "project_file", {"path": relative, "sha256": sha(raw),
                    "text": "\n".join(f"{i + 1}: {line}" for i, line in enumerate(raw.decode("utf-8").splitlines()))}, game.project_ref, build_ids)
                if relative.startswith("demo_outputs/") and relative.endswith(".json"):
                    packaged.append(raw)
        self.add("project-inventory", "inventory", inventory, game.project_ref, build_ids)
        mock_art = any(a.is_mock for a in developer.integrated_assets)
        if developer.art_manifest_ref:
            manifest = ArtManifest.model_validate_json(await self.read(developer.art_manifest_ref))
            mock_art = mock_art or any(a.is_mock for a in manifest.assets)
            self.add("art-manifest", "art_manifest", {"summary": manifest.summary, "limitations": manifest.limitations,
                "assets": [a.model_dump(mode="json", exclude={"request", "image_job"}) for a in manifest.assets]}, developer.art_manifest_ref, build_ids)
        player_ref, player = await self.report("player-report", PlayerReport, optional=True)
        if player:
            check(player.developer_report_ref == developer_ref, "Player references a different Developer report")
            check(len(player.sessions) <= self.config.max_sessions and len({s.session_id for s in player.sessions}) == len(player.sessions),
                  "Player session identity or count limit violation")
            sessions = {s.session_id: s for s in player.sessions}
            for session in player.sessions:
                await self.session(session, player)
            seen_pairs = set()
            for comparison in player.comparisons:
                capture, replay = sessions[comparison.capture_session_id], sessions[comparison.replay_session_id]
                pair = (capture.session_id, replay.session_id)
                check(pair not in seen_pairs, "Duplicate Player replay comparison")
                seen_pairs.add(pair)
                check(capture.mode == "capture" and replay.mode == "replay" and capture.trace_id == replay.trace_id and
                      capture.executed_trace == replay.executed_trace, "Replay does not execute its capture trace")
                actual = compare_sessions(capture, replay)
                check(actual == comparison, "Player replay verdict differs from actual frame journals")
                ids = [e.evidence_id for e in self.evidence.values() if e.kind == "replay" and
                       replay.log_ref in e.artifact_refs and capture.log_ref in e.artifact_refs]
                check(all(self.evidence[i].outcome == actual.status for i in ids), "Replay evidence outcome mismatch")
                self.add("comparison:" + replay.session_id, "replay_comparison", actual.model_dump(mode="json"), player_ref, ids)
                self.comparisons.append(actual)
                if actual.status in ("passed", "failed"):
                    self.integrity["replay_matches" if actual.status == "passed" else "replay_mismatches"] += 1
            self.add("player-interpretations", "interpretation", {"decisions": [d.model_dump(mode="json", exclude={"trials"}) for d in player.decisions],
                "limitations": player.limitations}, player_ref)
        packaged_ok = 1 <= len(packaged) <= 10
        for raw in packaged:
            try:
                trace = InputTrace.model_validate(strict_json(raw))
            except ValueError:
                packaged_ok = False
                continue
            matching = [s for s in self.captures if s.executed_trace == trace]
            packaged_ok = packaged_ok and any(
                len([c for c in self.comparisons if c.capture_session_id == s.session_id]) >= player.config.replay_runs and
                all(c.status == "passed" for c in self.comparisons if c.capture_session_id == s.session_id)
                for s in matching)
        self.gates = {"current_build_passed": build_verified and game.build_status == "passed",
            "current_play_evidence": bool(self.captures), "packaged_traces_replayed": bool(packaged_ok), "final_art": not mock_art}
        self.integrity.update(verified_artifacts=len(self.cache), verified_bytes=self.bytes_read,
            project_sha256=game.project_ref.sha256, screenshots_visually_inspected=False)
        if self.config.max_visual_images:
            from ..visual import select_screenshots
            self.visual_refs = select_screenshots(self.captures, self.config.max_visual_images)
            self.visual_source_ids = {f"image:{r.artifact_id}:v{r.version}" for r in self.visual_refs}
            for identity in self.visual_source_ids:
                self.sources[identity]["content"]["pixels_supplied_to_model"] = True
            self.integrity["screenshot_pixels_supplied"] = len(self.visual_refs)
            self.integrity["visual_review_mode"] = "model_image_input" if self.visual_refs else "text_only"
            if self.visual_refs:
                self.integrity.pop("screenshots_visually_inspected")
        return {"task": state.task.model_dump(mode="json"), "objective": self.context.request.objective,
            "completion_criteria": self.context.request.completion_criteria, "work_item_ids": self.context.request.work_item_ids,
            "game_revision": game.model_dump(mode="json"), "requirements": [r.model_dump(mode="json") for r in state.requirements],
            "issues": [i.model_dump(mode="json") for i in state.issues], "evidence": [e.model_dump(mode="json") for e in self.evidence.values()],
            "excluded_stale_evidence_ids": [e.evidence_id for e in state.evidence if e.evidence_id not in self.evidence],
            "allowed_criterion_evidence": {r.requirement_id: {kind: [e.evidence_id for e in self.evidence.values()
                if e.kind == kind and r.requirement_id in e.requirement_ids] for kind in ("build_check", "playthrough", "replay", "observation")}
                for r in state.requirements},
            "sources": list(self.sources.values()), "host_gates": self.gates, "evidence_integrity": self.integrity,
            "visual_source_ids": sorted(self.visual_source_ids)}
