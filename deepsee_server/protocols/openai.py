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
    """Extract the last user text and the last image (OpenAI shape).

    畸形结构(messages 非数组、messages/content 项非对象、image_url 非对象、
    text 非字符串)抛 ``ValueError``,由端点映射为 400;多图时取**最后一张**
    (与设计 §2 一致)。
    """
    messages = body.get("messages")
    if "messages" in body and not isinstance(messages, list):
        raise ValueError("messages 必须是数组")
    text = ""
    image = None
    for msg in messages or []:
        if not isinstance(msg, dict):
            raise ValueError("messages 项必须是对象")
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    raise ValueError("content 块必须是对象")
                btype = block.get("type")
                if btype == "text":
                    value = block.get("text", "")
                    if not isinstance(value, str):
                        raise ValueError("text 必须是字符串")
                    text = value
                elif btype == "image_url":
                    img = block.get("image_url")
                    if not isinstance(img, dict):
                        raise ValueError("image_url 必须是对象")
                    url = img.get("url", "")
                    if url:
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
    """SSE stream: vision_analysis as a leading chunk, then content, then [DONE].

    ``vision`` 作为**独立前置 chunk**(``delta.vision_analysis``)发出,即使
    上游回答为空流也不会丢失;``chunks`` 在结束/异常/取消时都会被
    ``aclose``(不依赖 GC)。
    """
    try:
        async with contextlib.aclosing(chunks):
            if vision is not None:
                payload = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"vision_analysis": vision},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
            async for chunk in chunks:
                payload = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": chunk},
                            "finish_reason": None,
                        }
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
