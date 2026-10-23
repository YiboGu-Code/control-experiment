"""Native SDK tests with intercepted HTTP transport; no online model calls."""

import asyncio
import json
import pytest

dashscope = pytest.importorskip("dashscope")
httpx = pytest.importorskip("httpx")
aiohttp = pytest.importorskip("aiohttp")

from adaptive_game_generation.providers import (
    DashScopeImageConfig, DashScopeImageProvider, GatewayConfig, ImageGatewayConfig, image_config_from_env,
)
from adaptive_game_generation.providers.openai_compatible import ProviderFailure
from test_image_provider import generate, image_bytes, job


def config(**changes):
    return DashScopeImageConfig(model="wan2.7-image", api_key="SECRET-NATIVE-KEY",
        base_url="https://native.test/api/v1", **changes)


def sdk_response(*, status=200, content=None, **extra):
    return {"status_code": status, "request_id": "native-test",
            "output": {"choices": [{"message": {"role": "assistant", "content": content if content is not None else [
                {"type": "image", "image": "https://cdn.test/image.png?sign=DOWNLOAD-SECRET"}]}}]},
            "usage": {"image_count": 1}, **extra}


def provider(tmp_path, call, *, settings=None):
    def download(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, content=image_bytes())
    return DashScopeImageProvider(settings or config(), capture_directory=tmp_path / "image_calls",
        sdk_call=call, download_client=httpx.AsyncClient(transport=httpx.MockTransport(download)))


@pytest.mark.parametrize("host", ["dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com",
                                 "space.cn-beijing.maas.aliyuncs.com", "space.ap-southeast-1.maas.aliyuncs.com"])
def test_env_preserves_region_and_text_chat_config(tmp_path, host):
    env = tmp_path / ".env"
    original = f"MODEL_GATEWAY_BASE_URL=https://{host}/compatible-mode/v1\nMODEL_GATEWAY_API_KEY=SHARED-KEY\nART_IMAGE_MODEL=wan2.7-image\nDESIGNER_MODEL=chat-fixture\n"
    env.write_text(original)
    chat_before = GatewayConfig.from_env(env, environ={})
    selected = image_config_from_env(env, environ={})
    assert isinstance(selected, DashScopeImageConfig)
    assert selected.base_url.get_secret_value() == f"https://{host}/api/v1"
    assert selected.api_key.get_secret_value() == "SHARED-KEY"
    assert selected.public_settings()["adapter"] == "dashscope-multimodal-images.v1"
    assert env.read_text() == original
    assert GatewayConfig.from_env(env, environ={}) == chat_before
    assert chat_before.base_url.get_secret_value().endswith("/compatible-mode/v1")


def test_separate_native_credentials_and_settings_override_shared_chat(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MODEL_GATEWAY_BASE_URL=https://text.test/v1\nMODEL_GATEWAY_API_KEY=SHARED\n"
                   "ART_IMAGE_MODEL=qwen-image-3.0-pro\nDASHSCOPE_API_KEY=NATIVE-SECRET\n"
                   "DASHSCOPE_BASE_HTTP_API_URL=https://native.test/api/v1\n")
    selected = image_config_from_env(env, environ={})
    assert selected.api_key.get_secret_value() == "NATIVE-SECRET"
    overrides = {"ART_IMAGE_BASE_URL": "https://override.test/api/v1", "ART_IMAGE_API_KEY": "OVERRIDE",
                 "ART_IMAGE_SIZE": "1536x1024", "ART_IMAGE_SEED": "123",
                 "ART_IMAGE_PROMPT_EXTEND": "false", "ART_IMAGE_WATERMARK": "false"}
    selected = image_config_from_env(env, environ=overrides)
    assert selected.api_key.get_secret_value() == "OVERRIDE"
    assert selected.base_url.get_secret_value() == "https://override.test/api/v1"
    assert selected.size == "1536*1024" and selected.seed == 123 and selected.prompt_extend is False
    public = json.dumps(selected.public_settings()) + repr(selected)
    assert "OVERRIDE" not in public and "override.test" not in public
    assert selected.configuration_id == selected.revised(api_key="ROTATED").configuration_id
    assert selected.configuration_id != selected.revised(seed=124).configuration_id


