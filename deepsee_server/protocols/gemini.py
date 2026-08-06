"""Google Gemini generateContent protocol adapter (shape-compatible)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator

from deepsee.errors import ComposeError, VisionBackendError

from .base import decode_base64_image, extract_image_from_url


def parse_request(body: dict) -> tuple[str, bytes | str | None]:
    """Extract the last text and an optional image (Gemini shape).

    ``inline_data``(base64)解码为 bytes;``file_data.file_uri`` 交给
    ``extract_image_from_url``(http(s) 放行,file:// 拒绝)。
    """
    text = ""
    image = None
    for content in body.get("contents", []):
        for part in content.get("parts", []):
            if "text" in part:
                text = part["text"]
            elif "inline_data" in part and image is None:
                data = part["inline_data"].get("data", "")
                if data:
                    image = decode_base64_image(data)
            elif "file_data" in part and image is None:
                uri = part["file_data"].get("file_uri", "")
                if uri:
                    image = extract_image_from_url(uri)
    return text, image


def encode_text(answer: str, vision: str | None, model: str) -> dict:
    """Non-streaming generateContent payload with vision part first."""
    parts = []
    if vision is not None:
        parts.append({"text": vision, "vision": True})
    parts.append({"text": answer})
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 0,
            "candidatesTokenCount": 0,
            "totalTokenCount": 0,
        },
    }


async def encode_stream(
    chunks: AsyncIterator[str],
    vision: str | None,
    model: str,
) -> AsyncIterator[bytes]:
    """Chunk stream (newline-delimited JSON): vision part on first chunk."""
    try:
        async with contextlib.aclosing(chunks):
            async for chunk in chunks:
                parts = []
                if vision is not None:
                    parts.append({"text": vision, "vision": True})
                    vision = None
                parts.append({"text": chunk})
                payload = {
                    "candidates": [
                        {"content": {"role": "model", "parts": parts}, "index": 0}
                    ]
                }
                yield json.dumps(payload, ensure_ascii=False).encode() + b"\n"
    except (ComposeError, VisionBackendError) as exc:
        yield json.dumps(
            {"error": {"code": 502, "message": str(exc)}}, ensure_ascii=False
        ).encode() + b"\n"
