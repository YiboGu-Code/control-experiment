"""Offline transport tests: no live image, text-model or Docker invocation."""

import asyncio
import base64
import hashlib
import io
import json

import pytest
from PIL import Image, ImageDraw

httpx = pytest.importorskip("httpx")
pytest.importorskip("dotenv")

from adaptive_game_generation.providers import ImageGatewayConfig, OpenAICompatibleImageProvider
from adaptive_game_generation.providers.openai_compatible import ProviderFailure
from adaptive_game_generation.providers import openai_image
from adaptive_game_generation.roles.art_producer import ImageJob
from test_designer import INSTRUCTION


def config(**changes):
    return ImageGatewayConfig(model="fixture-image-model", api_key="SECRET-IMAGE-KEY",
        base_url="https://image-gateway.test/v1", **changes)


def job(**changes):
    return ImageJob(prompt="Game asset for the public Benchmark task:\n" + INSTRUCTION.read_text(encoding="utf-8"),
        width=32, height=16, transparent_background=True, columns=2, rows=1,
        frame_names=("idle", "run")).revised(**changes)


def image_bytes(*, native=False, format="PNG"):
    image = Image.new("RGBA" if native else "RGB", (128, 64), (0, 0, 0, 0) if native else (255, 0, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((16, 16, 47, 47), fill=(10, 100, 200, 255) if native else (10, 100, 200))
    draw.rectangle((80, 16, 111, 47), fill=(10, 100, 200, 255) if native else (10, 100, 200))
    output = io.BytesIO()
    image.save(output, format=format)
    return output.getvalue()


def image_response(data=None, **fields):
    return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(
        data if data is not None else image_bytes()).decode(), **fields}]})


def provider(tmp_path, handler, *, settings=None, download=None):
    def forbid_download(request):
        raise AssertionError("Unexpected image download")
    return OpenAICompatibleImageProvider(settings or config(), capture_directory=tmp_path / "image_calls",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        download_client=httpx.AsyncClient(transport=httpx.MockTransport(download or forbid_download)))


def generate(service, request=None, *, timeout=20):
    async def run():
        async with service:
            return await service.generate((request or job()).model_dump_json(), timeout=timeout)
    return asyncio.run(run())


def test_image_config_env_overrides_shared_gateway_and_never_exports_secrets(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ART_IMAGE_MODEL=file-image\nMODEL_GATEWAY_API_KEY=SHARED-SECRET\n"
        "MODEL_GATEWAY_BASE_URL=https://shared.test/v1\n", encoding="utf-8")
    shared = ImageGatewayConfig.from_env(env, environ={})
    assert shared.api_key.get_secret_value() == "SHARED-SECRET"
    assert shared.base_url.get_secret_value() == "https://shared.test/v1"
    selected = ImageGatewayConfig.from_env(env, environ={
        "ART_IMAGE_MODEL": "process-image", "ART_IMAGE_API_KEY": "IMAGE-SECRET",
        "ART_IMAGE_BASE_URL": "https://images.test/v1", "ART_IMAGE_TIMEOUT_SECONDS": "700",
        "ART_IMAGE_TRANSPARENCY": "native", "ART_IMAGE_SIZE": "1536x1024",
        "ART_IMAGE_RESPONSE_FORMAT": "url", "ART_IMAGE_RESAMPLE": "lanczos"})
    assert selected.model == "process-image" and selected.timeout_seconds == 700
    assert selected.api_key.get_secret_value() == "IMAGE-SECRET"
    assert selected.base_url.get_secret_value() == "https://images.test/v1"
    public = repr(selected) + json.dumps(selected.public_settings())
    assert "SECRET" not in public and "images.test" not in public
    assert not selected.info.is_mock
    assert selected.configuration_id == selected.revised(api_key="ROTATED").configuration_id
    assert selected.configuration_id != selected.revised(base_url="https://different.test/v1").configuration_id
    assert selected.configuration_id != selected.revised(size="1024x1024").configuration_id


@pytest.mark.parametrize("overrides", [
    {"ART_IMAGE_MODEL": ""}, {"ART_IMAGE_TRANSPARENCY": "pretend-alpha"},
    {"ART_IMAGE_TIMEOUT_SECONDS": "nan"}, {"ART_IMAGE_TIMEOUT_SECONDS": "0"},
    {"ART_IMAGE_CHROMA_TOLERANCE": "999"}, {"ART_IMAGE_SIZE": "invalid"},
    {"ART_IMAGE_BASE_URL": "file:///private"}, {"ART_IMAGE_BASE_URL": "https://user:pass@images.test/v1"},
])
def test_invalid_configuration_fails_locally_without_secret_echo(tmp_path, overrides):
    env = tmp_path / ".env"
    env.write_text("ART_IMAGE_MODEL=image\nMODEL_GATEWAY_API_KEY=PRIVATE-KEY\n"
        "MODEL_GATEWAY_BASE_URL=https://private.test/v1\n")
    with pytest.raises(ProviderFailure) as caught:
        ImageGatewayConfig.from_env(env, environ=overrides)
    assert "PRIVATE-KEY" not in str(caught.value) and "private.test" not in str(caught.value)


