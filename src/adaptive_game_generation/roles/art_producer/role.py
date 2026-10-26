"""Bounded source discovery, strict plan validation, and staged asset publication."""

from __future__ import annotations

import hashlib
import asyncio
import json
from pathlib import PurePosixPath
from types import SimpleNamespace

from pydantic import ValidationError

from ...models import ArtifactRef, AssetRecord, Blocker, ProposedUpdates, RoleResult
from ...runtime import InvalidSubmission, PermissionDenied, RoleContext
from ...runtime.media import raster_info
from ...models.asset_pipeline import AssetLayout
from ...runtime.asset_normalization import generated_layout, regions_for
from ...runtime.workspace import logical_path
from .contracts import ArtManifest, ArtProducerConfig, ArtTurn, ImageJob, ProducedAsset, ProposalRejection
from .prompt import build_prompt
from .sourcing import validate_fallback, validate_library_usage, validate_repair
from .motion import validate_motion, motion_quality


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Nonfinite JSON number")


class ArtProducer:
    def __init__(self, config: ArtProducerConfig | None = None):
        self.config = config or ArtProducerConfig()

    @staticmethod
    def _result(context, status, summary, **kwargs):
        return RoleResult(request_id=context.request.request_id, role="art_producer",
            state_revision=context.request.state_revision, status=status, summary=summary, **kwargs)

    def _ref(self, context, artifact_id, path, data, *, kind="document", media_type="application/json"):
        version = 1 + max((ref.version for ref in context.state.artifacts if ref.artifact_id == artifact_id), default=0)
        return ArtifactRef(artifact_id=artifact_id, version=version, kind=kind, path=path,
            created_by="art_producer", media_type=media_type, sha256=hashlib.sha256(data).hexdigest())

    def _layout(self, asset, candidate=None):
        layout = asset.layout
        if asset.width and self.config.normalize_assets and layout is None:
            raise InvalidSubmission("Raster assets require an explicit layout: whole sprite/background/ui, grid, regions, catalog, or unresolved atlas")
        if layout is None:
            return None
        metadata = candidate.atlas_metadata if candidate else None
        if layout.mode == "whole" and metadata and metadata.status == "available" and len(metadata.regions) > 1:
            raise InvalidSubmission("Library metadata identifies an atlas; select catalog frames instead of a whole texture")
        if layout.mode == "catalog":
            if metadata is None or metadata.status != "available":
                raise InvalidSubmission("Catalog layout requires available metadata from the selected library search result")
            lookup = {r.name: r for r in metadata.regions}
            if any(name not in lookup for name in asset.frame_names):
                raise InvalidSubmission("Select exact frame_names returned in atlas_metadata.regions")
            layout = AssetLayout(kind=layout.kind, mode="regions", regions=tuple(lookup[n] for n in asset.frame_names))
        try:
            frames = regions_for(layout, asset.width, asset.height, asset.columns, asset.rows, asset.frame_names)
            for frame in frames:
                logical_path(f"assets/normalized/{asset.asset_id}/{frame.name}.png")
        except (ValueError, PermissionDenied) as exc:
            raise InvalidSubmission(str(exc)) from exc
        return layout

    async def _inputs(self, context):
        state, request, tools = context.state, context.request, context.tools
        raw_design = await tools.read_bytes(state.design.document_ref.path)
        if state.design.document_ref.sha256 and hashlib.sha256(raw_design).hexdigest() != state.design.document_ref.sha256:
            raise InvalidSubmission("Design artifact digest does not match shared state")
        previous = None
        manifests = [r for r in state.artifacts if r.artifact_id == "art-manifest" and r.created_by == "art_producer"]
        if manifests:
            ref = max(manifests, key=lambda r: r.version)
            data = await tools.read_bytes(ref.path)
            if ref.sha256 != hashlib.sha256(data).hexdigest():
                raise InvalidSubmission("Art manifest digest mismatch")
            previous = ArtManifest.model_validate_json(data)
            if (previous.task_id, previous.version, previous.config) != (state.task.task_id, ref.version, self.config):
                raise InvalidSubmission("Art manifest task, version, or frozen configuration mismatch")
            records = {a.asset_id: a for a in state.assets}
            if any(records.get(a.record.asset_id) != a.record for a in previous.assets):
                raise InvalidSubmission("Art manifest disagrees with shared assets")
        selected = set(request.work_item_ids)
        issues = [i.model_dump(mode="json") for i in state.issues if i.issue_id in selected or
                  bool(selected.intersection(i.requirement_ids)) or
                  (not selected and i.status != "closed" and (i.suggested_role == "art_producer" or i.issue_type == "visual"))]
        attachments = []
        for ref in request.artifact_refs:
            item = {"ref": ref.model_dump(mode="json"), "visually_inspected": False}
            if ref.kind in ("document", "report") and ref.media_type in ("text/plain", "text/markdown", "application/json"):
                item["text"] = await tools.read_text(ref.path)
            attachments.append(item)
        sources = await tools.asset_sources()
        inputs = {
            "task": state.task.model_dump(mode="json"),
            "objective": request.objective, "completion_criteria": request.completion_criteria,
            "work_item_ids": request.work_item_ids,
            "requirements": [r.model_dump(mode="json", exclude={"status", "verification"}) for r in state.requirements],
            "design": raw_design.decode("utf-8"),
            "existing_assets": [a.model_dump(mode="json") for a in state.assets],
            "previous_manifest": previous.model_dump(mode="json") if previous else None,
            "issues": issues, "attachments": attachments, "sources": sources,
        }
        return inputs, previous, sources

    def _validate_plan(self, context, plan, catalog, previous, sources, observations=()):
        if len(plan.assets) > self.config.max_assets:
            raise InvalidSubmission("Art plan exceeds the asset count limit")
        known_requirements = {r.requirement_id for r in context.state.requirements}
        kept = {a.record.asset_id: a for a in previous.assets} if previous else {}
        validate_motion(plan.assets, tuple(kept.values()), required=self.config.require_actor_motion)
        reference_ids = set(kept)
        targets = {a.request.target_path.casefold(): key for key, a in kept.items()}
        final_targets = {key: value.request.target_path.casefold() for key, value in kept.items()}
        final_targets.update({a.asset_id: a.target_path.casefold() for a in plan.assets})
        paths = list(final_targets.values())
        if any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
               for index, a in enumerate(paths) for b in paths[index + 1:]):
            raise InvalidSubmission("Art targets collide as files or directories")
        for index, asset in enumerate(plan.assets):
            location = f"assets.{index}"
            if set(asset.requirement_ids) - known_requirements:
                raise InvalidSubmission(f"{location}.requirement_ids: Art plan references unknown requirements")
            owner = targets.get(asset.target_path.casefold())
            if owner is not None and owner != asset.asset_id:
                raise InvalidSubmission("Art target path belongs to another asset")
            if asset.source == "generate":
                if not self.config.allow_generation or not sources["image_service"]:
                    raise InvalidSubmission("Image generation is not configured or permitted")
                if self.config.library_first:
                    validate_fallback(asset, sources, observations, require_scoped=self.config.normalize_assets)
                if asset.reference_asset_ids:
                    if not sources["image_service"].get("supports_reference_images"):
                        raise InvalidSubmission("Image references are unsupported by the configured provider")
                    if set(asset.reference_asset_ids) - reference_ids:
                        raise InvalidSubmission(f"{asset.asset_id}: references must name existing assets or earlier assets in this batch")
            else:
                candidate = catalog.get((asset.source, asset.source_path))
                if candidate is None:
                    raise InvalidSubmission(f"{location}.source_path: Library asset was not returned by a metered search; select an exact source/path pair from search_observations or search again")
                validate_library_usage(asset, candidate)
                if (asset.width, asset.height) != (candidate.width, candidate.height):
                    raise InvalidSubmission(f"{location}: Plan must preserve library asset dimensions: width={candidate.width}, height={candidate.height}")
                if PurePosixPath(asset.target_path).suffix.lower() != PurePosixPath(candidate.path).suffix.lower():
                    raise InvalidSubmission("Plan must preserve the library file extension")
                if asset.transparent_background and not candidate.has_alpha and not (self.config.normalize_assets and asset.layout and asset.layout.kind in ("sprite", "sprite_sheet", "prop")):
                    raise InvalidSubmission("Library asset does not have an alpha channel")
            self._layout(asset, catalog.get((asset.source, asset.source_path)))
            validate_repair(asset, kept.get(asset.asset_id))
            reference_ids.add(asset.asset_id)

    def _validate_submission(self, context, plan, catalog, previous, sources, observations=()):
        if plan.status == "completed":
            self._validate_plan(context, plan, catalog, previous, sources, observations)
            return
        if plan.status == "no_op" and not context.state.assets:
            raise InvalidSubmission("Initial Art Producer invocation cannot return no_op")
        if plan.status == "no_op" and previous and any(a.normalization and a.normalization.status != "materialized" for a in previous.assets):
            raise InvalidSubmission("Unresolved or rejected assets still need corrected layouts or replacement sources; no_op cannot resolve them")
        if plan.status == "no_op" and previous and getattr(previous, "production_failures", ()):
            raise InvalidSubmission("Pending image production failures require completion or an explicit blocker")
        if plan.status == "no_op" and previous and motion_quality(previous.assets):
            raise InvalidSubmission("Unresolved animation frames require repair; no_op cannot resolve them")
        known = {r.requirement_id for r in context.state.requirements} | {i.issue_id for i in context.state.issues}
        if any(set(b.dependency_ids) - known for b in plan.blockers):
            raise InvalidSubmission("Art blocker references unknown dependencies")

    async def __call__(self, context: RoleContext):
        if context.request.role != "art_producer":
            raise PermissionDenied("Art Producer requires an art_producer request")
        if context.state.design is None or not context.state.requirements:
            return self._result(context, "blocked", "A public Designer specification is required",
                blockers=(Blocker(description="Provide the Designer document and grounded requirements"),))
        inputs, previous, sources = await self._inputs(context)
        catalog, observations, rejections = {}, [], []
        rejected = None
        plan = None
        search_turns = 0
        produced, refs, quality_attempts = {}, [], []
        completed_plan, repair_stop = None, None
        production_rounds = 0
        production_failures = []
        for turn in range(self.config.max_model_turns):
            if produced and context.tools.remaining_allowance("model_calls") == 0:
                repair_stop = "Shared model-call allowance exhausted; keep completed candidates and quarantine unresolved assets"
                break
            inputs["remaining_image_calls"] = context.tools.remaining_allowance("image_calls")
            prompt = build_prompt(inputs, self.config, observations, self.config.max_model_turns - turn,
                                  rejections, rejected, search_turns=search_turns)
            if len(prompt.encode("utf-8")) > self.config.max_prompt_bytes:
                raise InvalidSubmission("Art Producer input exceeds the prompt limit")
            raw = await context.tools.generate_text(prompt, max_output_tokens=self.config.max_output_tokens)
            if len(raw.encode("utf-8")) > self.config.max_response_bytes:
                raise InvalidSubmission("Art Producer response exceeds the byte limit")
            try:
                plan = ArtTurn.model_validate(json.loads(raw, object_pairs_hook=_object, parse_constant=_constant))
                if plan.action == "load_skills":
                    if not self.config.progressive_skills:
                        raise InvalidSubmission("On-demand skills are disabled in this frozen configuration")
                    loaded = set(inputs.get("loaded_skills", ()))
                    if set(plan.skills).issubset(loaded):
                        raise InvalidSubmission("Requested skills are already loaded")
                    inputs["loaded_skills"] = sorted(loaded | set(plan.skills))
                    continue
                if (plan.action == "search" and self.config.max_search_turns is not None
                        and search_turns >= self.config.max_search_turns):
                    raise InvalidSubmission("Search turn allowance exhausted; submit a complete plan using existing catalog results or report a concrete blocker")
                if plan.action == "submit":
                    current_assets = {a.record.asset_id: a for a in previous.assets} if previous else {}
                    current_assets.update(produced)
                    working = SimpleNamespace(assets=tuple(current_assets.values()),
                        production_failures=previous.production_failures if previous else ()) if current_assets else previous
                    self._validate_submission(context, plan, catalog, working, sources, observations)
                    remaining = context.tools.remaining_allowance("image_calls")
                    if (self.config.max_quality_repair_rounds and remaining is not None and
                            sum(a.source == "generate" for a in plan.assets) > remaining):
                        raise InvalidSubmission("This batch exceeds the remaining shared image-call allowance; choose library sources or report a blocker")
                    if production_rounds:
                        motion_failed = {f["asset_id"] for f in motion_quality(tuple(current_assets.values()))}
                        for asset in plan.assets:
                            prior = current_assets.get(asset.asset_id)
                            if prior and asset.asset_id not in motion_failed and (not prior.normalization or prior.normalization.status == "materialized"):
                                raise InvalidSubmission(f"Asset {asset.asset_id} already passed; quality repair must leave accepted candidates unchanged")
            except (ValueError, InvalidSubmission) as exc:
                # Only model-authored proposals are repairable. Provider, permission,
                # budget and image failures remain outside this bounded correction loop.
                if self.config.prompt_version != "art-producer.v4":
                    if isinstance(exc, InvalidSubmission):
                        raise
                    raise InvalidSubmission("Art Producer returned an invalid JSON proposal") from None
                if isinstance(exc, ValidationError):
                    errors = tuple((".".join(map(str, e["loc"])) + ": " + e["msg"])[:320]
                        for e in exc.errors(include_input=False, include_context=False, include_url=False)[:16])
                elif isinstance(exc, json.JSONDecodeError):
                    errors = (f"Invalid or incomplete JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}. "
                              "Return a complete, shorter JSON object; do not continue the previous response.",)
                else:
                    errors = (str(exc)[:320],)
                rejections.append(ProposalRejection(model_turn=turn + 1, errors=errors))
                if turn + 1 == self.config.max_model_turns and not produced:
                    raise InvalidSubmission("Art Producer proposal validation budget exhausted: " + "; ".join(errors)[:1200]) from None
                if turn + 1 == self.config.max_model_turns:
                    repair_stop = "Quality repair proposal budget exhausted: " + "; ".join(errors)
                    break
                # Full replies stay in the provider capture. Do not feed an unbounded
                # tile enumeration back into the next prompt.
                encoded = raw.encode("utf-8")
                rejected = {"excerpt": encoded[:8192].decode("utf-8", errors="ignore"),
                            "truncated": len(encoded) > 8192, "original_bytes": len(encoded)}
                continue
            rejected = None
            if plan.action == "submit":
                if plan.status != "completed":
                    if not produced:
                        return self._result(context, plan.status, plan.summary, blockers=plan.blockers)
                    repair_stop = plan.summary
                    break
                batch, batch_refs, failed = await self._produce(context, plan, catalog, sources, production_rounds + 1,
                                                      reference_assets=current_assets)
                completed_plan = plan
                production_rounds += 1
                produced.update({a.record.asset_id: a for a in batch})
                refs.extend(batch_refs)
                if failed:
                    production_failures.extend(failed)
                    repair_stop = "Image transport failed; successful candidates are retained. No automatic paid retry was made."
                    break
                if self.config.max_quality_repair_rounds:
                    quality_attempts.extend({"round": production_rounds, "model_turn": turn + 1,
                        "asset_id": a.record.asset_id, "candidate": a.model_dump(mode="json")} for a in batch)
                current_assets.update(produced)
                motion_feedback = motion_quality(tuple(current_assets.values()))
                motion_failed = {f["asset_id"] for f in motion_feedback}
                failures = [a for a in current_assets.values() if (a.normalization and a.normalization.status != "materialized")
                            or a.record.asset_id in motion_failed]
                if not failures:
                    break
                if (context.tools.remaining_allowance("image_calls") == 0 and
                        not any(s["available"] for s in sources["libraries"])):
                    repair_stop = "Shared image-call allowance exhausted and no library is available; rejected candidates remain quarantined"
                    break
                if production_rounds > self.config.max_quality_repair_rounds or turn + 1 == self.config.max_model_turns:
                    repair_stop = "Quality repair allowance exhausted; rejected candidates remain quarantined"
                    break
                inputs["current_asset_candidates"] = [a.model_dump(mode="json") for a in current_assets.values()]
                inputs["quality_feedback"] = [{"asset_id": a.record.asset_id, "request": a.request.model_dump(mode="json"),
                    "image_job": a.image_job.model_dump(mode="json") if a.image_job else None,
                    "normalization": a.normalization.model_dump(mode="json") if a.normalization else None,
                    "motion_findings": [f for f in motion_feedback if f["asset_id"] == a.record.asset_id]} for a in failures]
                inputs["quality_repair_rounds_left"] = self.config.max_quality_repair_rounds - production_rounds + 1
                continue
            if turn + 1 == self.config.max_model_turns:
                if produced:
                    repair_stop = "Asset discovery exhausted remaining quality repair model turns"
                    break
                return self._result(context, "blocked", "Asset discovery reached the configured model-turn limit",
                    blockers=(Blocker(description="No final asset plan was submitted within the bounded discovery allowance"),))
            search_turns += 1
            for search in plan.searches:
                found = await context.tools.search_assets(search.library, search.query,
                    offset=search.offset, limit=self.config.search_results)
                observations.append({"search_id": f"search-{len(observations) + 1:03}",
                    "asset_ids": list(search.asset_ids), **found.model_dump(mode="json")})
                catalog.update({(a.library, a.path): a for a in found.assets})
        if completed_plan is None:
            raise InvalidSubmission("Art Producer produced no accepted plan")
        return await self._publish(context, completed_plan, previous, list(produced.values()), refs,
                                   rejections, observations, quality_attempts, repair_stop, production_failures, sources["libraries"])

    async def _produce(self, context, plan, catalog, sources, round_number, reference_assets=None):
        # The entire proposal was validated before importing bytes or spending image calls.
        tools, refs, produced = context.tools, [], []
        references = dict(reference_assets or {})
        failed = []
        prefix = f"art_attempts/{round_number:03}" if self.config.max_quality_repair_rounds else ""
        def destination(path):
            return prefix + "/" + path if prefix else path
        def identity(name):
            return name + f"-attempt-{round_number:03}" if prefix else name
        for asset_index, asset in enumerate(plan.assets):
            license_ref, job, candidate = None, None, None
            native_layout, display_regions = None, ()
            if asset.source == "generate":
                service = sources["image_service"]
                job = ImageJob(prompt=("ASSET TO DRAW (the sole subject of this image):\n" + asset.generation_prompt +
                    "\n\nSTYLE CONTEXT (palette and rendering only, not additional subjects):\n" + plan.style_guide +
                    "\n\nDraw only the requested asset. Do not turn style references to other game objects into scenery, "
                    "UI panels, ground, text or backgrounds inside an isolated sprite. Follow the asset request above."),
                    width=asset.width, height=asset.height, transparent_background=asset.transparent_background,
                    columns=asset.columns, rows=asset.rows, frame_names=asset.frame_names, layout=asset.layout)
                ref_ids = asset.reference_asset_ids
                if not ref_ids and asset.asset_id in references and service.get("supports_reference_images"):
                    ref_ids = (asset.asset_id,)
                image_refs = tuple(references[key].record.artifact_ref for key in ref_ids)
                if image_refs:
                    job = job.revised(prompt=job.prompt + "\nREFERENCE IMAGES: Preserve the referenced character identity, "
                        "palette, costume, materials and rendering style. Change only the requested pose/state or "
                        "explicit quality defect. Do not copy unrelated backgrounds, labels or layout errors.")
                kwargs = {"image_refs": image_refs} if image_refs else {}
                from ...providers.openai_compatible import ProviderFailure
                try:
                    path = await tools.generate_image(job.model_dump_json(), destination(asset.target_path), **kwargs)
                except (ProviderFailure, asyncio.TimeoutError) as exc:
                    # Keep already completed bytes. Security/budget/validation and
                    # invocation cancellation still propagate; never conceal them.
                    if not produced and not reference_assets:
                        raise
                    failed = [{"asset_id": item.asset_id, "request": item.model_dump(mode="json"),
                        "status": "provider_failed" if i == 0 else "not_attempted",
                        "reason": str(exc) or "Image tool timed out; no automatic retry"}
                        for i, item in enumerate(plan.assets[asset_index:])]
                    break
                data = await tools.read_bytes(path)
                media = raster_info(data)
                native = service.get("output_geometry", "requested") == "source_native"
                if media["media_type"] != "image/png" or (not native and (media["width"], media["height"]) != (asset.width, asset.height)):
                    raise InvalidSubmission("Image service output does not match the requested PNG dimensions")
                if native:
                    try:
                        native_layout, display_regions = generated_layout(job, media["width"], media["height"])
                    except ValueError as exc:
                        raise InvalidSubmission(str(exc)) from exc
                if asset.transparent_background and not media["has_alpha"]:
                    raise InvalidSubmission("Image service omitted the requested alpha channel")
                is_mock, license_text = service["is_mock"], service["license"]
                source = ("mock:" if is_mock else "generated:") + service["provider_id"]
            else:
                candidate = catalog[(asset.source, asset.source_path)]
                path = await tools.import_asset(candidate, destination(asset.target_path))
                data = await tools.read_bytes(path)
                media = {"media_type": candidate.media_type, "width": candidate.width,
                         "height": candidate.height, "has_alpha": candidate.has_alpha}
                is_mock, license_text = False, candidate.license_text
                source = f"{candidate.library}:{candidate.path}"
                license_bytes = license_text.encode("utf-8")
                license_path = await tools.write_bytes(destination(f"licenses/{asset.asset_id}.txt"), license_bytes)
                license_ref = self._ref(context, identity("art-license-" + asset.asset_id), license_path,
                                        license_bytes, media_type="text/plain")
                refs.append(license_ref)
            ref = self._ref(context, identity("art-asset-" + asset.asset_id), path, data, kind="asset", media_type=media["media_type"])
            refs.append(ref)
            layout = native_layout or self._layout(asset, candidate)
            normalization = None
            if layout:
                normalization = await tools.normalize_asset(ref, asset_id=asset.asset_id,
                    source_target=asset.target_path, layout=layout, columns=asset.columns, rows=asset.rows,
                    names=asset.frame_names, layout_source=candidate.atlas_metadata if candidate else None,
                    display_regions=display_regions, artifact_prefix=prefix)
                refs.extend(normalization.artifact_refs)
            notes = (f"Copy to {asset.target_path}. Filter: {asset.filter_mode}. Grid: {asset.columns} x {asset.rows}. "
                     f"Frames (row-major): {', '.join(asset.frame_names)}. " + asset.integration_notes +
                     " License handling: " + asset.license_handling +
                     (" MOCK PLACEHOLDER: replace before visual acceptance." if is_mock else "") +
                     " Visual review has not been performed.")
            if normalization:
                notes = ("Original image is source-only and must not be loaded by the game. Use normalized frames: " +
                    ", ".join(f.target_path for f in normalization.frames) +
                    f". Filter: {asset.filter_mode}. Preserve frame pivots and durations from the manifest. " +
                    "Native frame dimensions can exceed logical display_size. Use a single uniform render scale; never resize or stretch texture files. " +
                    asset.integration_notes + " License handling: " + asset.license_handling +
                    (" MOCK PLACEHOLDER." if is_mock else "") + " Visual acceptance remains pending.")
            record = AssetRecord(asset_id=asset.asset_id, artifact_ref=ref, purpose=asset.purpose,
                source=source, license=license_text, integration_notes=notes)
            produced.append(ProducedAsset(request=asset, record=record, is_mock=is_mock,
                width=media["width"], height=media["height"], has_alpha=media["has_alpha"],
                source_sha256=candidate.sha256 if candidate else None,
                source_license_path=candidate.license_path if candidate else None,
                source_license_sha256=candidate.license_sha256 if candidate else None,
                license_ref=license_ref, image_job=job, normalization=normalization))
            references[asset.asset_id] = produced[-1]
        return produced, refs, failed

    async def _publish(self, context, plan, previous, produced, refs, rejections, observations, quality_attempts, repair_stop, production_failures=(), library_availability=()):
        tools = context.tools
        merged = {a.record.asset_id: a for a in previous.assets} if previous else {}
        merged.update({a.record.asset_id: a for a in produced})
        pending = {f["asset_id"]: f for f in previous.production_failures} if previous else {}
        for a in produced:
            pending.pop(a.record.asset_id, None)
        pending.update({f["asset_id"]: f for f in production_failures})
        limitations = ["Images were checked for file format and dimensions, not visually reviewed or integrated into a game."]
        if repair_stop:
            limitations.append(repair_stop)
        if pending:
            limitations.append("Production remains pending for: " + ", ".join(pending))
        placeholders = any(a.is_mock for a in merged.values())
        if placeholders:
            limitations.append("Mock-generated images are test placeholders and do not fulfill final artwork requirements.")
        unresolved = [a.record.asset_id for a in merged.values() if a.normalization and a.normalization.status != "materialized"]
        if unresolved:
            limitations.append("Unresolved or quality-rejected sources are not display-ready: " + ", ".join(unresolved))
        motion_findings = motion_quality(tuple(merged.values()))
        limitations.extend(f"Motion {f['asset_id']}/{f['clip']}: {f['message']}" for f in motion_findings)
        manifest = ArtManifest(task_id=context.state.task.task_id, version=previous.version + 1 if previous else 1,
            config=self.config, proposal_rejections=tuple(rejections), search_observations=tuple(observations),
            quality_attempts=tuple(quality_attempts),
            production_failures=tuple(pending.values()),
            library_availability=tuple(library_availability),
            design_ref=context.state.design.document_ref, style_guide=plan.style_guide,
            summary=plan.summary, assets=tuple(merged.values()),
            affected_scenes=tuple(sorted({s for a in produced for s in a.request.affected_scenes})),
            limitations=tuple(limitations))
        data = (manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
        path = await tools.write_bytes("asset_manifest.json", data)
        refs.append(self._ref(context, "art-manifest", path, data))
        markdown = self._render(manifest)
        path = await tools.write_text("art_handoff.md", markdown)
        refs.append(self._ref(context, "art-handoff", path, markdown.encode("utf-8"), media_type="text/markdown"))
        return self._result(context, "partial" if placeholders or unresolved or motion_findings or pending else "completed",
            plan.summary + (" Image generation was mocked; final artwork remains pending." if placeholders else "") +
            (" Replace or repair non-display-ready assets: " + ", ".join(unresolved) if unresolved else "") +
            (" Repair unresolved motion frames." if motion_findings else "") +
            (" Image production remains pending: " + ", ".join(pending) if pending else ""),
            artifact_refs=tuple(refs), proposed_updates=ProposedUpdates(assets=tuple(a.record for a in produced)))

    @staticmethod
    def _render(manifest):
        lines = ["# Art Producer handoff", "", manifest.summary, "", "## Style guide", "", manifest.style_guide,
                 "", "## Limitations", "", *["- " + value for value in manifest.limitations], "",
                 "## Changed scenes", "", *["- " + scene for scene in manifest.affected_scenes], "", "## Asset inventory", ""]
        for item in manifest.assets:
            draft, record = item.request, item.record
            lines.extend([f"### {record.asset_id}", "", f"- {'Source-only' if item.normalization else 'Target'}: `{draft.target_path}`",
                f"- Published artifact: `{record.artifact_ref.path}` (version {record.artifact_ref.version})",
                f"- Source: `{record.source}`; mock: `{item.is_mock}`",
                f"- Dimensions: {item.width} x {item.height}; alpha channel: {item.has_alpha}",
                f"- Requirements: {', '.join(draft.requirement_ids)}",
                f"- Affected scenes: {', '.join(draft.affected_scenes)}", "", record.integration_notes, "",
                "License:", "", record.license, ""])
            if item.normalization:
                n = item.normalization
                lines.extend([f"- Normalization: {n.status}; preview: `{n.preview_ref.path}`", ""])
                if n.quality:
                    lines.extend(f"- Quality [{f.code}] {f.frame}: {f.message}" for f in n.quality.findings)
                lines.extend(f"- Frame `{f.region.name}`: `{f.target_path}`; rectangle=({f.region.x},{f.region.y},{f.region.width},{f.region.height}); display_size={f.display_size}; pivot=({f.region.pivot_x},{f.region.pivot_y}); duration={f.region.duration_ms}ms." for f in n.frames)
                lines.extend(["", *["- " + limit for limit in n.limitations], ""])
        return "\n".join(lines)
