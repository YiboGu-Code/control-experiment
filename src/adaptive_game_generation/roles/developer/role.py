"""Validated source patches, protected Art integration, and bounded real checks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

from pydantic import ValidationError

from ...models import ArtifactRef, Blocker, EvidenceRecord, GameRevision, ProposedUpdates, RoleResult
from ...runtime import InvalidSubmission, PermissionDenied, RoleContext, SandboxUnavailable
from ...runtime.errors import SandboxCleanupFailed
from ..art_producer import ArtManifest
from .contracts import (
    BuildAttempt, BuildStep, DeveloperConfig, DeveloperProposal, DeveloperReport,
    IntegratedAsset, ProposalRejection, project_path,
)
from .prompt import build_prompt
from .diagnostics import ENGINE_FAILURE, source_findings
from .delivery import captured_demos


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _constant(_):
    raise ValueError("Nonfinite JSON number")


class Developer:
    def __init__(self, config: DeveloperConfig | None = None):
        self.config = config or DeveloperConfig()

    @staticmethod
    def _result(context, status, summary, **fields):
        return RoleResult(request_id=context.request.request_id, role="developer",
            state_revision=context.request.state_revision, status=status, summary=summary, **fields)

    @staticmethod
    def _version(context, artifact_id):
        return max((r.version for r in context.state.artifacts if r.artifact_id == artifact_id), default=0) + 1

    async def _read_ref(self, context, ref):
        if ref not in context.state.artifacts:
            raise InvalidSubmission("Developer input references an unregistered artifact")
        data = await context.tools.read_bytes(ref.path)
        if ref.sha256 and hashlib.sha256(data).hexdigest() != ref.sha256:
            raise InvalidSubmission("Developer input artifact checksum mismatch")
        return data

    async def _sources(self, tools, base):
        files = {}
        if base is None:
            return files
        total = 0
        for path in await tools.list_files(base):
            relative = path[len(base) + 1:]
            try:
                project_path(relative)
            except (ValueError, PermissionDenied):
                continue
            data = await tools.read_bytes(path)
            total += len(data)
            if len(data) > self.config.max_file_bytes or total > self.config.max_source_bytes:
                raise InvalidSubmission("Current project source exceeds Developer context limits")
            files[relative] = data.decode("utf-8")
        return files

    async def _inputs(self, context):
        state, request, tools = context.state, context.request, context.tools
        design = (await self._read_ref(context, state.design.document_ref)).decode("utf-8")
        registry = {r.key: r for r in state.artifacts}
        art_ref, art = None, None
        refs = [r for r in state.artifacts if r.artifact_id == "art-manifest" and r.created_by == "art_producer"]
        if refs:
            art_ref = max(refs, key=lambda r: r.version)
            art = ArtManifest.model_validate_json(await self._read_ref(context, art_ref))
            records = {a.asset_id: a for a in state.assets}
            if art.task_id != state.task.task_id or {a.record.asset_id for a in art.assets} != set(records):
                raise InvalidSubmission("Art manifest does not cover the current task assets")
            if any(records[a.record.asset_id] != a.record for a in art.assets):
                raise InvalidSubmission("Art manifest disagrees with current shared assets")
        elif state.assets:
            raise InvalidSubmission("Asset integration requires the Art Producer manifest")
        copies, integrated = {}, []
        if art:
            for item in art.assets:
                target = item.request.target_path
                if target.casefold() in {p.casefold() for p in copies}:
                    raise InvalidSubmission("Art targets collide")
                copies[target] = await self._read_ref(context, item.record.artifact_ref)
                license_target = None
                if item.license_ref:
                    if registry.get(item.license_ref.key) != item.license_ref:
                        raise InvalidSubmission("Unregistered Art license reference")
                    license_target = f"licenses/{item.record.asset_id}.txt"
                    copies[license_target] = await self._read_ref(context, item.license_ref)
                integrated.append(IntegratedAsset(asset_id=item.record.asset_id, artifact_ref=item.record.artifact_ref,
                    target_path=target, is_mock=item.is_mock, license_target=license_target))
        current = next((g for g in state.game_revisions if g.revision_id == state.current_game_revision_id), None)
        prior_reports = [r for r in state.artifacts if r.artifact_id == "developer-report" and r.created_by == "developer"]
        prior = None
        if prior_reports:
            ref = max(prior_reports, key=lambda r: r.version)
            prior = DeveloperReport.model_validate_json(await self._read_ref(context, ref))
            if prior.config != self.config or prior.task_id != state.task.task_id or not current or prior.game_revision_id != current.revision_id:
                raise InvalidSubmission("Developer history or frozen configuration mismatch")
        base = current.project_ref.path if current else None
        if current:
            if current.project_ref.sha256 and await tools.fingerprint(base) != current.project_ref.sha256:
                raise InvalidSubmission("Current project checksum mismatch")
        else:
            if any(path.startswith("input/starter/") for path in await tools.list_files("input")):
                base = "input/starter"
        demos, packaged, assembly_required = {}, (), False
        if self.config.prompt_version in ("developer.v2", "developer.v3", "developer.v4"):
            demos, packaged, assembly_required = await captured_demos(self, context, current, prior)
            copies.update(demos)
        files = await self._sources(tools, base)
        selected = set(request.work_item_ids)
        issues = [i.model_dump(mode="json") for i in state.issues if i.issue_id in selected or
            bool(selected.intersection(i.requirement_ids)) or (not selected and i.status != "closed" and i.suggested_role == "developer")]
        evidence_ids = {eid for issue in issues for eid in issue["evidence_ids"]}
        evidence = [e.model_dump(mode="json") for e in state.evidence if e.evidence_id in evidence_ids]
        attachments = []
        for ref in request.artifact_refs:
            item = {"ref": ref.model_dump(mode="json"), "visually_inspected": False}
            if ref.kind in ("document", "report", "log") and ref.media_type in ("text/plain", "text/markdown", "application/json"):
                item["text"] = (await self._read_ref(context, ref)).decode("utf-8")
            attachments.append(item)
        inputs = {"task": state.task.model_dump(mode="json"), "objective": request.objective,
            "completion_criteria": request.completion_criteria, "work_item_ids": request.work_item_ids,
            "requirements": [r.model_dump(mode="json") for r in state.requirements], "design": design,
            "art_manifest": art.model_dump(mode="json") if art else None,
            "integrated_assets": [a.model_dump(mode="json") for a in integrated],
            "current_game_revision": current.model_dump(mode="json") if current else None,
            "previous_report": prior.model_dump(mode="json") if prior else None,
            "captured_demo_assembly": {"required": assembly_required,
                "traces": [p.model_dump(mode="json") for p in packaged],
                "meaning": "Host copies actual Player inputs. These must be replayed on the NEW project before QA can accept them."},
            "issues": issues, "evidence": evidence, "attachments": attachments,
            "execution_paths": {"staged_project": "/output/game", "delivery_project": "/workspace/game",
                "meaning": "Host mount mapping only; use res:// paths in game code. The original instruction is unchanged."}}
        return inputs, files, base, copies, tuple(integrated), art_ref, packaged

    def _validate_patch(self, context, plan, files, protected):
        if len(plan.files) + len(plan.delete_paths) > self.config.max_patch_files:
            raise InvalidSubmission("Developer patch exceeds the file count limit")
        known = {r.requirement_id for r in context.state.requirements}
        if set(plan.affected_requirement_ids) - known or set(plan.implemented_requirement_ids) - known:
            raise InvalidSubmission("Developer proposal references unknown requirements")
        updated = dict(files)
        for path in plan.delete_paths:
            if path not in updated:
                raise InvalidSubmission("Cannot delete a source file that does not exist")
            del updated[path]
        old_paths = {path.casefold(): path for path in files}
        for file in plan.files:
            if len(file.content.encode("utf-8")) > self.config.max_file_bytes or "\x00" in file.content:
                raise InvalidSubmission("Oversized or binary project source")
            if file.path.casefold() in old_paths and old_paths[file.path.casefold()] != file.path:
                raise InvalidSubmission("Case-only source renames are not portable")
            updated[file.path] = file.content
        if sum(len(v.encode("utf-8")) for v in updated.values()) > self.config.max_source_bytes:
            raise InvalidSubmission("Developer project exceeds the source byte limit")
        paths = [p.casefold() for p in (*updated, *protected)]
        if any(a == b or a.startswith(b + "/") or b.startswith(a + "/") for index, a in enumerate(paths) for b in paths[index + 1:]):
            raise InvalidSubmission("Project files collide with each other or protected Art targets")
        if not updated.get("project.godot") or not updated.get("Main.tscn"):
            raise InvalidSubmission("Game must contain project.godot and Main.tscn")
        if not re.search(r'(?m)^run/main_scene\s*=\s*"res://Main\.tscn"\s*$', updated["project.godot"]):
            raise InvalidSubmission("Project must launch the root Main.tscn scene")
        return updated

    async def _check(self, context, index, plan, expected, model_turn):
        tools = context.tools
        commands = [("engine_version", (self.config.godot_executable, "--headless", "--version")),
                    ("import", (self.config.godot_executable, "--headless", "--path", "/output/game", "--import")),
                    ("ordinary_launch", (self.config.godot_executable, "--headless", "--path", "/output/game", "--quit-after", "5"))]
        commands.extend(("scenario:" + scenario.scenario_id, (self.config.godot_executable, "--headless", "--path", "/output/game",
            "--fixed-fps", "60", "--quit-after", str(self.config.scenario_frames), "--", "--scenario", scenario.scenario_id))
            for scenario in plan.scenarios[:self.config.max_scenario_checks])
        digest = await tools.fingerprint(tools.output_path("game"))
        steps = []
        for name, argv in commands:
            try:
                result = await tools.run(argv, timeout=self.config.check_timeout_seconds)
            except SandboxCleanupFailed:
                raise
            except SandboxUnavailable as exc:
                steps.append(BuildStep(name=name, argv=argv, status="not_run", reason=str(exc)))
                break
            except asyncio.TimeoutError:
                steps.append(BuildStep(name=name, argv=argv, status="failed", reason="Isolated engine check timed out"))
                break
            # Some Godot parse/script failures are printed despite exit status 0.
            errors = re.search(r"(?mi)^\s*(?:SCRIPT ERROR:|ERROR:|Parse Error:|Shader compilation failed)", result.stdout + "\n" + result.stderr)
            if self.config.prompt_version in ("developer.v3", "developer.v4"):
                errors = ENGINE_FAILURE.search(result.stdout + "\n" + result.stderr)
            passed = result.returncode == 0 and errors is None
            if name == "engine_version" and not re.search(r"(?m)^4\.", result.stdout):
                passed = False
            steps.append(BuildStep(name=name, argv=argv, status="passed" if passed else "failed",
                returncode=result.returncode, stdout=result.stdout, stderr=result.stderr))
            if not passed:
                break
        actual = await self._sources(tools, tools.output_path("game"))
        changed = [path for path, text in expected.items() if actual.get(path) != text]
        unexpected = [path for path in actual if path not in expected and not path.endswith(".uid")]
        if changed or unexpected:
            steps.append(BuildStep(name="source_integrity", argv=("host-source-integrity-check",), status="failed",
                reason="Engine execution modified project sources: " + ", ".join(sorted(set(changed + unexpected)))))
        status = "failed" if any(s.status == "failed" for s in steps) else "not_run" if any(s.status == "not_run" for s in steps) else "passed"
        return BuildAttempt(attempt=index, model_turn=model_turn, source_digest=digest, status=status, steps=tuple(steps)), actual

    async def __call__(self, context: RoleContext):
        if context.request.role != "developer":
            raise PermissionDenied("Developer requires a developer request")
        if context.state.design is None or not context.state.requirements:
            return self._result(context, "blocked", "A grounded Designer specification is required",
                blockers=(Blocker(description="Provide the public Designer document and requirements"),))
        inputs, files, base, copies, integrated, art_ref, packaged = await self._inputs(context)
        attempts, plans, changed, deleted = [], [], set(), set()
        rejections, rejected = [], None
        staged = False
        from .inspection import DECISION, InspectionCycle
        inspection = InspectionCycle(self, context) if self.config.prompt_version == "developer.v4" else None
        for turn in range(self.config.max_model_turns):
            prompt = build_prompt(inputs, self.config, files, attempts, self.config.max_model_turns - turn, rejections, rejected,
                                  inspection_feedback=inspection.feedback() if inspection else None)
            if len(prompt.encode("utf-8")) > self.config.max_prompt_bytes:
                raise InvalidSubmission("Developer prompt exceeds the byte limit")
            raw = await context.tools.generate_text(prompt, max_output_tokens=self.config.max_output_tokens)
            if len(raw.encode("utf-8")) > self.config.max_response_bytes:
                raise InvalidSubmission("Developer response exceeds the byte limit")
            try:
                parsed = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
                plan = DECISION.validate_python(parsed) if inspection else DeveloperProposal.model_validate(parsed)
                if inspection and plan.status in ("inspect", "finish"):
                    if not staged or not attempts or attempts[-1].status != "passed":
                        raise InvalidSubmission("Submit a source patch with a passing build before inspection or finish")
                    if plan.status == "finish":
                        await inspection.finish(plan)
                        break
                    await inspection.inspect(plan.inspection, turn + 1, files, plans[-1].scenarios)
                    rejected = None
                    continue
            except (ValueError, ValidationError, InvalidSubmission) as exc:
                errors = tuple(
                    ".".join(map(str, e["loc"])) + ": " + e["msg"]
                    for e in exc.errors(include_input=False, include_url=False)
                ) if isinstance(exc, ValidationError) else (str(exc),)
                rejections.append(ProposalRejection(model_turn=turn + 1, errors=errors))
                if turn + 1 == self.config.max_model_turns:
                    raise InvalidSubmission("Developer proposal validation budget exhausted: " + "; ".join(errors)[:1200]) from None
                rejected = raw
                continue
            rejected = None
            if plan.status in ("no_op", "blocked"):
                if plan.status == "no_op" and inputs["captured_demo_assembly"]["required"]:
                    rejections.append(ProposalRejection(model_turn=turn + 1, errors=(
                        "Captured demo assembly is pending. Return a patch (the unchanged project.godot is sufficient if no code repair is needed), current controls/scenarios and affected requirements.",)))
                    if turn + 1 == self.config.max_model_turns:
                        raise InvalidSubmission("Developer did not assemble pending captured demos within its model budget")
                    rejected = raw
                    continue
                if staged or (plan.status == "no_op" and context.state.current_game_revision_id is None):
                    raise InvalidSubmission("Cannot abandon a staged repair or no_op without an existing game")
                known = {r.requirement_id for r in context.state.requirements} | {i.issue_id for i in context.state.issues}
                if any(set(b.dependency_ids) - known for b in plan.blockers):
                    raise InvalidSubmission("Developer blocker references unknown dependencies")
                return self._result(context, plan.status, plan.summary, blockers=plan.blockers)
            updated = self._validate_patch(context, plan, files, copies)
            if self.config.prompt_version in ("developer.v3", "developer.v4"):
                findings = [f for f in source_findings(updated) if f["severity"] == "error"]
                if findings:
                    errors = tuple(f'{f["path"]}:{f["line"]} [{f["code"]}]: {f["message"]}' for f in findings)
                    rejections.append(ProposalRejection(model_turn=turn + 1, errors=errors))
                    if turn + 1 == self.config.max_model_turns:
                        raise InvalidSubmission("Developer source validation budget exhausted: " + "; ".join(errors)[:1200])
                    rejected = raw
                    continue
            if not staged:
                if base:
                    await context.tools.copy(base, "game")
                if self.config.prompt_version in ("developer.v2", "developer.v3", "developer.v4") and base:
                    if any(p.startswith(base + "/demo_outputs/") for p in await context.tools.list_files(base)):
                        await context.tools.remove_output("game/demo_outputs")
                for target, data in copies.items():
                    await context.tools.write_bytes("game/" + target, data)
                changed.update(p.target_path for p in packaged)
                staged = True
            for path in plan.delete_paths:
                await context.tools.remove_output("game/" + path)
                deleted.add(path)
                changed.discard(path)
            for file in plan.files:
                await context.tools.write_text("game/" + file.path, file.content)
                changed.add(file.path)
                deleted.discard(file.path)
            plans.append(plan)
            attempt, files = await self._check(context, len(attempts) + 1, plan, updated, turn + 1)
            attempts.append(attempt)
            inputs["current_delivery_contract"] = plan.model_dump(mode="json", exclude={"files", "delete_paths"})
            if inspection and attempt.status == "passed":
                # Preview the exact authored package that can be published, not
                # a cache-bearing intermediate identity that changes at commit.
                if any("/game/.godot/" in p for p in await context.tools.list_files(context.tools.output_path("game"))):
                    await context.tools.remove_output("game/.godot")
                if len(inspection.records) < self.config.max_mcp_inspections:
                    from ...runtime.godot_mcp import GodotInspectionRequest
                    await inspection.inspect(GodotInspectionRequest(
                        scenario=plan.scenarios[0].scenario_id if plan.scenarios else None), turn + 1, files, plan.scenarios)
                continue
            if attempt.status != "failed":
                break
        if not plans:
            raise InvalidSubmission("Developer produced no implementation within its turn budget")
        # Recheck protected Art bytes even if generated game code tried to modify them.
        for path, data in copies.items():
            if await context.tools.fingerprint(context.tools.output_path("game/" + path)) != hashlib.sha256(data).hexdigest():
                raise InvalidSubmission("Generated game modified a protected Art asset or license")
        all_files = await context.tools.list_files(context.tools.output_path("game"))
        if any("/game/.godot/" in path for path in all_files):
            await context.tools.remove_output("game/.godot")
        return await self._publish(context, plans, attempts, integrated, art_ref, changed, deleted, rejections, packaged, inspection)

    async def _publish(self, context, plans, attempts, integrated, art_ref, changed, deleted, rejections, packaged=(), inspection=None):
        tools, state = context.tools, context.state
        version = self._version(context, "game-project")
        revision_id = f"game-{version:04}"
        project_ref = ArtifactRef(artifact_id="game-project", version=version, kind="game_project",
            path=tools.output_path("game"), created_by="developer", sha256=await tools.fingerprint(tools.output_path("game")))
        refs = [project_ref, *(inspection.refs if inspection else ())]
        async def write(name, artifact_id, value, *, kind="report", media="application/json"):
            data = value.encode("utf-8")
            path = await tools.write_bytes(name, data)
            ref = ArtifactRef(artifact_id=artifact_id, version=self._version(context, artifact_id), kind=kind,
                path=path, created_by="developer", media_type=media, sha256=hashlib.sha256(data).hexdigest())
            refs.append(ref)
            return ref
        log_ref = await write("build_log.json", "developer-build-log", json.dumps(
            [a.model_dump(mode="json") for a in attempts], indent=2) + "\n", kind="log")
        affected = tuple(sorted({r for p in plans for r in p.affected_requirement_ids}))
        claim_set = set()
        for plan in plans:
            claim_set.difference_update(plan.affected_requirement_ids)
            claim_set.update(plan.implemented_requirement_ids)
        claims = tuple(sorted(claim_set))
        status = attempts[-1].status
        limitations = list(plans[-1].remaining_work)
        if inspection:
            limitations.extend(inspection.limitations())
        limitations.append("Basic engine checks do not verify gameplay, visual quality, replay behavior, or official Benchmark acceptance.")
        if any(a.is_mock for a in integrated):
            limitations.append("Integrated Art Producer images are mock placeholders; final artwork and visual acceptance remain pending.")
        if status != "passed":
            limitations.append("The complete basic engine check sequence has not passed; inspect build_log.json.")
        report = DeveloperReport(config=self.config, task_id=state.task.task_id, game_revision_id=revision_id,
            parent_revision_id=state.current_game_revision_id, project_ref=project_ref, design_ref=state.design.document_ref,
            art_manifest_ref=art_ref, summaries=tuple(p.summary for p in plans), changed_files=tuple(sorted(changed)),
            deleted_files=tuple(sorted(deleted)), affected_requirement_ids=affected, model_implementation_claims=claims,
            integrated_assets=integrated, scenarios=plans[-1].scenarios, controls=plans[-1].controls,
            build_attempts=tuple(attempts), proposal_rejections=tuple(rejections), limitations=tuple(limitations),
            packaged_traces=packaged, mcp_inspections=tuple(inspection.records) if inspection else (),
            self_review_completed=inspection.finished if inspection else False,
            self_review_summary=inspection.summary if inspection else None)
        await write("developer_report.json", "developer-report", report.model_dump_json(indent=2) + "\n")
        await write("developer_handoff.md", "developer-handoff", self._render(report), media="text/markdown")
        evidence = ()
        if status in ("passed", "failed"):
            evidence = (EvidenceRecord(evidence_id=f"build-{version:04}", kind="build_check", game_revision_id=revision_id,
                # The checks run against the whole assembled project, including
                # unchanged files. Patch scope is retained separately on the revision.
                requirement_ids=tuple(r.requirement_id for r in state.requirements), artifact_refs=(log_ref,), outcome=status,
                observation="Fixed isolated Godot import and startup checks of the complete current project: " + status +
                ". This is provenance for static inspection, not acceptance of any requirement. See original per-step output; gameplay and replay were not verified."),)
        game = GameRevision(revision_id=revision_id, project_ref=project_ref, parent_revision_id=state.current_game_revision_id,
            change_summary="\n".join(p.summary for p in plans), affected_requirement_ids=affected,
            build_status=status, build_evidence_ids=tuple(e.evidence_id for e in evidence))
        updates = tuple(r.revised(status="implemented_unverified", verification=None) for r in state.requirements
                        if status == "passed" and r.requirement_id in claims)
        partial = status != "passed" or plans[-1].status == "partial" or any(a.is_mock for a in integrated)
        if inspection:
            partial = partial or not inspection.finished or bool(inspection.remaining)
        return self._result(context, "partial" if partial else "completed",
            f"Published {revision_id}; basic build {status}. " + plans[-1].summary,
            artifact_refs=tuple(refs), proposed_updates=ProposedUpdates(game_revision=game, requirements=updates, evidence=evidence),
            evidence_refs=tuple(e.evidence_id for e in evidence))

    @staticmethod
    def _render(report):
        lines = ["# Developer handoff", "", f"Game revision: `{report.game_revision_id}`. Basic build: **{report.build_attempts[-1].status}**.", "",
            "## Changes", "", *["- " + text for text in report.summaries], "", "## Run", "",
            "Open game/project.godot in Godot 4 and let the editor import assets. For a fresh source package, first run `godot --headless --path game --import`, then `godot --path game`. In the target Benchmark environment, deploy the game directory to /workspace/game and import it there before launch.", "",
            "## Controls", "", *["- " + value for value in report.controls], "", "## Named scenarios", ""]
        lines.extend(f"- `{s.scenario_id}`: {s.description}" for s in report.scenarios)
        if report.mcp_inspections:
            lines.extend(["", "## MCP self-review", "",
                f"Completed: {report.self_review_completed}. Inspections: {len(report.mcp_inspections)}.",
                report.self_review_summary or "No final review decision was received.",
                "Full MCP transcripts, screenshots and visual helper inputs/outputs are referenced in developer_report.json.",
                "These are development diagnostics, not Player or QA acceptance evidence."])
        lines.extend(["", "## Assets", ""])
        lines.extend(f"- `{a.target_path}`: {a.asset_id}, source version {a.artifact_ref.version}, mock={a.is_mock}." for a in report.integrated_assets)
        lines.extend(["", "## Limitations and remaining validation", "", *["- " + value for value in report.limitations], "",
            "Player must produce real interactions, traces and replay evidence; QA must review the original instruction. No requirement was marked verified by Developer.", ""])
        return "\n".join(lines)
