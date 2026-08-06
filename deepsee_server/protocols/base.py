"""Shared helpers for protocol adapters: image extraction & size limits."""

from __future__ import annotations

import re
from base64 import b64decode

from deepsee.pipeline.image import MAX_IMAGE_BYTES


def extract_image_from_url(url: str) -> bytes | str:
    """Accept base64 data: URLs (→ bytes) or http(s) URLs (→ URL string).

    http(s) URL 的下载防护(SSRF / 字节上限)在 ``load_image`` 层;data URL
    在此解码并做字节上限检查;``file://`` 与本地路径一律拒绝。
    """
    if not isinstance(url, str):
        raise ValueError(f"不支持的图片 URL 形式: {url!r}")
    if url.startswith("data:"):
        m = re.match(r"data:[^;]+;base64,(.*)", url, re.DOTALL)
        if not m:
            raise ValueError("仅支持 base64 data URL 图片")
        raw = b64decode(m.group(1))
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(
                f"图片数据过大(超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB)"
            )
        return raw
    if url.startswith("http://") or url.startswith("https://"):
        return url
    raise ValueError(f"不支持的图片 URL 形式: {url[:60]}")


def decode_base64_image(data: str) -> bytes:
    """Decode a bare base64 payload (Anthropic source / Gemini inline_data)."""
    if not isinstance(data, str) or not data:
        raise ValueError("图片 base64 数据缺失")
    raw = b64decode(data)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"图片数据过大(超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB)"
        )
    return raw
