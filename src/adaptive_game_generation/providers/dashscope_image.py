"""DashScope's native multimodal image SDK, isolated from Chat Completions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Mapping
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, StrictBool, field_validator, model_validator

from .openai_compatible import ProviderFailure
from .openai_image import ImageGatewayConfig, OpenAICompatibleImageProvider, MAX_RESPONSE_BYTES


def native_base_url(value: str) -> str:
    """Preserve the region/workspace host; never rewrite a third-party chat URL."""
    parsed = urlsplit(value)
    if parsed.path.rstrip("/") == "/compatible-mode/v1":
        if not parsed.hostname or not parsed.hostname.endswith(".aliyuncs.com"):
            raise ValueError("Set ART_IMAGE_BASE_URL to the native image endpoint")
        parsed = parsed._replace(path="/api/v1")
    if parsed.path.rstrip("/") != "/api/v1":
        raise ValueError("The native image base URL must end in /api/v1")
    return urlunsplit(parsed).rstrip("/")


class DashScopeImageConfig(ImageGatewayConfig):
    size: str = Field(default="1024*1024", pattern=r"^([124]K|[1-9]\d{1,4}\*[1-9]\d{1,4})$")
    # These OpenAI-only switches must not silently reach DashScope parameters.
    response_format: None = None
    quality: None = None
    transparency: Literal["chroma_key"] = "chroma_key"
    prompt_extend: StrictBool | None = None
    watermark: StrictBool = False
    seed: int | None = Field(default=None, ge=0, le=2147483647, strict=True)

    @field_validator("size", mode="before")
    @classmethod
    def native_size(cls, value):
        return value.replace("x", "*") if isinstance(value, str) else value

    @model_validator(mode="after")
    def native_endpoint(self):
        if urlsplit(self.base_url.get_secret_value()).path.rstrip("/") != "/api/v1":
            raise ValueError("DashScope image base URL must end in /api/v1")
        return self

    @classmethod
    def from_env(cls, env_file: Path, *, environ: Mapping[str, str] | None = None):
        from dotenv import dotenv_values

        values = {**dotenv_values(env_file, interpolate=False), **(os.environ if environ is None else environ)}
        def setting(name, default=None):
            return values.get("ART_IMAGE_" + name) or default
        def boolean(name, default=None):
            value = setting(name)
            if value is None:
                return default
            if value.lower() not in ("true", "false"):
                raise ValueError("Invalid boolean")
            return value.lower() == "true"
        key = setting("API_KEY") or values.get("DASHSCOPE_API_KEY") or values.get("MODEL_GATEWAY_API_KEY")
        url = (setting("BASE_URL") or values.get("DASHSCOPE_BASE_HTTP_API_URL")
               or values.get("MODEL_GATEWAY_BASE_URL") or "https://dashscope.aliyuncs.com/api/v1")
        if not setting("MODEL") or not key:
            raise ProviderFailure("Configure ART_IMAGE_MODEL and ART_IMAGE_API_KEY, DASHSCOPE_API_KEY or MODEL_GATEWAY_API_KEY")
        try:
            options = {"license": setting("LICENSE")} if setting("LICENSE") else {}
            return cls(model=setting("MODEL"), api_key=key, base_url=native_base_url(url),
                timeout_seconds=float(setting("TIMEOUT_SECONDS", "600")), size=setting("SIZE", "1024*1024"),
                response_format=setting("RESPONSE_FORMAT"), quality=setting("QUALITY"),
                transparency=setting("TRANSPARENCY", "chroma_key"), resample=setting("RESAMPLE", "nearest"),
                chroma_tolerance=int(setting("CHROMA_TOLERANCE", "40")),
                prompt_extend=boolean("PROMPT_EXTEND"), watermark=boolean("WATERMARK", False),
                seed=int(setting("SEED")) if setting("SEED") is not None else None, **options)
        except (ValueError, TypeError):
            raise ProviderFailure("Invalid DashScope image configuration; check ART_IMAGE_BASE_URL and native parameters in docs/fixed_loop.md") from None

    def public_settings(self):
        settings = super().public_settings()
        settings.update(adapter="dashscope-multimodal-images.v1", sdk_method="AioMultiModalConversation.call",
                        endpoint_path="/services/aigc/multimodal-generation/generation")
        return settings


def require_dashscope_sdk():
    try:
        from dashscope import AioMultiModalConversation
    except ImportError:
        raise ProviderFailure('Install the live dependencies: python -m pip install -e ".[live,test]"') from None
    return AioMultiModalConversation.call


class DashScopeImageProvider(OpenAICompatibleImageProvider):
    """Reuse bounded downloads, normalization and audit; generation uses only SDK.

    AioMultiModalConversation is the cancellable SDK equivalent of the documented
    MultiModalConversation.call. No thread continues a paid request after timeout.
    Explicit per-call base_address/api_key avoid changing SDK process globals.
    """

    def __init__(self, config: DashScopeImageConfig, *, capture_directory: Path,
                 sdk_call=None, download_client=None):
        self._sdk_call = sdk_call or require_dashscope_sdk()
        super().__init__(config, capture_directory=capture_directory, download_client=download_client)

    def _payload(self, job):
        parameters = {"n": 1, "size": self.config.size, "watermark": self.config.watermark,
                      "result_format": "message", "stream": False}
        if self.config.prompt_extend is not None:
            parameters["prompt_extend"] = self.config.prompt_extend
        if self.config.seed is not None:
            parameters["seed"] = self.config.seed
        return {"model": self.config.model,
                "input": {"messages": [{"role": "user", "content": [{"text": self._image_prompt(job)}]}]},
                "parameters": parameters}

    async def _generate(self, payload, job, directory, metadata, timeout):
        import aiohttp

        metadata["sdk_method"] = "AioMultiModalConversation.call"
        try:
            response = await self._sdk_call(
                model=payload["model"], messages=payload["input"]["messages"],
                api_key=self.config.api_key.get_secret_value(),
                base_address=self.config.base_url.get_secret_value(),
                request_timeout=timeout, **payload["parameters"])
        except aiohttp.ClientError:
            raise ProviderFailure("Could not connect to the DashScope image service; no automatic retry was made") from None
        # SDK responses are dict subclasses and include status/request_id/usage.
        if not isinstance(response, dict):
            raise ProviderFailure("DashScope SDK returned an invalid response object")
        if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ProviderFailure("DashScope SDK response exceeded the configured byte limit")
        self._save(directory / "response.json", response)
        status = response.get("status_code")
        if not isinstance(status, int):
            raise ProviderFailure("DashScope SDK response is missing a valid HTTP status")
        metadata["http_status"] = status
        if status != 200:
            raise ProviderFailure(f"DashScope image service returned HTTP {status}; inspect response.json for the service error")
        output = response.get("output")
        choices = output.get("choices") if isinstance(output, dict) else None
        if not isinstance(choices, list):
            raise ProviderFailure("DashScope image response is missing output.choices; asynchronous task responses are not supported")
        urls = []
        for choice in choices:
            message = choice.get("message") if isinstance(choice, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                raise ProviderFailure("DashScope image response has an invalid message.content")
            for item in content:
                if isinstance(item, dict) and "image" in item:
                    if not isinstance(item["image"], str) or not item["image"]:
                        raise ProviderFailure("DashScope image response contains an invalid image URL")
                    urls.append(item["image"])
        if len(urls) != 1:
            raise ProviderFailure("DashScope must return exactly one image for the metered n=1 request")
        metadata["response_encoding"] = "dashscope_image_url"
        source = await self._download(urls[0], timeout)
        return self._store_image(source, job, directory, metadata)


def image_config_from_env(env_file: Path, *, environ: Mapping[str, str] | None = None):
    """Native DashScope is the default; legacy Images API is an explicit choice."""
    from dotenv import dotenv_values

    values = {**dotenv_values(env_file, interpolate=False), **(os.environ if environ is None else environ)}
    selected = values.get("ART_IMAGE_PROVIDER") or "dashscope"
    if selected == "dashscope":
        config = DashScopeImageConfig.from_env(env_file, environ=environ)
        require_dashscope_sdk()  # Fail preflight before any text-model dispatch.
        return config
    if selected == "openai":
        return ImageGatewayConfig.from_env(env_file, environ=environ)
    raise ProviderFailure("ART_IMAGE_PROVIDER must be dashscope or openai")


def create_image_provider(config, *, capture_directory):
    cls = DashScopeImageProvider if isinstance(config, DashScopeImageConfig) else OpenAICompatibleImageProvider
    return cls(config, capture_directory=capture_directory)
