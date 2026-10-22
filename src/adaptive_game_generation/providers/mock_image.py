"""Explicit deterministic PNG placeholders; no image-model or network call."""

import hashlib
import io

from ..roles.art_producer.contracts import ImageJob
from ..runtime.media import ImageServiceInfo


class MockImageProvider:
    info = ImageServiceInfo(provider_id="mock-image.v1", is_mock=True,
        license="Synthetic test placeholder; replace before assessing art quality. No third-party artwork used.")

    def __init__(self):
        self.calls: list[dict] = []

    async def generate(self, prompt, *, timeout):
        from PIL import Image, ImageDraw

        job = ImageJob.model_validate_json(prompt)
        digest = hashlib.sha256(prompt.encode()).digest()
        image = Image.new("RGBA", (job.width, job.height), (0, 0, 0, 0) if job.transparent_background else (*digest[:3], 255))
        draw = ImageDraw.Draw(image)
        cell_w, cell_h = job.width // job.columns, job.height // job.rows
        for index in range(job.columns * job.rows):
            x, y = (index % job.columns) * cell_w, (index // job.columns) * cell_h
            draw.rectangle((x, y, x + cell_w - 1, y + cell_h - 1), outline=(255, 0, 255, 255))
            if cell_w >= 32 and cell_h >= 16:
                draw.text((x + 2, y + 2), "MOCK", fill=(255, 255, 255, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        self.calls.append({"request": job.model_dump(mode="json"), "timeout_seconds": timeout,
                           "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
                           "provider": self.info.model_dump(mode="json")})
        return data
