"""Real input dispatch, captured traces and fresh replay; no game or QA mutations."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re

from PIL import Image
from pydantic import ValidationError

from ...models import ArtifactRef, Blocker, EvidenceRecord, ProposedUpdates, RoleResult
from ...runtime import InvalidSubmission, PermissionDenied, SandboxUnavailable
from ...runtime.errors import SandboxCleanupFailed
from ..developer import DeveloperReport
from ..developer.role import _constant, _object
from .contracts import InputTrace, PlayerConfig, PlayerDecision, PlayerReport, ReplayComparison, SessionRecord, normalize_trace
from .prompt import build_prompt


def strict_json(raw):
    return json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)


def compare_sessions(capture, replay):
    identity = dict(trace_id=capture.trace_id, capture_session_id=capture.session_id, replay_session_id=replay.session_id)
    if capture.status != "passed" or replay.status != "passed":
        return ReplayComparison(**identity, status="not_run", reason="A complete successful capture and replay are required for comparison")
    def first(a, b):
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return i
        return min(len(a), len(b)) if len(a) != len(b) else None
    state, pixels = first(capture.state_digests, replay.state_digests), first(capture.pixel_digests, replay.pixel_digests)
    passed = state is None and pixels is None
    return ReplayComparison(**identity, status="passed" if passed else "failed", state_match=state is None,
        pixel_match=pixels is None, first_state_difference=state, first_pixel_difference=pixels,
        reason="All recorded state and rendered-pixel digests match" if passed else "Fresh replay diverged; inspect first differing frames and actual screenshots")


class Player:
    def __init__(self, config=None):
        self.config = config or PlayerConfig()

    @staticmethod
    def _version(context, artifact_id):
        return max((r.version for r in context.state.artifacts if r.artifact_id == artifact_id), default=0) + 1

    @staticmethod
    def _result(context, status, summary, **fields):
        return RoleResult(role="player", request_id=context.request.request_id, state_revision=context.request.state_revision,
                          status=status, summary=summary, **fields)

    async def _read_ref(self, context, ref):
        if ref not in context.state.artifacts:
            raise InvalidSubmission("Unregistered Player input artifact")
        data = await context.tools.read_bytes(ref.path)
        if not ref.sha256 or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise InvalidSubmission("Player input artifact checksum mismatch")
        return data

    async def _inputs(self, context, game):
        refs = [r for r in context.state.artifacts if r.artifact_id == "developer-report" and r.created_by == "developer"]
        if not refs:
            raise InvalidSubmission("The current Developer report is required")
        ref = max(refs, key=lambda r: r.version)
        developer = DeveloperReport.model_validate_json(await self._read_ref(context, ref))
        if developer.game_revision_id != game.revision_id or developer.project_ref != game.project_ref:
            raise InvalidSubmission("Developer handoff does not describe the current game")
        if not game.project_ref.sha256 or await context.tools.fingerprint(game.project_ref.path) != game.project_ref.sha256:
            raise InvalidSubmission("Current game checksum mismatch")
        histories = [r for r in context.state.artifacts if r.artifact_id == "player-report" and r.created_by == "player"]
        if histories:
            previous = PlayerReport.model_validate_json(await self._read_ref(context, max(histories, key=lambda r: r.version)))
            if previous.config != self.config:
                raise InvalidSubmission("Player configuration is frozen within a run")
        selected = set(context.request.work_item_ids)
        issues = [i.model_dump(mode="json") for i in context.state.issues if i.issue_id in selected or
            bool(selected.intersection(i.requirement_ids)) or (not selected and i.status != "closed" and i.suggested_role == "player")]
        traces = []
        for source in context.request.artifact_refs:
            if source.kind == "input_trace":
                trace = InputTrace.model_validate(strict_json(await self._read_ref(context, source)))
                traces.append({"ref": source.model_dump(mode="json"), "trace": trace.wire(),
                    "prior_game_revisions": sorted({e.game_revision_id for e in context.state.evidence if source in e.artifact_refs})})
        return {"task": context.state.task.model_dump(mode="json"), "objective": context.request.objective,
            "completion_criteria": context.request.completion_criteria, "work_item_ids": context.request.work_item_ids,
            "game_revision": game.model_dump(mode="json"), "requirements": [r.model_dump(mode="json") for r in context.state.requirements],
            "developer": {"ref": ref.model_dump(mode="json"), "summaries": developer.summaries,
                "controls": developer.controls, "scenarios": [s.model_dump(mode="json") for s in developer.scenarios],
                "limitations": developer.limitations, "mock_art": any(a.is_mock for a in developer.integrated_assets),
                "inspection_hints": [{"request": r.request.model_dump(mode="json"), "result": r.result,
                    "visual_interpretation": r.visual_review, "formal_player_evidence": False}
                    for r in developer.mcp_inspections if r.project_sha256 == game.project_ref.sha256]},
            "issues": issues, "existing_traces": traces}, ref, developer

    async def _write(self, context, refs, path, artifact_id, value, *, kind="log", media="application/json"):
        data = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
        logical = await context.tools.write_bytes(path, data)
        ref = ArtifactRef(artifact_id=artifact_id, version=self._version(context, artifact_id), kind=kind, path=logical,
            created_by="player", media_type=media, sha256=hashlib.sha256(data).hexdigest())
        refs.append(ref)
        return ref

    async def _session(self, context, refs, game, trace, checkpoints, mode, session_id, trace_id=None):
        request_ref = await self._write(context, refs, f"requests/{session_id}.json", "player-job-" + session_id,
            {"harness_version": self.config.harness_version, "trace": trace.wire(), "checkpoints": checkpoints,
             "project_sha256": game.project_ref.sha256, "seed": self.config.seed})
        folder = f"sessions/{session_id}"
        argv = (self.config.driver_executable, self.config.driver_path, "--project", "/workspace/" + game.project_ref.path,
                "--request", "/output/" + request_ref.path.removeprefix(context.tools.output_prefix + "/"),
                "--output", "/output/" + folder)
        status, reason, process = "failed", "Session did not produce complete verified runtime evidence", None
        try:
            process = await context.tools.run(argv, timeout=self.config.session_timeout_seconds)
        except SandboxCleanupFailed:
            raise
        except SandboxUnavailable as exc:
            status, reason = "not_run", str(exc)
        except asyncio.TimeoutError:
            reason = "Real play session exceeded its deadline; partial output is not a complete trace"
        raw_files, file_refs, screenshots, observation_refs, observations = {}, {}, [], [], []
        names = await context.tools.list_files(context.tools.output_path(folder))
        for logical in names:
            if not logical.startswith(context.tools.output_path(folder) + "/"):
                continue
            name = logical.rsplit("/", 1)[-1]
            if not re.fullmatch(r"(?:driver\.json|receipt\.json|executed_trace\.json|frames\.jsonl|events\.jsonl|frame_\d{4}\.(?:png|json))", name):
                raise InvalidSubmission("Unexpected file produced by Player runtime")
            data = await context.tools.read_bytes(logical)
            raw_files[name] = data
            kind = "screenshot" if name.endswith(".png") else "log"
            media = "image/png" if kind == "screenshot" else "application/x-ndjson" if name.endswith(".jsonl") else "application/json"
            if kind == "screenshot":
                with Image.open(io.BytesIO(data)) as picture:
                    picture.load()
                    if picture.format != "PNG" or picture.size != (1280, 720):
                        raise InvalidSubmission("Runtime screenshot has an invalid format or viewport size")
            ref = ArtifactRef(artifact_id="player-" + session_id + "-" + name.replace(".", "-"),
                version=self._version(context, "player-" + session_id + "-" + name.replace(".", "-")), kind=kind,
                path=logical, created_by="player", media_type=media, sha256=hashlib.sha256(data).hexdigest())
            refs.append(ref)
            file_refs[name] = ref
            if kind == "screenshot":
                screenshots.append(ref)
            if re.fullmatch(r"frame_\d{4}\.json", name):
                observation_refs.append(ref)
                snap = strict_json(data)
                nodes = [n for n in snap["nodes"] if n.get("scripted") or n["class"] in
                         ("Button", "Label", "RichTextLabel", "ProgressBar", "CharacterBody2D", "AnimatedSprite2D")]
                observations.append({"frame": int(name[6:10]), "nodes": nodes[:100],
                    "truncations": snap["truncations"] + max(0, len(nodes) - 100)})
        state_hashes, pixel_hashes, completed, actual_trace = (), (), 0, None
        validation_error = None
        if process is not None:
            try:
                driver = strict_json(raw_files["driver.json"])
                receipt = strict_json(raw_files["receipt.json"])
                frames = [strict_json(line) for line in raw_files["frames.jsonl"].splitlines()]
                events = [strict_json(line) for line in raw_files["events.jsonl"].splitlines()]
                actual_trace = InputTrace.model_validate(strict_json(raw_files["executed_trace.json"]))
                if process.returncode != 0 or driver["status"] != "passed" or not driver["authored_files_unchanged"] or not driver["original_project_unchanged"]:
                    raise ValueError("Engine failed or modified authored source files")
                if driver["project_sha256"] != game.project_ref.sha256 or receipt["harness_version"] != self.config.harness_version:
                    raise ValueError("Runtime identity mismatch")
                if (receipt["width"], receipt["height"], receipt["fps"]) != (1280, 720, 30):
                    raise ValueError("Runtime viewport or frame rate mismatch")
                if receipt["completed_frames"] != trace.duration_frames or len(frames) != trace.duration_frames:
                    raise ValueError("Incomplete frame recording")
                if [f["frame"] for f in frames] != list(range(trace.duration_frames)):
                    raise ValueError("Missing or reordered recorded frames")
                if actual_trace != trace or events != trace.wire()["events"] or receipt["event_count"] != len(events):
                    raise ValueError("Executed events differ from the requested canonical input trace")
                for f in frames:
                    if not all(re.fullmatch(r"[a-f0-9]{64}", f[key]) for key in ("state_sha256", "pixels_sha256")):
                        raise ValueError("Invalid runtime digest")
                for checkpoint in checkpoints:
                    stem = f"frame_{checkpoint:04}"
                    if hashlib.sha256(raw_files[stem + ".json"]).hexdigest() != frames[checkpoint]["state_sha256"]:
                        raise ValueError("Observed state checksum differs from its frame journal")
                    with Image.open(io.BytesIO(raw_files[stem + ".png"])) as picture:
                        pixels = picture.convert("RGBA").tobytes()
                    if hashlib.sha256(pixels).hexdigest() != frames[checkpoint]["pixels_sha256"]:
                        raise ValueError("Screenshot pixels differ from their frame journal")
                completed = trace.duration_frames
                state_hashes, pixel_hashes = tuple(f["state_sha256"] for f in frames), tuple(f["pixels_sha256"] for f in frames)
                status, reason = "passed", "Real frame-indexed input capture completed; gameplay objectives and QA acceptance are separate"
            except (KeyError, ValueError, TypeError, ValidationError) as exc:
                validation_error = str(exc)[:1000]
                reason = "Runtime recording validation failed: " + validation_error
        log = {"session_id": session_id, "mode": mode, "argv": argv, "status": status, "reason": reason,
            "process": {"returncode": process.returncode, "stdout": process.stdout, "stderr": process.stderr} if process else None,
            "driver": strict_json(raw_files["driver.json"]) if "driver.json" in raw_files else None,
            "runtime_files": [r.model_dump(mode="json") for r in file_refs.values()]}
        log_ref = await self._write(context, refs, f"logs/{session_id}.json", "player-session-" + session_id, log)
        trace_ref = None
        if mode == "capture" and status == "passed":
            trace_ref = await self._write(context, refs, f"demo_outputs/{trace_id}.json", "player-trace-" + trace_id,
                                          actual_trace.wire(), kind="input_trace")
        return SessionRecord(session_id=session_id, mode=mode, trace_id=trace_id, scenario=trace.scenario,
            status=status, reason=reason, completed_frames=completed, log_ref=log_ref, trace_ref=trace_ref,
            executed_trace=actual_trace if status == "passed" else None,
            screenshot_refs=tuple(screenshots), observation_refs=tuple(observation_refs), observations=tuple(observations),
            state_digests=state_hashes, pixel_digests=pixel_hashes)

    async def __call__(self, context):
        if context.request.role != "player":
            raise PermissionDenied("Player requires a player request")
        game = next((g for g in context.state.game_revisions if g.revision_id == context.state.current_game_revision_id), None)
        if game is None:
            return self._result(context, "blocked", "A Developer game revision is required",
                blockers=(Blocker(description="Provide a registered executable game project and Developer handoff"),))
        inputs, developer_ref, developer = await self._inputs(context, game)
        refs, sessions, comparisons, decisions, errors, used, bindings = [], [], [], [], [], set(), {}
        known_requirements = {r.requirement_id for r in context.state.requirements}
        scenarios = {s.scenario_id for s in developer.scenarios}
        probe = InputTrace(duration_frames=self.config.probe_frames)
        sessions.append(await self._session(context, refs, game, probe, (0, probe.duration_frames - 1), "probe", "probe"))
        async def play_trial(trace_id, trace, checkpoints, requirement_ids):
            used.add(trace_id)
            bindings[trace_id] = requirement_ids
            capture = await self._session(context, refs, game, trace, checkpoints, "capture", trace_id + "-capture", trace_id)
            sessions.append(capture)
            if capture.status != "passed":
                return
            captured = InputTrace.model_validate(strict_json(await context.tools.read_bytes(capture.trace_ref.path)))
            for replay_index in range(self.config.replay_runs):
                replay = await self._session(context, refs, game, captured, checkpoints, "replay",
                    f"{trace_id}-replay-{replay_index + 1}", trace_id)
                sessions.append(replay)
                comparisons.append(compare_sessions(capture, replay))

        finished = False
        if sessions[-1].status == "passed":
            if self.config.prompt_version == "player.v2":
                # The exact packaged inputs get priority over model exploration.
                # All sessions consume the same max_traces and shared tool budget.
                paths = [p for p in await context.tools.list_files(game.project_ref.path)
                         if p.startswith(game.project_ref.path + "/demo_outputs/")]
                records = {p.target_path: p for p in developer.packaged_traces}
                if len(records) != len(developer.packaged_traces) or len(paths) > self.config.max_traces:
                    raise InvalidSubmission("Packaged demo count exceeds the fixed Player trial budget")
                if {p.removeprefix(game.project_ref.path + "/") for p in paths} != set(records):
                    raise InvalidSubmission("Packaged demos disagree with Developer capture provenance")
                for index, path in enumerate(sorted(paths)):
                    record = records[path.removeprefix(game.project_ref.path + "/")]
                    raw = await context.tools.read_bytes(path)
                    if raw != await self._read_ref(context, record.trace_ref):
                        raise InvalidSubmission("Packaged demo differs from the original Player capture")
                    trace = InputTrace.model_validate(strict_json(raw))
                    if normalize_trace(trace) != trace or (trace.scenario and trace.scenario not in scenarios):
                        raise InvalidSubmission("Packaged demo is not canonical or uses an unsupported scenario")
                    if set(record.requirement_ids) - known_requirements:
                        raise InvalidSubmission("Packaged demo references unknown requirements")
                    checkpoints = tuple(sorted({0, trace.duration_frames - 1}))
                    await play_trial(f"packaged_{index + 1:02}", trace, checkpoints, record.requirement_ids)
            for turn in range(self.config.max_model_turns):
                from ..visual import select_screenshots
                visual_refs = select_screenshots(sessions, self.config.max_visual_images)
                prompt = build_prompt(inputs, self.config, sessions, comparisons, self.config.max_model_turns - turn,
                                      self.config.max_traces - len(used), errors, visual_refs=visual_refs)
                if len(prompt.encode()) > self.config.max_prompt_bytes:
                    raise InvalidSubmission("Player observation prompt exceeds its byte limit")
                raw = await context.tools.generate_text(prompt, max_output_tokens=self.config.max_output_tokens,
                                                       image_refs=visual_refs)
                if len(raw.encode()) > self.config.max_response_bytes:
                    raise InvalidSubmission("Player response exceeds its byte limit")
                try:
                    decision = PlayerDecision.model_validate(strict_json(raw))
                    if len(used) + len(decision.trials) > self.config.max_traces:
                        raise ValueError("Trial budget exceeded; finish with limitations")
                    session_ids = {s.session_id for s in sessions}
                    for finding_index, finding in enumerate(decision.findings):
                        if set(finding.session_ids) - session_ids or set(finding.requirement_ids) - known_requirements:
                            raise ValueError(f"Finding {finding_index} ({finding.category}) references unknown evidence or requirements")
                        if finding.category != "unexplored" and not finding.session_ids:
                            raise ValueError(
                                f"Finding {finding_index} ({finding.category}) has empty session_ids. "
                                "Cite an actual session from runtime_observations that supports this finding, "
                                "including claims about visible mock-art labels. If the claim was not observed, "
                                "use category=unexplored and describe the uncertainty instead. "
                                "No trial in this rejected response was executed."
                            )
                    prepared = []
                    checkpoint_errors = []
                    for plan in decision.trials:
                        if plan.trace_id in used or set(plan.requirement_ids) - known_requirements:
                            raise ValueError("Reused trace ID or unknown requirement")
                        trace = plan.trace or InputTrace.model_validate(strict_json(await self._read_ref(context, plan.source_trace_ref)))
                        trace = normalize_trace(trace)
                        if trace.scenario and trace.scenario not in scenarios:
                            raise ValueError("Unsupported scenario; request Developer support instead")
                        if not any(e.type != "wait" for e in trace.events):
                            raise ValueError("A play trial must include real keyboard or mouse input")
                        checkpoints = tuple(sorted({0, trace.duration_frames - 1, *plan.checkpoint_frames}))
                        if len(plan.checkpoint_frames) > self.config.max_checkpoints - 2 or checkpoints[-1] >= trace.duration_frames:
                            checkpoint_errors.append(f"Trial {plan.trace_id}: checkpoint_frames={list(plan.checkpoint_frames)}; "
                                f"provide at most {self.config.max_checkpoints - 2} additional frames, each below {trace.duration_frames}. "
                                "The host adds frame 0 and the final frame automatically.")
                        prepared.append((plan, trace, checkpoints))
                    if checkpoint_errors:
                        raise ValueError(" ".join(checkpoint_errors))
                except (ValueError, ValidationError) as exc:
                    errors.append({"model_turn": turn + 1, "error": str(exc)[:1500], "rejected_response": raw})
                    continue
                decisions.append(decision)
                if decision.action in ("finish", "blocked"):
                    finished = decision.action == "finish"
                    break
                for plan, trace, checkpoints in prepared:
                    await play_trial(plan.trace_id, trace, checkpoints, plan.requirement_ids)
        if await context.tools.fingerprint(game.project_ref.path) != game.project_ref.sha256:
            raise InvalidSubmission("Player must not modify the published game")
        # Detect alteration of previous captures/jobs by a later session as well.
        for ref in refs:
            if await context.tools.fingerprint(ref.path) != ref.sha256:
                raise InvalidSubmission("Player runtime modified an earlier artifact")
        version = self._version(context, "player-report")
        limitations = ["This is a custom public trace harness, not the hidden official evaluator or a QA verdict.",
            "Determinism means matching bounded observed state and rendered pixels at every recorded frame on this fixed engine/image; hidden state, other seeds and other platforms are not proved.",
            ("Selected capture screenshots were supplied as image input. Unseen scenes and frames are not visually verified."
             if self.config.max_visual_images else
             "The text model received actual scene observations and screenshot references, but did not visually inspect images."),
            "Godot wall-clock time is not rewritten; wall-clock animations can legitimately fail exact visual replay.",
            "The published Developer project and its assets are unchanged. New captures can be assembled by Developer in a later cycle; the assembled revision requires fresh checks."]
        if not finished:
            limitations.append("A final model observation decision was not reached within the configured resources.")
        report = PlayerReport(config=self.config, task_id=context.state.task.task_id, game_revision_id=game.revision_id,
            project_ref=game.project_ref, developer_report_ref=developer_ref, sessions=tuple(sessions), comparisons=tuple(comparisons),
            decisions=tuple(decisions), protocol_errors=tuple(errors), limitations=tuple(limitations))
        report_ref = await self._write(context, refs, "player_report.json", "player-report", report.model_dump(mode="json"), kind="report")
        await self._write(context, refs, "player_handoff.md", "player-handoff", self._render(report).encode(), kind="report", media="text/markdown")
        evidence = []
        for index, session in enumerate(sessions):
            if session.mode == "capture" and session.trace_ref:
                evidence.append(EvidenceRecord(evidence_id=f"player-{version:04}-play-{index:02}", kind="playthrough",
                    game_revision_id=game.revision_id, requirement_ids=bindings[session.trace_id], scenario=session.scenario,
                    artifact_refs=(session.trace_ref, session.log_ref, *session.screenshot_refs), outcome="passed",
                    observation="Real input dispatch and frame capture completed; this outcome describes execution, not satisfaction of gameplay requirements."))
        for index, comparison in enumerate(comparisons):
            capture = next(s for s in sessions if s.session_id == comparison.capture_session_id)
            replay = next(s for s in sessions if s.session_id == comparison.replay_session_id)
            evidence.append(EvidenceRecord(evidence_id=f"player-{version:04}-replay-{index:02}", kind="replay",
                game_revision_id=game.revision_id, requirement_ids=bindings[comparison.trace_id], scenario=capture.scenario,
                artifact_refs=(capture.trace_ref, capture.log_ref, replay.log_ref, report_ref), outcome=comparison.status,
                observation=comparison.reason))
        evidence.append(EvidenceRecord(evidence_id=f"player-{version:04}-observations", kind="observation",
            game_revision_id=game.revision_id, artifact_refs=(report_ref,), outcome="passed" if any(s.status == "passed" for s in sessions) else "not_run",
            observation="Recorded runtime diagnostics and explicitly attributed model interpretations; no requirement or readiness verdict was made."))
        complete = finished and bool(used) and all(s.status == "passed" for s in sessions) and bool(comparisons) and all(c.status == "passed" for c in comparisons)
        return self._result(context, "completed" if complete else "partial",
            f"Captured {sum(s.trace_ref is not None for s in sessions)} real input traces and checked {len(comparisons)} fresh replays; see original diagnostics and model findings.",
            artifact_refs=tuple(refs), evidence_refs=tuple(e.evidence_id for e in evidence),
            proposed_updates=ProposedUpdates(evidence=tuple(evidence)))

    @staticmethod
    def _render(report):
        lines = ["# Player handoff", "", f"Game revision: `{report.game_revision_id}`. The game project was not modified.", "",
            "## Sessions", "", "| Session | Mode | Execution | Frames |", "| --- | --- | --- | ---: |"]
        lines.extend(f"| {s.session_id} | {s.mode} | {s.status} | {s.completed_frames} |" for s in report.sessions)
        lines.extend(["", "## Fresh replay", "", "| Trace | Replay | State match | Pixels match | Result |", "| --- | --- | --- | --- | --- |"])
        lines.extend(f"| {c.trace_id} | {c.replay_session_id} | {c.state_match} | {c.pixel_match} | {c.status} |" for c in report.comparisons)
        lines.extend(["", "## Model observations (not QA verdicts)", ""])
        for decision in report.decisions:
            lines.append("- " + decision.summary)
            lines.extend(f"- {f.category}: {f.description} Sessions: {', '.join(f.session_ids) or 'none'}. Uncertainty: {f.uncertainty}" for f in decision.findings)
            lines.extend("- " + item for item in decision.limitations)
        lines.extend(["", "## Limits and next steps", "", *["- " + item for item in report.limitations], ""])
        return "\n".join(lines)