def test_base64_request_is_one_image_with_grid_and_exact_processed_png(tmp_path):
    requests = []
    source = image_bytes()
    def handle(request):
        requests.append(request)
        return image_response(source)
    service = provider(tmp_path, handle)
    result = generate(service)
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://image-gateway.test/v1/images/generations"
    assert request.headers["authorization"] == "Bearer SECRET-IMAGE-KEY"
    body = json.loads(request.content)
    assert body["model"] == "fixture-image-model" and body["n"] == 1 and body["size"] == "1024x1024"
    assert "response_format" not in body and "background" not in body
    assert INSTRUCTION.read_text(encoding="utf-8") in body["prompt"]
    assert "2 columns and 1 rows" in body["prompt"] and "#FF00FF" in body["prompt"]
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (128, 64) and image.format == "PNG"
        assert image.getpixel((0, 0))[3] == 0 and image.getpixel((32, 32))[3] == 255
    capture = tmp_path / "image_calls/001"
    assert (capture / "source.png").read_bytes() == source
    assert (capture / "output.png").read_bytes() == result
    metadata = json.loads((capture / "metadata.json").read_bytes())
    assert metadata["status"] == "completed" and not metadata["provider"]["is_mock"]
    assert metadata["sha256"] == hashlib.sha256(result).hexdigest()
    assert [step["operation"] for step in metadata["normalization"]["operations"]] == ["chroma_key", "frame_native", "frame_native"]
    assert all("SECRET-IMAGE-KEY" not in p.read_text(encoding="utf-8") for p in capture.glob("*.json"))


def test_native_alpha_and_optional_wire_parameters(tmp_path):
    payloads = []
    def handle(request):
        payloads.append(json.loads(request.content))
        return image_response(image_bytes(native=True))
    service = provider(tmp_path, handle, settings=config(transparency="native", response_format="b64_json", quality="high"))
    generate(service)
    assert payloads[0]["background"] == "transparent" and payloads[0]["output_format"] == "png"
    assert payloads[0]["response_format"] == "b64_json" and payloads[0]["quality"] == "high"
    assert [s["operation"] for s in service.calls[0]["normalization"]["operations"]] == ["preserve_alpha", "frame_native", "frame_native"]


@pytest.mark.parametrize("background", [(255, 0, 255, 254), (219, 75, 224, 254)])
def test_nearly_opaque_alpha_does_not_bypass_chroma_removal(tmp_path, background):
    # Run permissions-002 had alpha 253..255 and corners near (219,75,224).
    original = Image.new("RGBA", (128, 64), background)
    ImageDraw.Draw(original).rectangle((16, 16, 47, 47), fill=(10, 100, 200, 128))
    stream = io.BytesIO()
    original.save(stream, format="PNG")
    service = provider(tmp_path, lambda r: image_response(stream.getvalue()))
    result = Image.open(io.BytesIO(generate(service)))
    assert result.getpixel((0, 0))[3] == 0
    assert result.getpixel((32, 32)) == (10, 100, 200, 128)
    operation = service.calls[0]["normalization"]["operations"][0]
    assert operation["operation"] == "chroma_key"
    if background[:3] != (255, 0, 255):
        assert "#DB4BE0" in operation["keys"]
    assert (tmp_path / "image_calls/001/source.png").read_bytes() == stream.getvalue()


@pytest.mark.parametrize("native", [True, False])
def test_near_opaque_non_key_image_is_not_usable_transparency(tmp_path, native):
    stream = io.BytesIO()
    Image.new("RGBA", (128, 64), (10, 100, 200, 254)).save(stream, format="PNG")
    service = provider(tmp_path, lambda r: image_response(stream.getvalue()),
                       settings=config(transparency="native" if native else "chroma_key"))
    with pytest.raises(ProviderFailure, match="no usable transparency"):
        generate(service)


def test_native_transparency_preserves_magenta_subjects(tmp_path):
    original = Image.new("RGBA", (128, 64), (0, 0, 0, 0))
    ImageDraw.Draw(original).rectangle((16, 16, 47, 47), fill=(255, 0, 255, 128))
    stream = io.BytesIO()
    original.save(stream, format="PNG")
    service = provider(tmp_path, lambda r: image_response(stream.getvalue()), settings=config(transparency="native"))
    image = Image.open(io.BytesIO(generate(service)))
    assert image.getpixel((32, 32)) == (255, 0, 255, 128)


