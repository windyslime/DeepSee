"""Image loading and preprocessing shared by all vision backends."""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path
from typing import Union

import httpx
from PIL import Image, UnidentifiedImageError

from deepsee.errors import ImageError

ImageInput = Union[str, os.PathLike, bytes, "Image.Image"]

MAX_DIMENSION = 2048
SUPPORTED_FORMATS = ("JPEG", "PNG", "WEBP")
_HTTP_TIMEOUT = 30.0


def load_image(image: ImageInput) -> Image.Image:
    """Load an image from a local path, http(s) URL, raw bytes, or PIL Image."""
    if isinstance(image, Image.Image):
        return image

    data: bytes | None = None
    if isinstance(image, bytes):
        data = image
    elif isinstance(image, (str, os.PathLike)):
        text = os.fspath(image)
        if text.startswith("http://") or text.startswith("https://"):
            try:
                resp = httpx.get(text, timeout=_HTTP_TIMEOUT, follow_redirects=True)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise ImageError(f"图片下载失败: {text} ({exc.__class__.__name__})") from exc
            data = resp.content
        else:
            path = Path(text)
            if not path.is_file():
                raise ImageError(f"图片文件不存在: {path}")
            data = path.read_bytes()
    else:
        raise ImageError(f"不支持的图片输入类型: {type(image).__name__}")

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageError(f"无法解码图片: 格式不受支持或文件损坏") from exc

    if img.format not in SUPPORTED_FORMATS:
        raise ImageError(
            f"不支持的图片格式: {img.format};仅支持 {', '.join(SUPPORTED_FORMATS)}"
        )
    return img


def normalize_image(img: Image.Image) -> tuple[str, str]:
    """Resize (long edge <= MAX_DIMENSION), flatten alpha, re-encode as JPEG.

    Returns ``(media_type, base64_string)``.
    """
    img = img.copy()
    if img.mode != "RGB":
        img = img.convert("RGB")
    width, height = img.size
    long_edge = max(width, height)
    if long_edge > MAX_DIMENSION:
        scale = MAX_DIMENSION / long_edge
        img = img.resize(
            (round(width * scale), round(height * scale)), Image.LANCZOS
        )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return "image/jpeg", base64.b64encode(buf.getvalue()).decode("ascii")


def prepare_image(image: ImageInput) -> tuple[str, str]:
    """Load and normalize an image in one call."""
    return normalize_image(load_image(image))