"""Offline pixel, boundary and end-to-end contract regressions; no live APIs."""
import asyncio
import hashlib
import io
import json
import os
from dataclasses import replace
from pathlib import Path
import shutil

import pytest
from PIL import Image
from pydantic import ValidationError

from adaptive_game_generation.models import ArtifactRef, Role, RoleResult, ResourceLimits
from adaptive_game_generation.models.asset_pipeline import AssetLayout, FrameRegion
from adaptive_game_generation.roles.art_producer import ArtProducer, ArtProducerConfig
from adaptive_game_generation.roles.developer import Developer, DeveloperConfig, DeveloperReport
from adaptive_game_generation.roles.developer.assets import asset_reference_findings, asset_package_findings
from adaptive_game_generation.runtime import PermissionDenied
from adaptive_game_generation.runtime.asset_normalization import normalize_pixels, png, regions_for, parse_atlas_metadata
from adaptive_game_generation.runtime.policy import RolePolicy
from test_art_producer import make_run, invoke, manifest, asset, proposal, library_pair, search_turn
from test_developer import ScriptedSandbox, proposal as dev_proposal, invoke as dev_invoke, report

FIXTURES = Path(__file__).parent / "fixtures/asset_normalization"
GRID = AssetLayout(kind="sprite_sheet", mode="grid", cell_width=32, cell_height=32)


def sheet(**changes):
    return asset(width=64, height=32, columns=2, frame_names=["idle", "run"], layout=GRID.model_dump(), **changes)


def test_grid_margins_spacing_and_rgba_preserve_exact_pixels():
    source = Image.new("RGBA", (15, 12))
    for y in range(12):
        for x in range(15):
            source.putpixel((x, y), (x * 10, y * 10, 80, (x * 20) % 256))
    data = png(source)
    layout = AssetLayout(kind="sprite_sheet", mode="grid", cell_width=4, cell_height=3,
                         margin_x=2, margin_y=1, spacing_x=3, spacing_y=2)
    one = normalize_pixels(data, layout, 2, 2, ("a", "b", "c", "d"))
    two = normalize_pixels(data, layout, 2, 2, ("a", "b", "c", "d"))
    assert one == two  # Includes stable encoded bytes and contact sheet.
    for frame, rect in zip(one[1], [(2,1,6,4), (9,1,13,4), (2,6,6,9), (9,6,13,9)]):
        assert Image.open(io.BytesIO(frame["data"])).tobytes() == source.crop(rect).tobytes()
    assert png(source) == data


@pytest.mark.parametrize("case", ["bounds", "negative", "count", "duplicate", "whole_atlas", "full_region", "pixel_budget", "unsafe_name"])
def test_bad_layouts_fail_before_any_crop(case):
    layout, width, height, cols, rows, names = GRID, 64, 32, 2, 1, ["a", "b"]
    with pytest.raises(ValueError):
        if case == "bounds": width = 63
        if case == "negative": layout = AssetLayout(kind="sprite_sheet", mode="regions", regions=[dict(name="a", x=-1,y=0,width=2,height=2)])
        if case == "count": names = ["a"]
        if case == "duplicate": names = ["a", "A"]
        if case == "unsafe_name": names = ["../outside", "b"]
        if case == "whole_atlas": layout = AssetLayout(kind="tile_set", mode="whole")
        if case == "full_region": layout = AssetLayout(kind="tile_set", mode="regions", regions=[FrameRegion(name="a", x=0,y=0,width=64,height=32)]); names=["a"]
        if case == "pixel_budget":
            width=height=4096
            layout = AssetLayout(kind="source_atlas", mode="regions", regions=[FrameRegion(name=n,x=0,y=0,width=4096,height=4095) for n in names])
        regions_for(layout, width, height, cols, rows, names)


def test_explicit_regions_preserve_pivots_timing_empty_alpha_and_unresolved():
    region = FrameRegion(name="left", x=1,y=0,width=5,height=3,pivot_x=0.2,pivot_y=1,animation="walk",duration_ms=80)
    layout = AssetLayout(kind="source_atlas",mode="regions",regions=(region,))
    data = png(Image.new("RGBA", (9, 7)))
    _, frames, _ = normalize_pixels(data,layout,1,1,["left"])
    assert frames[0]["region"] == region and frames[0]["visible_pixels"] == 0
    assert frames[0]["transparent_pixels"] == 15
    assert normalize_pixels(data,AssetLayout(kind="source_atlas",mode="unresolved"),1,1,["source"])[1] == []


