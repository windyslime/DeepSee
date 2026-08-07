"""Anthropic messages protocol adapter (shape-compatible)."""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator

from deepsee.errors import ComposeError, VisionBackendError

from .base import decode_base64_image, extract_image_from_url


def parse_request(body: dict) -> tuple[str, bytes | str | None]:
    """Extract the last user text and the last image (Anthropic shape).

    畸形结构(messages 非数组、messages/content 项或 image source 非对象、
    text 非字符串)抛 ``ValueError``,由端点映射为 400;多图时取**最后一张**
    (与设计 §2 一致)。图片块 ``{type: "image", source: ...}``:base64 source
    解码为 bytes;url source 交给 ``extract_image_from_url``(http(s) 放行,
    file:// 拒绝)。
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
                elif btype == "image":
                    source = block.get("source")
                    if not isinstance(source, dict):
                        raise ValueError("image source 必须是对象")
                    if source.get("type") == "base64":
                        data = source.get("data", "")
                        if data:
                            image = decode_base64_image(data)
                    elif source.get("type") == "url":
                        url = source.get("url", "")
                        if url:
                            image = extract_image_from_url(url)
    return text, image


def encode_text(answer: str, vision: str | None, model: str) -> dict:
    """Non-streaming message payload with optional top-level vision_analysis."""
    resp: dict = {
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": answer}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    if vision is not None:
        resp["vision_analysis"] = vision
    return resp


def _event(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


async def encode_stream(
    chunks: AsyncIterator[str],
    vision: str | None,
    model: str,
) -> AsyncIterator[bytes]:
    """SSE event stream: message_start → vision_analysis → text deltas → stop.

    整个事件序列(含前置事件)都在 ``aclosing`` 内:客户端在任何阶段断开
    (生成器被 ``aclose``)都会关闭上游迭代器。上游错误时只发 ``error``
    事件,不再发送 ``end_turn``/``message_stop`` 成功收尾。
    """
    try:
        async with contextlib.aclosing(chunks):
            yield _event(
                {
                    "type": "message_start",
                    "message": {
                        "id": f"msg_{uuid.uuid4().hex[:12]}",
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                        "stop_reason": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                }
            )
            if vision is not None:
                yield _event({"type": "vision_analysis", "vision": vision})
            yield _event(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            )
            errored = False
            try:
                async for chunk in chunks:
                    yield _event(
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": chunk},
                        }
                    )
            except (ComposeError, VisionBackendError) as exc:
                yield _event(
                    {
                        "type": "error",
                        "error": {"type": "upstream_error", "message": str(exc)},
                    }
                )
                errored = True
            if not errored:
                yield _event({"type": "content_block_stop", "index": 0})
                yield _event(
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn"},
                        "usage": {"output_tokens": 0},
                    }
                )
                yield _event({"type": "message_stop"})
    except GeneratorExit:
        raise
