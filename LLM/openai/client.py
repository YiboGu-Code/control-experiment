"""OpenAI-compatible model calls in plain `openai` SDK style.

`OpenAIClient` binds one model name to a configured `OpenAI` client so callers
do not repeat credentials on every request:

    client = OpenAIClient("kimi-k2.6")
    text = client.chat([{"role": "user", "content": "Hello"}])

    image_client = OpenAIClient(_env("ART_IMAGE_MODEL"))
    png = image_client.image("a red apple on a white background")

    text_client = OpenAIClient(_env("ART_PLANNER_MODEL"))
    markup = text_client.svg("a sunflower icon, flat style")

    text = text_client.chat_with_images("describe this frame", [Path("frame.png")])

Everything else stays native to the SDK: `chat_raw()` / `image_raw()` return the
raw `ChatCompletion` / `ImagesResponse` objects, and gateway-specific parameters
are passed straight through as keyword arguments (e.g. `extra_body={...}`).

Credentials come from the environment. The project-root `.env` is loaded
automatically on import (existing environment variables win):

    MODEL_GATEWAY_API_KEY
    MODEL_GATEWAY_BASE_URL

Retries stay disabled so that one logical call maps to exactly one HTTP
request, keeping call counts and token usage comparable across runs.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx
from dotenv import load_dotenv
from openai import OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from openai.types.images_response import ImagesResponse

DEFAULT_TIMEOUT = 180.0
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # LLM/openai/client.py -> repo root
ENV_FILE = PROJECT_ROOT / ".env"
_API_KEY_VARS = "MODEL_GATEWAY_API_KEY"
_BASE_URL_VARS = "MODEL_GATEWAY_BASE_URL"

load_dotenv(ENV_FILE)  # existing environment variables win

_SVG_SYSTEM_PROMPT = (
    "You output standalone SVG documents. Reply with the raw <svg>...</svg> "
    "markup only: no explanation and no markdown code fences."
)


def _env(name: str) -> str | None:
    """Read one environment variable, returning None when unset or empty."""
    return os.environ.get(name) or None


def _svg_document(reply: str) -> str:
    """Extract the `<svg>...</svg>` document from a model reply."""
    start, end = reply.find("<svg"), reply.rfind("</svg>")
    if start == -1 or end == -1:
        raise ValueError("model reply does not contain an SVG document")
    return reply[start : end + len("</svg>")]


def _data_uri(source: bytes | Path) -> str:
    """Encode one image as a base64 data URI, detecting PNG vs JPEG."""
    payload = source.read_bytes() if isinstance(source, Path) else source
    mime = "image/jpeg" if payload.startswith(b"\xff\xd8\xff") else "image/png"
    return f"data:{mime};base64,{base64.standard_b64encode(payload).decode('ascii')}"


class OpenAIClient:
    """One model bound to an `OpenAI` client configured from the environment."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        **kwargs: Any,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self._client = OpenAI(
            api_key=api_key or _env(_API_KEY_VARS),
            base_url=base_url or _env(_BASE_URL_VARS),
            timeout=timeout,
            max_retries=0,
            **kwargs,
        )

    def chat_raw(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        **options: Any,
    ) -> ChatCompletion:
        """Send one chat completion and return the raw `ChatCompletion`."""
        return self._client.chat.completions.create(
            model=self.model, messages=list(messages), **options
        )

    def chat(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        **options: Any,
    ) -> str:
        """Send one chat completion and return the assistant text."""
        return self.chat_raw(messages, **options).choices[0].message.content or ""

    def image_raw(self, prompt: str, **options: Any) -> ImagesResponse:
        """Send one image request and return the raw `ImagesResponse`."""
        return self._client.images.generate(
            model=self.model, prompt=prompt, **options
        )

    def image(self, prompt: str, **options: Any) -> bytes:
        """Generate one image and return its bytes.

        Gateways answer with either inline base64 or a temporary URL; both are
        accepted. Use `image_raw()` when a batch or the URL itself is needed.
        """
        item = self.image_raw(prompt, **options).data[0]
        if item.b64_json:
            return base64.b64decode(item.b64_json)
        response = httpx.get(item.url, timeout=self.timeout, follow_redirects=True)
        response.raise_for_status()
        return response.content

    def svg(self, prompt: str, **options: Any) -> str:
        """Generate an SVG document and return its markup.

        SVG comes from a text model's chat reply, so this requires a client
        built on a text model (e.g. `ART_PLANNER_MODEL`); image models have no
        chat endpoint. Save the result with `Path.write_text()`.
        """
        messages = [
            {"role": "system", "content": _SVG_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        return _svg_document(self.chat(messages, **options))

    @staticmethod
    def interleave(
        parts: Sequence[str | bytes | Path | Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Turn alternating text and images into message `content` parts.

        A `str` becomes a text part, `bytes`/`Path` an image data URI, and a
        mapping is passed through as-is (handy for a remote image URL).
        """
        content: list[dict[str, Any]] = []
        for part in parts:
            if isinstance(part, str):
                content.append({"type": "text", "text": part})
            elif isinstance(part, (bytes, Path)):
                image = {"url": _data_uri(part)}
                content.append({"type": "image_url", "image_url": image})
            else:
                content.append(dict(part))
        return content

    def chat_with_images(
        self,
        prompt: str,
        images: Sequence[bytes | Path],
        **options: Any,
    ) -> str:
        """Send `prompt` with `images` and return the assistant text.

        Images go first and the prompt last, mirroring how the benchmark judges
        feed sampled frames. Requires a vision-capable model.
        """
        content = self.interleave([*images, prompt])
        return self.chat([{"role": "user", "content": content}], **options)

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()


if __name__ == "__main__":
    # import subprocess

    # source = PROJECT_ROOT / "sunflower.png"
    # compressed = Path("/tmp/sunflower_1024.jpg")
    # subprocess.run(
    #     [
    #         "ffmpeg", "-loglevel", "error", "-y", "-i", str(source),
    #         "-vf", "scale='min(1024,iw)':-2", "-q:v", "2", str(compressed),
    #     ],
    #     check=True,
    # )
    # print(f"{source.stat().st_size} -> {compressed.stat().st_size} bytes")

    client = OpenAIClient(_env("DESIGNER_MODEL"))
    print(client.chat([{"role": "user", "content": "你好"}]))