@pytest.mark.parametrize("filename,layout,columns,rows,names", [
    ("generated_player.png",GRID,4,1,["idle","run","jump","fall"]),
    ("oga_font.png",AssetLayout(kind="tile_set",mode="regions",regions=(
        FrameRegion(name="digit_0",x=0,y=12,width=8,height=12),
        FrameRegion(name="digit_1",x=8,y=12,width=8,height=12))),1,1,["digit_0","digit_1"]),
    ("oga_scene_layer.png",AssetLayout(kind="source_atlas",mode="regions",regions=(
        FrameRegion(name="ledge_fragment",x=128,y=144,width=32,height=16),)),1,1,["ledge_fragment"]),
])
def test_recorded_real_assets_have_exact_crops_and_preserved_provenance(filename,layout,columns,rows,names):
    entry = next(e for e in json.loads((FIXTURES/"provenance.json").read_text()) if e["file"]==filename)
    data=(FIXTURES/filename).read_bytes()
    assert hashlib.sha256(data).hexdigest()==entry["sha256"]
    source=Image.open(io.BytesIO(data)).convert("RGBA")
    _,frames,_=normalize_pixels(data,layout,columns,rows,names)
    for f in frames:
        r=f["region"]
        assert Image.open(io.BytesIO(f["data"])).tobytes()==source.crop((r.x,r.y,r.x+r.width,r.y+r.height)).tobytes()


@pytest.mark.parametrize("suffix", ["xml","json"])
def test_library_metadata_resolves_exact_regions_and_detects_changes(tmp_path,suffix):
    libraries,roots=library_pair(tmp_path)
    sidecar=roots["kenney"]/f"pack/player.{suffix}"
    if suffix=="xml":
        data=b'<TextureAtlas imagePath="player.png"><SubTexture name="idle.png" x="0" y="0" width="16" height="32"/></TextureAtlas>'
    else:
        data=json.dumps({"meta":{"image":"player.png"},"frames":{"idle.png":{"frame":{"x":0,"y":0,"w":16,"h":32},"pivot":{"x":0.25,"y":1},"duration":120}}}).encode()
    sidecar.write_bytes(data)
    selected=asyncio.run(libraries.search("kenney","player",limit=1)).assets[0]
    metadata=selected.atlas_metadata
    assert metadata.status=="available" and metadata.regions[0].width==16
    if suffix=="json":
        assert metadata.regions[0].pivot_x==0.25 and metadata.regions[0].pivot_y==1 and metadata.regions[0].duration_ms==120
    assert asyncio.run(libraries.read(selected,max_bytes=100000))
    sidecar.write_bytes(data+b" ")
    from adaptive_game_generation.runtime import InvalidSubmission
    with pytest.raises(InvalidSubmission): asyncio.run(libraries.read(selected,max_bytes=100000))


