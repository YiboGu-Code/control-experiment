"""Bounded media inspection and trusted image-service identity."""

from __future__ import annotations

import io
from typing import Literal

from pydantic import StrictBool

from ..models.base import Model, Text
from .errors import InvalidSubmission


class ImageServiceInfo(Model):
    provider_id: Text
    is_mock: StrictBool
    license: Text
    output_format: Literal["png"] = "png"
    # Trusted host contract, never inferred from model-authored PNG metadata.
    output_geometry: Literal["requested", "source_native"] = "requested"


def raster_info(data: bytes) -> dict:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("PNG", "JPEG", "WEBP"):
                raise ValueError("unsupported raster")
            width, height = image.size
            if width * height > 16_777_216 or width < 1 or height < 1:
                raise ValueError("oversize raster")
            info = {"width": width, "height": height,
                    "media_type": Image.MIME[image.format],
                    "has_alpha": "A" in image.getbands() or "transparency" in image.info}
            image.verify()
        return info
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise InvalidSubmission("Invalid, unsupported, or oversized raster image") from None