@pytest.mark.parametrize("field,value", [
    ("ART_IMAGE_BASE_URL", "https://third-party.test/compatible-mode/v1"),
    ("ART_IMAGE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"),
    ("ART_IMAGE_RESPONSE_FORMAT", "b64_json"), ("ART_IMAGE_QUALITY", "high"),
    ("ART_IMAGE_TRANSPARENCY", "native"), ("ART_IMAGE_SIZE", "auto"),
    ("ART_IMAGE_SEED", "-1"), ("ART_IMAGE_PROMPT_EXTEND", "maybe"), ("ART_IMAGE_PROVIDER", "unknown"),
])
def test_native_config_rejects_unsupported_options_before_network(tmp_path, field, value):
    env = tmp_path / ".env"
    env.write_text("ART_IMAGE_MODEL=wan2.7-image\nDASHSCOPE_API_KEY=SECRET\n")
    with pytest.raises(ProviderFailure):
        image_config_from_env(env, environ={field: value})


def test_openai_images_remain_explicit_opt_in(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ART_IMAGE_PROVIDER=openai\nART_IMAGE_MODEL=other-image\n"
                   "MODEL_GATEWAY_API_KEY=SECRET\nMODEL_GATEWAY_BASE_URL=https://other.test/v1\n")
    selected = image_config_from_env(env, environ={})
    assert type(selected) is ImageGatewayConfig
    assert selected.base_url.get_secret_value() == "https://other.test/v1"


@pytest.mark.parametrize("model", ["wan2.7-image", "qwen-image-3.0-pro"])
def test_actual_dashscope_sdk_serializes_native_body_and_isolates_globals(tmp_path, monkeypatch, model):
    """Execute the installed SDK; replace only aiohttp's actual network send."""
    requests, sessions = [], []
    before = (dashscope.api_key, dashscope.base_http_api_url)
    class Response:
        status = 200
        content_type = "application/json"
        headers = {}
        async def json(self):
            response = sdk_response()
            return {k: v for k, v in response.items() if k != "status_code"}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    async def request(session, method, url, **kwargs):
        requests.append({"method": method, "url": url, **kwargs})
        sessions.append(session)
        return Response()
    monkeypatch.setattr(aiohttp.ClientSession, "_request", request)
    selected = config().revised(model=model)
    # sdk_call omitted: this uses the real AioMultiModalConversation.call.
    service = DashScopeImageProvider(selected, capture_directory=tmp_path / "image_calls",
        download_client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=image_bytes()))))
    result = generate(service)
    assert len(requests) == 1 and requests[0]["method"] == "POST"
    assert requests[0]["url"] == "https://native.test/api/v1/services/aigc/multimodal-generation/generation"
    body = requests[0]["json"]
    assert body["model"] == model and body["parameters"]["n"] == 1
    assert body["parameters"]["size"] == "1024*1024" and body["parameters"]["stream"] is False
    assert body["parameters"]["watermark"] is False
    if model.startswith("wan2.7"):
        assert "prompt_extend" not in body["parameters"]
    else:
        assert body["parameters"]["prompt_extend"] is False
    image_prompt = body["input"]["messages"][0]["content"][0]["text"]
    assert job().prompt in image_prompt and image_prompt.startswith("ASSET CAPABILITY:")
    for name in ("prompt", "response_format", "background", "quality", "output_format"):
        assert name not in body["parameters"] and name not in body
    assert requests[0]["headers"]["Authorization"] == "Bearer SECRET-NATIVE-KEY"
    assert sessions[0].closed and sessions[0].timeout.total == 20
    assert (dashscope.api_key, dashscope.base_http_api_url) == before
    folder = tmp_path / "image_calls/001"
    assert json.loads((folder / "request.json").read_bytes()) == body
    assert (folder / "output.png").read_bytes() == result
    saved = json.loads((folder / "response.json").read_bytes())
    assert saved["request_id"] == "native-test" and saved["usage"]["image_count"] == 1
    serialized = "".join(p.read_text(encoding="utf-8") for p in folder.glob("*.json"))
    assert "SECRET-NATIVE-KEY" not in serialized and "DOWNLOAD-SECRET" not in serialized
    assert service.calls[0]["response_encoding"] == "dashscope_image_url"