def test_border_sampling_preserves_one_dirty_pixel_for_quality_review(tmp_path):
    original = Image.new("RGBA", (128, 64), (219, 75, 224, 254))
    original.putpixel((0, 0), (10, 100, 200, 254))
    stream = io.BytesIO()
    original.save(stream, format="PNG")
    service = provider(tmp_path, lambda r: image_response(stream.getvalue()))
    result = Image.open(io.BytesIO(generate(service)))
    assert result.size == original.size and result.getpixel((0, 0)) == original.getpixel((0, 0))
    assert service.calls[0]["normalization"]["findings"]  # Empty actor cells are quarantined, never downsampled away.


def test_image_url_download_redirect_has_no_gateway_auth_and_is_redacted(tmp_path):
    downloaded = []
    def download(request):
        downloaded.append(request)
        assert "authorization" not in request.headers
        if len(downloaded) == 1:
            return httpx.Response(302, headers={"location": "https://cdn.test/final?token=URL-SECRET"})
        return httpx.Response(200, content=image_bytes(format="JPEG"))
    service = provider(tmp_path, lambda request: httpx.Response(200, json={"data": [
        {"url": "https://cdn.test/first?token=URL-SECRET", "revised_prompt": "Returned image"}]}), download=download)
    result = generate(service, job(transparent_background=False))
    assert len(downloaded) == 2
    assert (tmp_path / "image_calls/001/source.jpg").exists()
    assert Image.open(io.BytesIO(result)).mode == "RGB"
    captured = (tmp_path / "image_calls/001/response.json").read_text(encoding="utf-8")
    assert "URL-SECRET" not in captured and "cdn.test" not in captured


@pytest.mark.parametrize("url", ["file:///private", "http://cdn.test/a", "https://127.0.0.1/a",
                              "https://169.254.169.254/a", "https://localhost/a", "https://user:pass@cdn.test/a"])
def test_unsafe_download_is_rejected_without_fetch(tmp_path, url):
    service = provider(tmp_path, lambda r: httpx.Response(200, json={"data": [{"url": url}]}))
    with pytest.raises(ProviderFailure, match="unsafe download"):
        generate(service)
    assert service.calls[0]["status"] == "failed"


@pytest.mark.parametrize("response", [
    {"data": []}, {"data": [{}, {}]}, {"data": [{"b64_json": "invalid!"}]},
    {"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]},
    {"output": {"task_id": "unsupported-async-task"}}, {"data": [{}]},
])
def test_malformed_responses_fail_without_retry_or_mock(tmp_path, response):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=response)
    service = provider(tmp_path, handle)
    with pytest.raises(ProviderFailure):
        generate(service)
    assert len(requests) == 1 and service.calls[0]["status"] == "failed"
    assert not (tmp_path / "image_calls/001/output.png").exists()


def test_http_failure_redacts_echoed_secret_and_has_no_retry(tmp_path):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(429, json={"error": "SECRET-IMAGE-KEY at https://image-gateway.test/v1"})
    service = provider(tmp_path, handle)
    with pytest.raises(ProviderFailure, match="HTTP 429") as caught:
        generate(service)
    assert len(requests) == 1 and "SECRET" not in str(caught.value)
    for path in (tmp_path / "image_calls/001").glob("*.json"):
        assert "SECRET-IMAGE-KEY" not in path.read_text(encoding="utf-8")
        assert "image-gateway.test" not in path.read_text(encoding="utf-8")
    assert service.calls[0]["http_status"] == 429


@pytest.mark.parametrize("native", [True, False])
def test_no_fake_transparency_is_added_to_opaque_output(tmp_path, native):
    data = io.BytesIO()
    Image.new("RGB", (64, 64), (0, 100, 200)).save(data, format="PNG")
    service = provider(tmp_path, lambda r: image_response(data.getvalue()),
                       settings=config(transparency="native" if native else "chroma_key"))
    with pytest.raises(ProviderFailure, match="no usable transparency"):
        generate(service)
    assert (tmp_path / "image_calls/001/source.png").exists()
    assert not (tmp_path / "image_calls/001/output.png").exists()


def test_http_body_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(openai_image, "MAX_RESPONSE_BYTES", 20)
    service = provider(tmp_path, lambda r: httpx.Response(200, content=b"a" * 21))
    with pytest.raises(ProviderFailure, match="byte limit"):
        generate(service)


def test_whole_operation_timeout_is_captured(tmp_path):
    async def slow(request):
        await asyncio.sleep(1)
        return image_response()
    service = provider(tmp_path, slow)
    with pytest.raises(ProviderFailure, match="timed out"):
        generate(service, timeout=0.02)
    assert service.calls[0]["error_code"] == "provider_timeout"


def test_cancellation_is_saved_without_retry(tmp_path):
    async def cancelled(request):
        raise asyncio.CancelledError()
    service = provider(tmp_path, cancelled)
    with pytest.raises(asyncio.CancelledError):
        generate(service)
    metadata = json.loads((tmp_path / "image_calls/001/metadata.json").read_bytes())
    assert metadata["status"] == "cancelled" and "finished_at" in metadata
