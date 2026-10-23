"""One metered image job per OpenAI-compatible Images API request."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Literal, Mapping
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator

from ..models.base import Model, Seconds, Text, utc_now
from ..roles.art_producer.contracts import ImageJob
from ..runtime.media import ImageServiceInfo, raster_info
from ..runtime.store import atomic_text
from .openai_compatible import ProviderFailure

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 48 * 1024 * 1024


class ImageGatewayConfig(Model):
    model: Text
    api_key: SecretStr = Field(repr=False, exclude=True)
    base_url: SecretStr = Field(repr=False, exclude=True)
    timeout_seconds: Seconds = Field(default=600.0, gt=0)
    size: str = Field(default="1024x1024", pattern=r"^(auto|[1-9]\d{1,4}x[1-9]\d{1,4})$")
    response_format: Literal["url", "b64_json"] | None = None
    quality: Text | None = None
    # Chroma key works with RGB-only image models. Native mode requires real alpha.
    transparency: Literal["chroma_key", "native"] = "chroma_key"
    resample: Literal["nearest", "lanczos"] = "nearest"
    chroma_tolerance: int = Field(default=40, ge=0, le=128, strict=True)
    license: Text = "AI-generated artwork; subject to the configured image provider's terms. No independent license verification."

    @model_validator(mode="after")
    def valid_gateway(self):
        value = urlsplit(self.base_url.get_secret_value())
        if (value.scheme not in ("https", "http") or not value.hostname
                or value.username or value.password or value.query or value.fragment
                or not self.api_key.get_secret_value().strip()):
            raise ValueError("Invalid image gateway URL or credential")
        return self

    def revised(self, **changes):
        return type(self).model_validate({
            **self.model_dump(), "api_key": self.api_key, "base_url": self.base_url, **changes,
        })

    @classmethod
    def from_env(cls, env_file: Path, *, environ: Mapping[str, str] | None = None):
        """Use ART_IMAGE overrides, otherwise the existing shared gateway."""
        from dotenv import dotenv_values

        values = {**dotenv_values(env_file, interpolate=False), **(os.environ if environ is None else environ)}
        def setting(name, default=None):
            return values.get("ART_IMAGE_" + name) or default
        model = setting("MODEL")
        key = setting("API_KEY", values.get("MODEL_GATEWAY_API_KEY"))
        url = setting("BASE_URL", values.get("MODEL_GATEWAY_BASE_URL"))
        if not model or not key or not url:
            raise ProviderFailure("Configure ART_IMAGE_MODEL and image or shared gateway credentials; see docs/fixed_loop.md")
        try:
            options = {"license": setting("LICENSE")} if setting("LICENSE") else {}
            return cls(model=model, api_key=key, base_url=url,
                timeout_seconds=float(setting("TIMEOUT_SECONDS", "600")),
                size=setting("SIZE", "1024x1024"), response_format=setting("RESPONSE_FORMAT"),
                quality=setting("QUALITY"), transparency=setting("TRANSPARENCY", "chroma_key"),
                resample=setting("RESAMPLE", "nearest"),
                chroma_tolerance=int(setting("CHROMA_TOLERANCE", "40")), **options)
        except (ValueError, TypeError):
            raise ProviderFailure("Invalid ART_IMAGE configuration; check docs/fixed_loop.md") from None

    def public_settings(self):
        return {"adapter": "openai-compatible-images.v1", **self.model_dump(mode="json"),
                "max_retries": 0, "images_per_request": 1, "output_format": "png",
                "max_image_bytes": MAX_IMAGE_BYTES, "max_response_bytes": MAX_RESPONSE_BYTES,
                "normalization": "asset-pixels.v3: native resolution, no resampling, lossless PNG, matte cleanup and conservative sprite checks",
                "output_geometry": "source_native", "resample_applied": False}

    @property
    def configuration_id(self):
        data = {**self.public_settings(), "gateway": self.base_url.get_secret_value()}
        return "live-image-" + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:24]

    @property
    def info(self):
        return ImageServiceInfo(provider_id=self.configuration_id, is_mock=False, license=self.license,
                                output_geometry="source_native")


class OpenAICompatibleImageProvider:
    """Host-only network service. Raw and processed images stay auditable.

    Only the final PNG enters the role workspace. No SDK retries, model fallback,
    hidden edits API calls, or credentials forwarded to returned image URLs.
    """

    def __init__(self, config: ImageGatewayConfig, *, capture_directory: Path,
                 http_client=None, download_client=None):
        import httpx

        self.config, self.info = config, config.info
        self.capture_directory = Path(capture_directory)
        self.capture_directory.mkdir(parents=True, exist_ok=False)
        self._client = http_client or httpx.AsyncClient(follow_redirects=False)
        self._downloads = download_client or httpx.AsyncClient(follow_redirects=False)
        self.calls: list[dict] = []

    def _save(self, path, value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        for secret in (self.config.api_key.get_secret_value(), self.config.base_url.get_secret_value()):
            text = text.replace(secret, "[REDACTED]")
        # Image URLs may carry signed credentials. The downloaded bytes are saved instead.
        text = re.sub(r"https?://[^\s\"<>\\]+", "[REDACTED_URL]", text)
        atomic_text(path, text)

    def _image_prompt(self, job):
        from .image_prompts import is_actor, subject_instructions
        grid = (f"Produce one image with {job.columns} columns and {job.rows} rows of equally sized cells. "
                f"Frame meanings in row-major order (never render these words): {json.dumps(job.frame_names)}. "
                "Keep every subject fully inside its cell, with consistent scale and alignment. "
                "Fill the regular grid edge to edge; no presentation cards, poster margins, sample scenes or comparison panels. "
                "No grid lines, labels, captions or watermarks. ")
        if job.layout:
            from ..runtime.asset_normalization import regions_for
            regions = regions_for(job.layout, job.width, job.height, job.columns, job.rows, job.frame_names)
            grid = ("Produce one image following these logical layout rectangles: " +
                    json.dumps([r.model_dump(mode="json") for r in regions]) + ". "
                    "Each name describes its subject; never render names or coordinates as text. "
                    "Preserve the specified margins and gaps. Keep subjects fully inside their rectangles, "
                    "with consistent scale and pivot alignment across animation frames. "
                    "No grid lines, captions, watermarks, presentation cards or sample scenes. "
                    "Coordinates refer to the logical dimensions below; preserve their proportional positions on the native canvas. ")
        if job.transparent_background:
            background = ("Use a perfectly flat solid magenta (#FF00FF) background, including the gaps between frames. "
                          "Do not use magenta in the subjects. No shadows or gradients on the background. "
                          "The host will remove this exact background color to create transparency."
                          if self.config.transparency == "chroma_key" else
                          "Use a genuinely transparent background with an alpha channel, not a drawn checkerboard.")
        else:
            background = "Create a complete opaque image."
        return (subject_instructions(job) + "\n\n" + job.prompt + "\n\n" + grid +
                f"The logical layout canvas is {job.width} by {job.height}; it is not a request to rasterize at that low resolution. "
                "Render sharp detail at the full requested generation resolution. The host keeps native pixels and "
                "crops cells without downsampling, upscaling or stretching. Draw natural subject proportions. "
                "Keep subject scale and alignment consistent across cells. " + background +
                (" FINAL ACTOR CHECK: only the requested character poses on the specified removable matte or real alpha. "
                 "No text of any language, typography, annotations, labels, numbers, logos, watermark, scenery, ground, "
                 "pedestal, border, card, UI, cast shadow or decorative objects. Pose names and instructions are metadata, "
                 "never visible image content. These constraints also apply when the asset/style description mentions them."
                 if is_actor(job) else ""))

    def _payload(self, job):
        payload = {"model": self.config.model, "n": 1, "size": self.config.size,
                   "prompt": self._image_prompt(job)}
        if self.config.response_format:
            payload["response_format"] = self.config.response_format
        if self.config.quality:
            payload["quality"] = self.config.quality
        if job.transparent_background and self.config.transparency == "native":
            payload.update(background="transparent", output_format="png")
        return payload

    async def _read_response(self, client, method, url, *, limit, timeout, **kwargs):
        import httpx

        async with client.stream(method, url, timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
                                 follow_redirects=False, **kwargs) as response:
            data = bytearray()
            async for part in response.aiter_bytes():
                data.extend(part)
                if len(data) > limit:
                    raise ProviderFailure("Image gateway response exceeded the configured byte limit")
            return response.status_code, response.headers, bytes(data)

    @staticmethod
    def _validate_download_url(url):
        try:
            value = urlsplit(url)
            host = (value.hostname or "").lower().rstrip(".")
            if (value.scheme != "https" or not host or value.username or value.password
                    or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
                raise ValueError()
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ValueError()
        except (ValueError, TypeError):
            raise ProviderFailure("Image gateway returned an unsafe download URL") from None

    async def _download(self, url, timeout):
        from urllib.parse import urljoin

        for _ in range(4):
            self._validate_download_url(url)
            status, headers, data = await self._read_response(
                self._downloads, "GET", url, limit=MAX_IMAGE_BYTES, timeout=timeout)
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                url = urljoin(url, headers["location"])
                continue
            if status != 200:
                raise ProviderFailure(f"Image download returned HTTP {status}; no automatic retry was made")
            return data
        raise ProviderFailure("Image download exceeded the redirect limit")

    def _normalize(self, source, job):
        from PIL.PngImagePlugin import PngInfo
        from ..runtime.image_processing import prepare_generated, PROCESSING_KEY
        details = raster_info(source)
        try:
            image, report = prepare_generated(source, job, transparency=self.config.transparency,
                tolerance=self.config.chroma_tolerance, resample=self.config.resample)
        except ValueError as exc:
            raise ProviderFailure(str(exc)) from exc
        output = io.BytesIO()
        metadata = PngInfo()
        # Only rejection findings travel with the candidate PNG. The full transform
        # stays in image_calls metadata; a forged clean report cannot bypass checks.
        metadata.add_text(PROCESSING_KEY, json.dumps({"findings": report["findings"][:128]}))
        image.save(output, format="PNG", pnginfo=metadata)
        result = output.getvalue()
        if len(result) > MAX_IMAGE_BYTES:
            raise ProviderFailure("Normalized image exceeded the byte limit")
        return result, {"source": details, "output": raster_info(result), **report}

    async def _generate(self, payload, job, directory, metadata, timeout):
        status, _, body = await self._read_response(self._client, "POST",
            self.config.base_url.get_secret_value().rstrip("/") + "/images/generations",
            headers={"Authorization": "Bearer " + self.config.api_key.get_secret_value()},
            json=payload, limit=MAX_RESPONSE_BYTES, timeout=timeout)
        metadata["http_status"] = status
        try:
            response = json.loads(body)
        except (ValueError, UnicodeError):
            self._save(directory / "response.txt", body[:65536].decode("utf-8", errors="replace"))
            raise ProviderFailure(f"Image gateway returned non-JSON data (HTTP {status})") from None
        self._save(directory / "response.json", response)
        if status != 200:
            raise ProviderFailure(f"Image gateway returned HTTP {status}; no automatic retry was made")
        entries = response.get("data") if isinstance(response, dict) else None
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            raise ProviderFailure("Image gateway must return exactly one image in data")
        entry = entries[0]
        if entry.get("b64_json"):
            try:
                source = base64.b64decode(entry["b64_json"], validate=True)
            except (ValueError, TypeError):
                raise ProviderFailure("Image gateway returned invalid base64 data") from None
            metadata["response_encoding"] = "b64_json"
        elif isinstance(entry.get("url"), str) and entry["url"]:
            source = await self._download(entry["url"], timeout)
            metadata["response_encoding"] = "url"
        else:
            raise ProviderFailure("Image response contains neither b64_json nor a download URL")
        return self._store_image(source, job, directory, metadata)

    def _store_image(self, source, job, directory, metadata):
        if not source or len(source) > MAX_IMAGE_BYTES:
            raise ProviderFailure("Generated image is empty or exceeds the byte limit")
        details = raster_info(source)
        extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[details["media_type"]]
        (directory / f"source.{extension}").write_bytes(source)
        metadata.update(source_file=f"source.{extension}", source_sha256=hashlib.sha256(source).hexdigest())
        result, normalization = self._normalize(source, job)
        (directory / "output.png").write_bytes(result)
        metadata.update(normalization=normalization, sha256=hashlib.sha256(result).hexdigest(),
                        size_bytes=len(result), output_file="output.png", status="completed")
        return result

    async def generate(self, prompt: str, *, timeout: float) -> bytes:
        import httpx

        job = ImageJob.model_validate_json(prompt)
        if job.layout:
            from ..runtime.asset_normalization import regions_for
            try:
                regions_for(job.layout, job.width, job.height, job.columns, job.rows, job.frame_names)
            except ValueError:
                raise ProviderFailure("Image job layout is inconsistent") from None
        elif job.width % job.columns or job.height % job.rows or len(job.frame_names) != job.columns * job.rows:
            raise ProviderFailure("Image job dimensions and frame grid are inconsistent")
        allowed = min(timeout, self.config.timeout_seconds)
        if allowed <= 0:
            raise ProviderFailure("Image deadline has already expired")
        directory = self.capture_directory / f"{len(self.calls) + 1:03}"
        directory.mkdir()
        payload = self._payload(job)
        self._save(directory / "job.json", job.model_dump(mode="json"))
        self._save(directory / "request.json", payload)
        metadata = {"started_at": utc_now().isoformat(), "status": "started",
                    "timeout_seconds": allowed, "provider": self.info.model_dump(mode="json"),
                    "model": self.config.model, "capture": directory.name}
        self.calls.append(metadata)
        self._save(directory / "metadata.json", metadata)
        try:
            # Bound POST + redirects/download together, not a new deadline per operation.
            return await asyncio.wait_for(self._generate(payload, job, directory, metadata, allowed), timeout=allowed)
        except asyncio.CancelledError:
            metadata.update(status="cancelled", error_code="cancelled")
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException):
            metadata.update(status="failed", error_code="provider_timeout")
            raise ProviderFailure("Image request timed out; no automatic retry was made") from None
        except httpx.RequestError:
            metadata.update(status="failed", error_code="provider_connection_error")
            raise ProviderFailure("Could not connect to the image service; no automatic retry was made") from None
        except ProviderFailure as exc:
            metadata.update(status="failed", error_code="provider_response_error", message=str(exc))
            raise
        except Exception:
            metadata.update(status="failed", error_code="provider_response_error")
            raise ProviderFailure("Invalid image response or local image processing failure; inspect image_calls metadata") from None
        finally:
            metadata["finished_at"] = utc_now().isoformat()
            self._save(directory / "metadata.json", metadata)

    async def aclose(self):
        await self._client.aclose()
        await self._downloads.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()
