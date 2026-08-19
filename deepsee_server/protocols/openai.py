"""OpenAI-compatible chat completions protocol adapter."""

from __future__ import annotations

import contextlib
import copy
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from deepsee.errors import ComposeError, VisionBackendError

from .base import extract_image_from_url


_SUPPORTED_CHAT_FIELDS = {
    "model",
    "messages",
    "stream",
    "stream_options",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "response_format",
    "seed",
    "store",
    "user",
}
_SUPPORTED_ROLES = {"system", "user", "assistant", "tool"}


@dataclass(frozen=True)
class ParsedChatRequest:
    messages: list[dict[str, Any]]
    stream: bool
    params: dict[str, Any]
    image_count: int


def parse_chat_request(body: dict[str, Any]) -> ParsedChatRequest:
    """Validate a Chat Completions request without flattening its messages."""
    unknown = set(body) - _SUPPORTED_CHAT_FIELDS
    if unknown:
        raise ValueError(
            "不支持的请求参数: "
            + ", ".join(sorted(str(value) for value in unknown))
        )

    messages = body.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages 必须是数组")
    if not messages:
        raise ValueError("messages 不能为空")

    image_count = 0
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("messages 项必须是对象")
        role = message.get("role")
        if role not in _SUPPORTED_ROLES:
            raise ValueError("role 必须是 system、user、assistant 或 tool")

        content = message.get("content")
        assistant_tool_call = (
            role == "assistant"
            and content is None
            and isinstance(message.get("tool_calls"), list)
        )
        if not (
            isinstance(content, str)
            or isinstance(content, list)
            or assistant_tool_call
        ):
            raise ValueError("content 必须是字符串或数组")

        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    raise ValueError("content 块必须是对象")
                block_type = block.get("type")
                if block_type == "text":
                    if not isinstance(block.get("text"), str):
                        raise ValueError("text 必须是字符串")
                elif block_type == "image_url":
                    image_url = block.get("image_url")
                    if not isinstance(image_url, dict):
                        raise ValueError("image_url 必须是对象")
                    url = image_url.get("url")
                    if not isinstance(url, str) or not url:
                        raise ValueError("url 必须是非空字符串")
                    image_count += 1

    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise ValueError("stream 必须是布尔值")
    params = {
        key: copy.deepcopy(value)
        for key, value in body.items()
        if key not in {"model", "messages", "store", "stream"}
    }
    return ParsedChatRequest(
        messages=copy.deepcopy(messages),
        stream=stream,
        params=params,
        image_count=image_count,
    )


def encode_upstream_response(
    response: dict[str, Any],
    *,
    vision: str | None = None,
    include_vision: bool = False,
) -> dict[str, Any]:
    """Return an upstream response unchanged, optionally adding vision data."""
    payload = copy.deepcopy(response)
    if not (include_vision and vision):
        return payload
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return payload
    choice = choices[0]
    if not isinstance(choice, dict):
        return payload
    message = choice.get("message")
    if isinstance(message, dict):
        message["vision_analysis"] = vision
    return payload


async def encode_upstream_stream(
    chunks: AsyncIterator[dict[str, Any]],
    *,
    vision: str | None = None,
    include_vision: bool = False,
    on_error: Callable[[str], None] | None = None,
) -> AsyncIterator[bytes]:
    """Encode raw upstream chunks as SSE without dropping fields."""
    saw_finish = False
    stream_id: str | None = None
    vision_emitted = False
    emit_done = False
    try:
        async with contextlib.aclosing(chunks):
            async for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                stream_id = stream_id or chunk.get("id")
                if include_vision and vision and not vision_emitted:
                    payload = {
                        "id": stream_id or f"chatcmpl-{uuid.uuid4().hex[:12]}",
                        "object": "chat.completion.chunk",
                        "created": chunk.get("created", int(time.time())),
                        **({"model": chunk["model"]} if "model" in chunk else {}),
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"vision_analysis": vision},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
                    vision_emitted = True
                choices = chunk.get("choices")
                if isinstance(choices, list) and choices:
                    choice = choices[0]
                    if isinstance(choice, dict) and choice.get("finish_reason") is not None:
                        saw_finish = True
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            if include_vision and vision and not vision_emitted:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": stream_id or f"chatcmpl-{uuid.uuid4().hex[:12]}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"vision_analysis": vision},
                                    "finish_reason": None,
                                }
                            ],
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                ).encode()
            if not saw_finish:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": stream_id or f"chatcmpl-{uuid.uuid4().hex[:12]}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "choices": [
                                {"index": 0, "delta": {}, "finish_reason": "stop"}
                            ],
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                ).encode()
            emit_done = True
    except (ComposeError, VisionBackendError) as exc:
        if on_error is not None:
            # 流式响应无法改变 HTTP 状态码,必须让 trace 记录真实失败
            on_error("upstream_error")
        yield (
            "data: "
            + json.dumps(
                {"error": {"message": str(exc), "type": "upstream_error"}},
                ensure_ascii=False,
            )
            + "\n\n"
        ).encode()
        emit_done = True
    finally:
        # A client disconnect closes this async generator with GeneratorExit or
        # CancelledError. Do not yield from finally in that path: async generators
        # must finish closing without attempting to write another response chunk.
        if emit_done:
            yield b"data: [DONE]\n\n"


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
        content = msg.get("content")
        if "content" in msg and not (
            isinstance(content, str) or isinstance(content, list)
        ):
            # content 字段存在但既非字符串也非数组(含 null/数字/对象),
            # 对所有 role 生效,不因 role != user 而绕过
            raise ValueError("content 必须是字符串或数组")
        if msg.get("role") != "user":
            continue
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
                    if "url" in img:
                        url = img["url"]
                        if not isinstance(url, str):
                            raise ValueError("url 必须是字符串")
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
    on_error: Callable[[str], None] | None = None,
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
        if on_error is not None:
            # 流式响应无法改变 HTTP 状态码,必须让 trace 记录真实失败
            on_error("upstream_error")
        yield (
            "data: "
            + json.dumps(
                {"error": {"message": str(exc), "type": "upstream_error"}},
                ensure_ascii=False,
            )
            + "\n\n"
        ).encode()
    yield b"data: [DONE]\n\n"
