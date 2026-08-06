"""OpenAI-compatible chat completions protocol adapter."""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from deepsee.errors import ComposeError, VisionBackendError

from .base import extract_image_from_url


def parse_request(body: dict) -> tuple[str, bytes | str | None]:
    """Extract the last user text and an optional image (OpenAI shape)."""
    text = ""
    image = None
    for msg in body.get("messages", []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                elif btype == "image_url":
                    url = block["image_url"].get("url", "")
                    if url and image is None:
                        image = extract_image_from_url(url)
    return text, image


def encode_text(answer: str, vision: str | None, model: str) -> dict:
    """Non-streaming completion payload with optional vision_analysis."""
    message: dict[str, Any] = {"role": "assistant", "content": answer}
    if vision is not None:
        message["vision_analysis"] = vision
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def encode_stream(
    chunks: AsyncIterator[str],
    vision: str | None,
    model: str,
) -> AsyncIterator[bytes]:
    """SSE stream: vision_analysis on the first chunk, then content, then [DONE].

    ``chunks`` 在结束/异常/取消时都会被 ``aclose``(不依赖 GC)。
    """
    try:
        async with contextlib.aclosing(chunks):
            async for chunk in chunks:
                delta: dict[str, Any] = {"content": chunk}
                if vision is not None:
                    delta["vision_analysis"] = vision
                    vision = None  # 只出现在首个 chunk
                payload = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": delta, "finish_reason": None}
                    ],
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
    except (ComposeError, VisionBackendError) as exc:
        yield (
            "data: "
            + json.dumps(
                {"error": {"message": str(exc), "type": "upstream_error"}},
                ensure_ascii=False,
            )
            + "\n\n"
        ).encode()
    yield b"data: [DONE]\n\n"
