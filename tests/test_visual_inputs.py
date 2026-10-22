"""Offline image transport, screenshot provenance and visual-review gates."""

import asyncio
import base64
import hashlib
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from adaptive_game_generation.models import ArtifactRef, ResourceLimits
from adaptive_game_generation.providers import OpenAICompatibleTextProvider
from adaptive_game_generation.runtime import Executor, Services, TextReply, InvalidSubmission
from adaptive_game_generation.runtime.tools import ModelImage
from adaptive_game_generation.roles.qa_reviewer import QAConfig, QAReviewer, QAReport
from adaptive_game_generation.roles.qa_reviewer.contracts import CriterionReview
from adaptive_game_generation.roles.qa_reviewer.role import validate_criterion
from adaptive_game_generation.roles.visual import select_screenshots
from test_provider import settings, completion
from test_runtime import make_run, invoke, result
from test_qa_reviewer import setup_executor, request, conservative_proposal


def png():
    data = io.BytesIO()
    Image.new("RGB", (1280, 720), (12, 24, 48)).save(data, format="PNG")
    return data.getvalue()


def test_image_pixels_and_labels_reach_chat_api_with_budget_bound(tmp_path):
    data = png()
    image = ModelImage("image:frame:v1", data, "image/png")
    def handle(request):
        body = json.loads(request.content)
        parts = body["messages"][1]["content"]
        assert parts[0] == {"type": "text", "text": "Inspect the supplied image; return English JSON."}
        assert parts[1]["text"].endswith(image.source_id)
        assert base64.b64decode(parts[2]["image_url"]["url"].split(",", 1)[1]) == data
        return httpx.Response(200, json=completion())
    async def run():
        async with OpenAICompatibleTextProvider(settings(vision=True), capture_directory=tmp_path / "calls",
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))) as provider:
            prompt = "Inspect the supplied image; return English JSON."
            assert provider.input_token_upper_bound(prompt, images=(image,)) > len(data) + 65536
            reply = await provider.generate(prompt, images=(image,), max_output_tokens=128, timeout=5)
            assert reply.output_tokens == 20
    asyncio.run(run())
    assert json.loads((tmp_path / "calls/001/request.json").read_bytes())["messages"][1]["content"][2]["type"] == "image_url"


@pytest.mark.parametrize("fault", [None, "checksum", "non_image", "private"])
def test_executor_checks_public_image_bytes_before_model_dispatch(tmp_path, fault):
    class Vision:
        calls = 0
        def input_token_upper_bound(self, prompt, *, images):
            assert len(images) == 1
            return 70000
        async def generate(self, prompt, *, images, **kwargs):
            self.calls += 1
            assert images[0].data == png()
            return TextReply("{}", 2000, 10)
    provider = Vision()
    executor = make_run(tmp_path, services=Services(text=provider),
                        budget=ResourceLimits(max_model_calls=2, max_input_tokens=200000, max_output_tokens=1000))
    async def inspect(context):
        data = b"not an image" if fault == "non_image" else png()
        path = await context.tools.write_bytes("frame.png", data)
        ref = ArtifactRef(artifact_id="frame", kind="screenshot", path=path, created_by="player",
                          sha256="0" * 64 if fault == "checksum" else hashlib.sha256(data).hexdigest())
        if fault == "private":
            ref = ref.revised(path="private/secret.png")
        await context.tools.generate_text("Inspect frame", image_refs=(ref,), max_output_tokens=100)
        return result(context, artifact_refs=(ref,))
    outcome = invoke(executor, "player", inspect)
    if fault:
        assert outcome.status == "failed" and provider.calls == 0
        assert executor.state.ledger.usage.model_calls == 0
    else:
        assert outcome.status == "completed", outcome.error
        assert provider.calls == 1
        assert executor.state.ledger.usage.input_tokens == 2000


def test_selection_diversifies_capture_sessions_deduplicates_and_bounds():
    def frame(n, digest):
        return SimpleNamespace(path=f"frames/{n:04}.png", sha256=digest)
    a, b, c = frame(0, "a"), frame(59, "b"), frame(99, "c")
    sessions = [SimpleNamespace(mode="capture", status="passed", screenshot_refs=(a, b)),
                SimpleNamespace(mode="capture", status="passed", screenshot_refs=(a, c)),
                SimpleNamespace(mode="replay", status="passed", screenshot_refs=(a, c))]
    assert select_screenshots(sessions, 2) == (b, c)
    assert select_screenshots(sessions, 8) == (b, c, a)
    assert select_screenshots(sessions, 0) == ()