def test_native_optional_parameters_and_sdk_deadline(tmp_path):
    calls = []
    async def call(**kwargs):
        calls.append(kwargs)
        return sdk_response()
    service = provider(tmp_path, call, settings=config(prompt_extend=False, seed=42, size="2K", timeout_seconds=9.0).revised(model="qwen-image-3.0-pro"))
    generate(service)
    assert calls[0]["request_timeout"] == 9 and calls[0]["prompt_extend"] is False
    assert calls[0]["seed"] == 42 and calls[0]["size"] == "2K"
    assert "max_retries" not in calls[0]  # Not a native SDK model parameter.


@pytest.mark.parametrize("response", [
    sdk_response(status=400, code="InvalidParameter", message="SECRET-NATIVE-KEY at https://native.test/api/v1"),
    sdk_response(content=[{"text": "No image was produced"}]),
    sdk_response(content=[{"image": "https://cdn.test/1"}, {"image": "https://cdn.test/2"}]),
    sdk_response(content=[{"image": None}]),
    {"status_code": 200, "output": {"task_id": "unsupported-async-task"}},
    {"status_code": 200, "output": {"choices": [{"message": {"content": "invalid"}}]}},
])
def test_native_errors_are_saved_without_retry_or_fallback(tmp_path, response):
    calls = []
    async def call(**kwargs):
        calls.append(kwargs)
        return response
    service = provider(tmp_path, call)
    with pytest.raises(ProviderFailure):
        generate(service)
    assert len(calls) == 1 and service.calls[0]["status"] == "failed"
    folder = tmp_path / "image_calls/001"
    assert (folder / "response.json").exists() and not (folder / "output.png").exists()
    assert "SECRET-NATIVE-KEY" not in (folder / "response.json").read_text(encoding="utf-8")


def test_native_timeout_cancels_sdk_and_preserves_audit(tmp_path):
    stopped = []
    async def call(**kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            stopped.append(True)
    service = provider(tmp_path, call)
    with pytest.raises(ProviderFailure, match="timed out"):
        generate(service, timeout=0.02)
    assert stopped == [True] and service.calls[0]["error_code"] == "provider_timeout"


def test_native_cancellation_preserves_audit(tmp_path):
    async def call(**kwargs):
        raise asyncio.CancelledError()
    service = provider(tmp_path, call)
    with pytest.raises(asyncio.CancelledError):
        generate(service)
    assert service.calls[0]["status"] == "cancelled"


def test_native_image_integrates_real_art_and_developer_with_shared_budget(tmp_path):
    from adaptive_game_generation.controllers import FixedLoop, FixedLoopConfig
    from adaptive_game_generation.roles.art_producer import ArtManifest
    from test_fixed_loop import make_run, report

    async def call(**kwargs):
        return sdk_response()
    service = provider(tmp_path, call)
    executor, adapters, _, _ = make_run(tmp_path, image_service=service)
    async def run():
        async with service:
            return await FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=1)).run()
    state = asyncio.run(run())
    art = report(executor, "art-manifest", ArtManifest)
    assert not art.assets[0].is_mock and state.game_revisions
    assert state.ledger.usage.image_calls == 1 and len(service.calls) == 1
    assert art.assets[0].record.artifact_ref.sha256 == service.calls[0]["sha256"]