@pytest.mark.parametrize("data,suffix", [
    (b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///secret">]><TextureAtlas/>',"xml"),
    (b'{"meta":{"image":"other.png"},"frames":{}}',"json"),
    (b'{"meta":{"image":"a.png"},"frames":{"a":{"rotated":true}}}',"json"),
    (b'{"meta":{"image":"a.png"},"frames":{"a":{"trimmed":true}}}',"json"),
    (b'{"meta":{"image":"a.png"},"frames":{"a":{"frame":{"x":0,"y":0,"w":2,"h":2},"sourceSize":{"w":4,"h":4}}}}',"json"),
    (b'x'*262145,"json"),
], ids=["xml-entities", "wrong-image", "rotated", "trimmed", "implicit-trimming", "oversized"])
def test_unsupported_metadata_never_silently_guesses(data,suffix):
    result=parse_atlas_metadata(data,"a."+suffix,"a.png")
    assert result.status=="unsupported" and not result.regions


@pytest.mark.parametrize("bad", ["missing", "outside", "whole"])
def test_art_rejects_bad_layout_before_image_call(tmp_path,bad):
    value=sheet()
    if bad=="missing": value.pop("layout")
    if bad=="outside": value["layout"]["margin_x"]=1
    if bad=="whole": value["layout"]={"kind":"sprite_sheet","mode":"whole"}
    executor,_,images=make_run(tmp_path,proposal(value))
    result=invoke(executor,ArtProducer(ArtProducerConfig(normalize_assets=True,max_model_turns=1)))
    assert result.status=="failed" and not images.calls and not executor.state.assets


@pytest.mark.parametrize("role", ["designer","developer","player","qa_reviewer","scheduler"])
def test_normalizer_permission_is_only_art(role):
    with pytest.raises(PermissionDenied): RolePolicy().allow_tool(role,"asset_normalize")


def test_denied_normalizer_is_audited_before_reading_bytes(tmp_path):
    executor,_,_=make_run(tmp_path,seed=False)
    async def adapter(context):
        await context.tools.normalize_asset(None,asset_id="player",source_target="assets/player.png",layout=GRID,names=["idle"])
    request=executor.request("developer","Check normalization permissions",("No unauthorized file read",))
    result=asyncio.run(executor.execute(request,adapter))
    assert result.status=="failed" and result.error.code=="permission_denied"
    audit=[json.loads(line) for line in (executor.workspace.private/"tools.jsonl").read_text().splitlines()]
    assert len(audit)==1 and audit[0]["tool"]=="asset_normalize" and audit[0]["outcome"]=="denied"
    assert executor.state.ledger.usage.tool_calls==0


def test_normalizer_charges_tools_and_not_additional_images_and_prevents_atlas_misuse(tmp_path):
    executor,_,images=make_run(tmp_path,proposal(sheet()))
    result=invoke(executor,ArtProducer(ArtProducerConfig(normalize_assets=True)))
    assert result.status=="partial", result.error
    p=manifest(executor).assets[0].normalization
    assert p.status=="materialized" and len(p.frames)==2 and len(images.calls)==1
    audit=(executor.workspace.private/"tools.jsonl").read_text()
    assert '"asset_normalize"' in audit
    assert all(ref in executor.state.artifacts for ref in p.artifact_refs)
    bad=dev_proposal(script='extends Node2D\nvar texture = preload("res://assets/player.png")\n')
    good=dev_proposal(script='extends Node2D\nvar texture = preload("res://assets/normalized/player/idle.png")\n')
    from test_art_producer import ScriptedText
    provider=ScriptedText(bad,good)
    sandbox=ScriptedSandbox()
    executor.services=replace(executor.services,text=provider,sandbox=sandbox)
    result=dev_invoke(executor,DeveloperConfig(require_normalized_assets=True))
    assert result.status=="partial", result.error
    doc=report(executor)
    assert len(doc.proposal_rejections)==1 and "source_atlas_reference" in str(doc.proposal_rejections)
    assert len(doc.build_attempts)==1 and len(provider.prompts)==2
    game=executor.workspace.read_public(doc.project_ref.path)
    assert not (game/"assets/player.png").exists()
    assert (game/p.frames[0].target_path).exists()
    assert asset_package_findings({"assets/renamed.png":p.source_ref.sha256},[p])
    assert not asset_package_findings({f.target_path:f.artifact_ref.sha256 for f in p.frames},[p])
    assert not asset_reference_findings({"x.gd":'# "res://assets/player.png"\n'},[p])


def test_unresolved_library_layout_stays_partial_and_is_not_copied(tmp_path):
    libraries,_=library_pair(tmp_path)
    executor,_,images=make_run(tmp_path/"case",search_turn(),proposal(asset(source="kenney",source_path="pack/player.png",
        generation_prompt=None,layout={"kind":"source_atlas","mode":"unresolved"})),libraries=libraries)
    result=invoke(executor,ArtProducer(ArtProducerConfig(normalize_assets=True)))
    assert result.status=="partial",result.error
    assert not manifest(executor).assets[0].normalization.frames and not images.calls
    from test_art_producer import ScriptedText
    executor.services=replace(executor.services,text=ScriptedText(dev_proposal()),sandbox=ScriptedSandbox())
    result=dev_invoke(executor,DeveloperConfig(require_normalized_assets=True))
    assert result.status=="partial",result.error
    doc=report(executor)
    assert not doc.integrated_assets and any("resolve layout" in s for s in doc.limitations)
    from adaptive_game_generation.controllers.work import WorkPolicy
    needed,reason,work=WorkPolicy(executor.workspace).plan(executor.state,Role.ART_PRODUCER)
    assert needed and "layouts" in reason and "R001" in work
    from types import SimpleNamespace
    from adaptive_game_generation.roles.art_producer.contracts import ArtTurn
    from adaptive_game_generation.runtime import InvalidSubmission
    with pytest.raises(InvalidSubmission,match="Unresolved or rejected"):
        ArtProducer()._validate_submission(SimpleNamespace(state=executor.state),
            ArtTurn(action="submit",status="no_op",summary="Retain source"),{},manifest(executor),{})


def test_catalog_layout_materializes_library_frames_and_keeps_license(tmp_path):
    libraries,roots=library_pair(tmp_path)
    # A solid rectangle is now correctly quarantined as a suspect actor panel.
    # Use a clean small silhouette for this positive metadata/license test.
    from PIL import ImageDraw
    sprite = Image.new("RGBA", (32, 32))
    draw = ImageDraw.Draw(sprite)
    draw.ellipse((5, 4, 10, 11), fill="cyan")
    draw.polygon([(6, 11), (9, 11), (12, 22), (8, 19), (4, 22)], fill="cyan")
    sprite.save(roots["kenney"] / "pack/player.png")
    (roots["kenney"]/"pack/player.xml").write_text('<TextureAtlas imagePath="player.png"><SubTexture name="left" x="0" y="0" width="16" height="32"/></TextureAtlas>')
    candidate=asyncio.run(libraries.search("kenney","player",limit=1)).assets[0]
    name=candidate.atlas_metadata.regions[0].name
    executor,_,images=make_run(tmp_path/"case",search_turn(),proposal(asset(source="kenney",source_path="pack/player.png",
        generation_prompt=None,frame_names=[name],layout={"kind":"sprite_sheet","mode":"catalog"})),libraries=libraries)
    result=invoke(executor,ArtProducer(ArtProducerConfig(normalize_assets=True)))
    assert result.status=="completed",result.error
    item=manifest(executor).assets[0]
    assert item.normalization.layout_source==candidate.atlas_metadata
    assert item.normalization.frames[0].region.width==16 and item.license_ref and not images.calls
    from test_art_producer import ScriptedText
    executor.services=replace(executor.services,text=ScriptedText(dev_proposal()),sandbox=ScriptedSandbox())
    assert dev_invoke(executor,DeveloperConfig(require_normalized_assets=True)).status=="completed"
    doc=report(executor)
    license_path=doc.integrated_assets[0].license_target
    assert executor.workspace.read_public(doc.project_ref.path+"/"+license_path).read_text()==candidate.license_text


def test_revised_art_removes_stale_frames_and_requires_assembly(tmp_path):
    from test_art_producer import ScriptedText
    executor,_,_=make_run(tmp_path,proposal(sheet()))
    art=ArtProducer(ArtProducerConfig(normalize_assets=True))
    assert invoke(executor,art).status=="partial"
    config=DeveloperConfig(require_normalized_assets=True)
    class ImportSidecarSandbox(ScriptedSandbox):
        async def run(self,workspace,argv,timeout):
            result=await super().run(workspace,argv,timeout)
            for path in workspace.output_path("game/assets/normalized").rglob("*.png"):
                path.with_suffix(".png.import").write_text("Offline engine import sidecar fixture")
            return result
    executor.services=replace(executor.services,text=ScriptedText(dev_proposal()),sandbox=ImportSidecarSandbox())
    assert dev_invoke(executor,config).status=="partial"
    next_asset=sheet(); next_asset["frame_names"]=["new_idle","new_run"]
    executor.services=replace(executor.services,text=ScriptedText(proposal(next_asset)))
    assert invoke(executor,art).status=="partial"
    text=ScriptedText({"status":"no_op","summary":"No code changes needed"},dev_proposal())
    executor.services=replace(executor.services,text=text)
    result=dev_invoke(executor,config)
    assert result.status=="partial",result.error
    doc=report(executor); game=executor.workspace.read_public(doc.project_ref.path)
    assert not (game/"assets/normalized/player/idle.png").exists()
    assert not (game/"assets/normalized/player/idle.png.import").exists()
    assert (game/"assets/normalized/player/new_idle.png").exists()
    assert len(doc.proposal_rejections)==1


def test_visual_helper_receives_contact_sheet_for_normalized_frames(tmp_path):
    from types import SimpleNamespace
    from adaptive_game_generation.roles.developer.inspection import InspectionCycle
    from test_art_producer import ScriptedText
    executor,_,_=make_run(tmp_path,proposal(sheet()))
    assert invoke(executor,ArtProducer(ArtProducerConfig(normalize_assets=True))).status=="partial"
    art=manifest(executor)
    class Reader:
        async def read_bytes(self,path): return executor.workspace.read_public(path).read_bytes()
    cycle=InspectionCycle(Developer(),SimpleNamespace(tools=Reader()),art.model_dump(mode="json"))
    refs,descriptions=asyncio.run(cycle.source_images({"main.gd":'load("res://assets/normalized/player/%s.png" % frame)'},8*1024*1024))
    assert refs==(art.assets[0].normalization.preview_ref,) and descriptions[0]["normalization"]


def test_normalizer_budget_denial_prevents_publication(tmp_path):
    # Reserve only the file read: the normalization operation itself needs a tool call.
    executor,_,_=make_run(tmp_path,seed=False,budget=ResourceLimits(max_tool_calls=1))
    # Use a known public image ref instead of touching hidden input or issuing an image request.
    data=(FIXTURES/"generated_player.png").read_bytes()
    path=executor.workspace.public/"input/probe.png"; path.write_bytes(data)
    ref=ArtifactRef(artifact_id="probe",kind="asset",path="input/probe.png",created_by="system",sha256=hashlib.sha256(data).hexdigest())
    async def adapter(context):
        await context.tools.normalize_asset(ref,asset_id="probe",source_target="assets/probe.png",layout=GRID,names=["a","b","c","d"],columns=4)
    result=invoke(executor,adapter)
    assert result.status=="failed" and result.error.code=="budget_exceeded"
    assert not executor.state.assets and executor.state.ledger.usage.image_calls==0


def test_qa_vetoes_unresolved_real_library_art(tmp_path):
    from adaptive_game_generation.controllers import FixedLoop, FixedLoopConfig
    from adaptive_game_generation.roles.qa_reviewer import QAReport
    from test_fixed_loop import make_run as chain, report as chain_report
    libraries,_=library_pair(tmp_path/"libraries")
    executor,adapters,workers,_=chain(tmp_path/"run")
    workers[Role.ART_PRODUCER].replies=[search_turn(),proposal(asset(source="kenney",source_path="pack/player.png",
        generation_prompt=None,layout={"kind":"source_atlas","mode":"unresolved"})),
        {"action":"submit", "status":"blocked", "summary":"No reliable atlas regions are available in this offline fixture",
         "blockers":[{"description":"Obtain actual atlas metadata before publishing display frames"}]}]
    from types import MappingProxyType
    executor.services_by_role=MappingProxyType({**executor.services_by_role,
        Role.ART_PRODUCER:replace(executor.services_by_role[Role.ART_PRODUCER],libraries=libraries)})
    adapters[Role.ART_PRODUCER]=ArtProducer(ArtProducerConfig(normalize_assets=True, max_quality_repair_rounds=2))
    adapters[Role.DEVELOPER]=Developer(DeveloperConfig(prompt_version="developer.v3",require_normalized_assets=True))
    state=asyncio.run(FixedLoop(executor,adapters,FixedLoopConfig(max_cycles=1)).run())
    assert state.game_revisions
    assert state.assets and not any(r.payload.result.error for r in state.ledger.events
        if getattr(r.payload, "kind", None) == "role_call_finished")
    qa=chain_report(executor,"qa-report",QAReport)
    assert not qa.gates["final_art"]


@pytest.mark.parametrize("mode", ["grid","regions"])
def test_generated_layout_reaches_image_provider_without_equal_grid_assumption(tmp_path,mode):
    from test_image_provider import provider, image_response, generate, job
    layout = AssetLayout(kind="sprite_sheet",mode="grid",cell_width=15,cell_height=14,
                         margin_x=1,margin_y=1,spacing_x=1) if mode=="grid" else AssetLayout(
        kind="sprite_sheet",mode="regions",regions=(FrameRegion(name="idle",x=0,y=0,width=12,height=14),
                                                     FrameRegion(name="run",x=17,y=2,width=16,height=12)))
    captured=[]
    def response(request):
        captured.append(json.loads(request.content))
        return image_response()
    service=provider(tmp_path,response)
    # 33 is deliberately not divisible by columns=2. Explicit geometry is authoritative.
    output=generate(service,job(width=33,layout=layout))
    assert Image.open(io.BytesIO(output)).size==(128,64)
    expected = '"x": 1' if mode=="grid" else '"x": 17'
    assert expected in captured[0]["prompt"]
    assert "equally sized cells" not in captured[0]["prompt"]


def test_dashscope_native_payload_and_mock_both_honor_explicit_margins(tmp_path):
    from test_dashscope_image import provider, sdk_response
    from test_image_provider import generate, job
    from adaptive_game_generation.providers.mock_image import MockImageProvider
    request=job(width=33,layout=AssetLayout(kind="sprite_sheet",mode="grid",cell_width=15,cell_height=14,
                                         margin_x=1,margin_y=1,spacing_x=1))
    captured=[]
    async def call(**kwargs):
        captured.append(kwargs)
        return sdk_response()
    generate(provider(tmp_path,call),request)
    assert '"x": 17' in captured[0]["messages"][0]["content"][0]["text"]
    data=asyncio.run(MockImageProvider().generate(request.model_dump_json(),timeout=10))
    image=Image.open(io.BytesIO(data))
    assert image.getpixel((0,0))[3]==0 and image.getpixel((1,1))[3]==255 and image.getpixel((16,1))[3]==0


@pytest.mark.parametrize("controller", ["fixed_loop","adaptive_scheduler"])
def test_both_full_controllers_share_normalized_art_player_replay_and_qa(tmp_path,controller):
    from adaptive_game_generation.controllers import FixedLoop, FixedLoopConfig, AdaptiveScheduler, SchedulerConfig
    from adaptive_game_generation.controllers.results import export_run, write_json
    from adaptive_game_generation.roles.player import PlayerReport
    from adaptive_game_generation.roles.qa_reviewer import QAReport
    from test_fixed_loop import make_run as chain, report as chain_report
    from test_scheduler import ScriptedScheduler
    provider=ScriptedScheduler(("designer","art_producer","developer","player","qa_reviewer"))
    executor,adapters,workers,_=chain(tmp_path,controller=controller,scheduler_provider=provider)
    workers[Role.ART_PRODUCER].replies=[proposal(sheet())]
    adapters[Role.ART_PRODUCER]=ArtProducer(ArtProducerConfig(normalize_assets=True))
    adapters[Role.DEVELOPER]=Developer(DeveloperConfig(prompt_version="developer.v3",require_normalized_assets=True))
    responses={r:[] for r in workers}
    for role,worker in workers.items():
        original=worker.generate
        async def recorded(*args,_role=role,_generate=original,**kwargs):
            reply=await _generate(*args,**kwargs)
            responses[_role].append(reply.text)
            return reply
        worker.generate=recorded
    runner=FixedLoop(executor,adapters,FixedLoopConfig(max_cycles=1)) if controller=="fixed_loop" else AdaptiveScheduler(executor,adapters,SchedulerConfig(max_role_calls=5))
    state=asyncio.run(runner.run())
    dev=chain_report(executor,"developer-report",DeveloperReport)
    player=chain_report(executor,"player-report",PlayerReport)
    qa=chain_report(executor,"qa-report",QAReport)
    assert dev.asset_packages and len(dev.integrated_assets)==2
    assert player.comparisons and all(c.status=="passed" for c in player.comparisons)
    assert not qa.gates["final_art"] and not state.readiness.ready
    assert all(a.target_path.startswith("assets/normalized/") for a in dev.integrated_assets)
    assert not (executor.workspace.read_public(dev.project_ref.path)/"assets/player.png").exists()
    assert "HIDDEN-LOOP" not in str([p.prompts for p in workers.values()])
    destination=os.environ.get("ASSET_PIPELINE_REVIEW_ROOT")
    if destination:
        output=Path(destination)/controller
        output.mkdir(parents=True,exist_ok=False)
        export_run(executor,output,mode="offline-scripted-models-and-engine/mock-images/asset-normalization")
        shutil.copytree(executor.workspace.root,output/"workspace")
        write_json(output/"offline_model_io.json",{r.value:{"inputs":w.prompts,"outputs":[json.loads(v) for v in responses[r]]} for r,w in workers.items()})
        write_json(output/"offline_scheduler_io.json",{"inputs":provider.prompts,"outputs":provider.responses})
