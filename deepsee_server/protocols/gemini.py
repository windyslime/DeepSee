"""Google Gemini generateContent protocol adapter (shape-compatible)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator

from deepsee.errors import ComposeError, VisionBackendError

from .base import decode_base64_image, extract_image_from_url


def parse_request(body: dict) -> tuple[str, bytes | str | None]:
    """Extract the last text and the last image (Gemini shape).

    畸形结构(contents 非数组、contents/parts 项或 inline_data/file_data
    非对象、text 非字符串)抛 ``ValueError``,由端点映射为 400;多图时取
    **最后一张**(与设计 §2 一致)。``inline_data``(base64)解码为 bytes;
    ``file_data.file_uri`` 交给 ``extract_image_from_url``(http(s) 放行,
    file:// 拒绝)。
    """
    contents = body.get("contents")
    if "contents" in body and not isinstance(contents, list):
        raise ValueError("contents 必须是数组")
    text = ""
    image = None
    for content in contents or []:
        if not isinstance(content, dict):
            raise ValueError("contents 项必须是对象")
        parts = content.get("parts")
        if "parts" in content and not isinstance(parts, list):
            raise ValueError("parts 必须是数组")
        for part in parts or []:
            if not isinstance(part, dict):
                raise ValueError("parts 项必须是对象")
            if "text" in part:
                value = part["text"]
                if not isinstance(value, str):
                    raise ValueError("text 必须是字符串")
                text = value
            elif "inline_data" in part:
                inline = part["inline_data"]
                if not isinstance(inline, dict):
                    raise ValueError("inline_data 必须是对象")
                if "data" in inline:
                    data = inline["data"]
                    if not isinstance(data, str):
                        raise ValueError("data 必须是字符串")
                    if data:
                        image = decode_base64_image(data)
            elif "file_data" in part:
                fd = part["file_data"]
                if not isinstance(fd, dict):
                    raise ValueError("file_data 必须是对象")
                if "file_uri" in fd:
                    uri = fd["file_uri"]
                    if not isinstance(uri, str):
                        raise ValueError("file_uri 必须是字符串")
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
    """Chunk stream (newline-delimited JSON): vision as a leading part chunk.

    ``vision`` 作为**独立前置 chunk**(``parts`` 首位 ``{"text", "vision":
    True}``)发出,即使上游回答为空流也不会丢失。
    """
    try:
        async with contextlib.aclosing(chunks):
            if vision is not None:
                payload = {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"text": vision, "vision": True}],
                            },
                            "index": 0,
                        }
                    ]
                }
                yield json.dumps(payload, ensure_ascii=False).encode() + b"\n"
            async for chunk in chunks:
                payload = {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": chunk}]},
                            "index": 0,
                        }
                    ]
                }
                yield json.dumps(payload, ensure_ascii=False).encode() + b"\n"
    except (ComposeError, VisionBackendError) as exc:
        yield json.dumps(
            {"error": {"code": 502, "message": str(exc)}}, ensure_ascii=False
        ).encode() + b"\n"