def test_visual_verdict_cannot_mix_seen_and_unseen_screenshot_citations():
    reader = SimpleNamespace(
        evidence={"capture": SimpleNamespace(requirement_ids=("R001",), kind="playthrough")},
        sources={identity: {"source_id": identity, "kind": "screenshot", "evidence_ids": ["capture"]}
                 for identity in ("image:seen", "image:unseen")},
        visual_source_ids={"image:seen"},
    )
    criterion = CriterionReview(requirement_id="R001", criterion_index=0, verdict="fail", basis="visual",
        rationale="A supplied frame cannot justify claims about an unseen frame.",
        evidence_ids=("capture",), source_ids=("image:seen", "image:unseen"))
    with pytest.raises(InvalidSubmission, match="actually supplied"):
        validate_criterion(criterion, reader)


def test_qa_receives_real_recorded_screenshots_and_cites_only_supplied_pixels(tmp_path):
    class Vision:
        seen = None
        def input_token_upper_bound(self, prompt, *, images):
            return len(prompt.encode()) + 65536 * len(images)
        async def generate(self, prompt, *, images, **kwargs):
            inputs = json.loads(prompt)["input"]
            self.seen = inputs
            assert 0 < len(images) <= 2
            assert {i.source_id for i in images} == set(inputs["visual_source_ids"])
            assert all(Image.open(io.BytesIO(i.data)).size == (1280, 720) for i in images)
            raw = conservative_proposal(inputs)
            source = next(s for s in inputs["sources"] if s["source_id"] in inputs["visual_source_ids"])
            evidence = next(e for e in inputs["evidence"] if e["evidence_id"] in source["evidence_ids"]
                            and e["requirement_ids"])
            criterion = next(c for c in raw["criteria"] if c["requirement_id"] in evidence["requirement_ids"])
            criterion.update(basis="visual", verdict="fail", evidence_ids=[evidence["evidence_id"]],
                             source_ids=[source["source_id"]], rationale="Synthetic visual-review decision to test provenance gates.")
            return TextReply(json.dumps(raw), 2000, 1000)
    provider = Vision()
    async def run():
        executor, workspace = await setup_executor(tmp_path / "workspace", provider)
        outcome = await executor.execute(request(executor), QAReviewer(QAConfig(max_visual_images=2)))
        assert outcome.status == "completed", outcome.error
        ref = next(r for r in outcome.artifact_refs if r.artifact_id == "qa-report")
        return QAReport.model_validate_json(workspace.read_public(ref.path).read_bytes())
    report = asyncio.run(run())
    assert report.evidence_integrity["screenshot_pixels_supplied"] == 2
    assert any(c.basis == "visual" and c.verdict == "fail" for c in report.effective_proposal.criteria)
    assert not report.readiness.ready
    source = next(s for s in provider.seen["sources"] if s["source_id"] in provider.seen["visual_source_ids"])
    ev = next(e for e in provider.seen["evidence"] if e["evidence_id"] in source["evidence_ids"] and e["requirement_ids"])
    from adaptive_game_generation.models import EvidenceRecord
    reader = SimpleNamespace(evidence={ev["evidence_id"]: EvidenceRecord.model_validate(ev)},
        sources={source["source_id"]: source}, visual_source_ids={"image:not-supplied"})
    criterion = CriterionReview(requirement_id=ev["requirement_ids"][0], criterion_index=0,
        verdict="pass", basis="visual", rationale="Unseen pixels cannot pass",
        evidence_ids=(ev["evidence_id"],), source_ids=(source["source_id"],))
    with pytest.raises(InvalidSubmission, match="actually supplied"):
        validate_criterion(criterion, reader)


def test_player_passes_captured_pixels_to_the_next_observation_turn(tmp_path):
    from test_player import make_run as player_run, invoke as player_invoke, plan, FINISH
    from adaptive_game_generation.roles.player import PlayerConfig
    executor, _, backend = player_run(tmp_path, plan(), FINISH)
    class Vision:
        calls = []
        def input_token_upper_bound(self, prompt, **kwargs):
            return len(prompt.encode()) + 65536 * len(kwargs.get("images", ()))
        async def generate(self, prompt, **kwargs):
            images = kwargs.get("images", ())
            self.calls.append(images)
            parsed = json.loads(prompt)
            if len(self.calls) == 1:
                assert not images
                return TextReply(json.dumps(plan()), 1000, 1000)
            assert images and parsed["visual_inputs"]
            assert "has NOT visually inspected" not in parsed["instructions"]
            return TextReply(json.dumps(FINISH), 1000, 1000)
    vision = Vision()
    executor = Executor.open(executor.workspace, services=Services(text=vision, sandbox=backend))
    outcome = player_invoke(executor, PlayerConfig(prompt_version="player.v2", max_visual_images=2))
    assert outcome.status == "completed", outcome.error
    assert len(vision.calls) == 2
